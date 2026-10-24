"""Rollouts: one change to what some robots run, from a policy version to robots switched
(design §29.4).

A deploy goes through fixed steps; each is built on what already exists, so nothing here can make
a robot move that the site's own checks and its operator would not allow:

1. check    the version fits the robots: an execution profile is derived from the artifact, the
            robots' profile and a nominal forward pass, and checked for compatibility.
2. prepare  the site verifies and fetches the weights and measures the forward pass on its GPU
            (a prepare). The control plane derives the final timing limits from the measurement;
            if they differ, it prepares a revision with those limits.
3. shadow   when robots already run something and a shadow test was asked for, the new revision
            runs beside it on live observations, its actions never sent (an evaluation).
4. approve  an activation request: only a person at the site approves (or, on a bench site whose
            operator allowed it, the site's agent).
5. switch   robots switch at their next task boundary; the rollout is live once every one has.

A rollback returns robots to what they ran before the last rollout that reached them: approve and
switch only. Each step change is an event with a one-line summary (activity).

The driver advances a rollout when one of its operations changes (``on_operation``) and on a
periodic sweep, so a missed report only delays it.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neura_contracts import ContractError, ReasonCode, check_compatibility, parse_document
from neura_contracts.inference import (
    InferenceError,
    Timing,
    derive_execution_profile,
    nominal_timing,
)
from neura_control.clock import iso, utcnow
from neura_control.db.models import (
    ActivationRequest,
    ContractDocument,
    Deployment,
    Evaluation,
    Operation,
    Policy,
    PolicyVersion,
    Project,
    Revision,
    Robot,
    Rollout,
    Server,
    Site,
)
from neura_control.ids import Kind, new_id
from neura_control.problems import ProblemError
from neura_control.security.principal import Principal, PrincipalKind
from neura_control.services import contracts, operations, records, release_gates
from neura_control.services.revisions import report_json, revision_digest

log = logging.getLogger("neura.control.rollouts")

DRIVER = Principal(subject="neura:rollouts", kind=PrincipalKind.SYSTEM)
ACTIVE = ("checking", "preparing", "shadow_testing", "awaiting_approval", "switching")
TERMINAL = ("live", "stopped", "cancelled")
DEPLOY_STEPS = ("check", "prepare", "shadow", "approve", "switch")
ROLLBACK_STEPS = ("approve", "switch")
DEFAULT_SHADOW_S = 600
PASSING_VERDICTS = ("superior", "non_inferior")


# --- reading --------------------------------------------------------------------------------


def step(rollout: Rollout, name: str) -> dict[str, Any]:
    for entry in rollout.steps:
        if entry["name"] == name:
            return dict(entry)
    raise KeyError(name)


async def _robots(session: AsyncSession, robot_ids: Sequence[str]) -> list[Robot]:
    rows = (await session.scalars(select(Robot).where(Robot.id.in_(list(robot_ids))))).all()
    by_id = {r.id: r for r in rows}
    return [by_id[r] for r in robot_ids if r in by_id]


def _names(robots: Sequence[Robot]) -> str:
    names = [r.name for r in robots]
    if len(names) <= 3:
        return ", ".join(names)
    return f"{', '.join(names[:2])} and {len(names) - 2} more"


async def describe_version(session: AsyncSession, version_id: str | None) -> str:
    if version_id is None:
        return "the previous version"
    version = await session.get(PolicyVersion, version_id)
    if version is None:
        return version_id
    policy = await session.get(Policy, version.policy_id)
    return f"{policy.name if policy else version.policy_id} v{version.number}"


async def _device(session: AsyncSession, site_id: str) -> str:
    servers = (await session.scalars(select(Server).where(Server.site_id == site_id))).all()
    return "cuda" if any(s.observed.get("gpus") for s in servers) else "cpu"


def _measured(revision: Revision) -> Timing | None:
    """The forward pass the site measured when it prepared ``revision``, if it reported one."""
    assumptions = (revision.capacity or {}).get("assumptions") or {}
    by_batch = assumptions.get("latency_p99_ms_by_batch") or {}
    if (revision.capacity or {}).get("source") != "measured_profile" or not by_batch:
        return None
    try:
        return Timing({int(b): round(float(ms) * 1000) for b, ms in by_batch.items()})
    except (TypeError, ValueError):
        return None


# --- changing -------------------------------------------------------------------------------


async def _emit(session: AsyncSession, rollout: Rollout, summary: str, level: str = "info") -> None:
    rollout.version += 1
    rollout.updated_at = utcnow()
    await records.event(
        session,
        project_id=rollout.project_id,
        type="resource.updated",
        resource=rollout.id,
        version=rollout.version,
        summary=summary,
        level=level,
        site_id=rollout.site_id,
        rollout_id=rollout.id,
    )


def _set_step(rollout: Rollout, name: str, state: str, summary: str | None = None) -> None:
    now = iso(utcnow())
    steps = []
    for entry in rollout.steps:
        entry = dict(entry)
        if entry["name"] == name:
            if state == "running" and entry.get("started_at") is None:
                entry["started_at"] = now
            if state in ("passed", "skipped", "failed"):
                entry["started_at"] = entry.get("started_at") or now
                entry["finished_at"] = now
            entry["state"] = state
            if summary is not None:
                entry["summary"] = summary
        steps.append(entry)
    rollout.steps = steps


async def _stop(
    session: AsyncSession,
    rollout: Rollout,
    step_name: str | None,
    reason: str,
    message: str,
    *,
    state: str = "stopped",
) -> None:
    if step_name is not None:
        _set_step(rollout, step_name, "failed", message)
    for entry in rollout.steps:
        if entry["state"] in ("pending", "running") and entry["name"] != step_name:
            _set_step(rollout, entry["name"], "skipped")
    rollout.state, rollout.reason, rollout.message = state, reason, message
    rollout.finished_at = utcnow()
    what = await describe_version(session, rollout.policy_version_id)
    robots = await _robots(session, rollout.robot_ids)
    verb = "cancelled" if state == "cancelled" else "stopped"
    await _emit(
        session,
        rollout,
        f"{what} → {_names(robots)}: {verb}. {message}",
        "warning" if state == "cancelled" else "error",
    )
    await _retire_unused(session, rollout.deployment_id)


async def _start(
    session: AsyncSession,
    rollout: Rollout,
    project: Project,
    *,
    kind: str,
    target: str,
    command: dict[str, Any],
) -> Operation:
    operation = await operations.start(
        session,
        project,
        DRIVER,
        request_id=f"rollout:{rollout.id}",
        site_id=rollout.site_id,
        kind=kind,
        target=target,
        command=command,
    )
    rollout.operation_id = operation.id
    return operation


# --- planning -------------------------------------------------------------------------------


async def _active_rollouts(session: AsyncSession, deployment_id: str) -> list[Rollout]:
    return list(
        (
            await session.scalars(
                select(Rollout).where(
                    Rollout.deployment_id == deployment_id, Rollout.state.in_(ACTIVE)
                )
            )
        ).all()
    )


async def _deployment_for(
    session: AsyncSession,
    project: Project,
    site: Site,
    robots: Sequence[Robot],
    *,
    name: str,
    principal: Principal,
) -> Deployment:
    """The deployment these robots run in: theirs if they share one (robots not yet in any are
    added to it), else a new one. Robots spread over several deployments are refused."""
    ids = [r.id for r in robots]
    deployments = (
        await session.scalars(
            select(Deployment).where(
                Deployment.project_id == project.id, Deployment.site_id == site.id
            )
        )
    ).all()
    owners = {d.id: d for d in deployments if set(d.robot_ids) & set(ids)}
    if len(owners) > 1:
        groups = "; ".join(
            f"{', '.join(r.name for r in robots if r.id in d.robot_ids)} in {d.name}"
            for d in owners.values()
        )
        raise ProblemError(
            ReasonCode.API_CONFLICT,
            f"these robots run in different deployments ({groups}): deploy to each group on "
            "its own",
        )
    if owners:
        deployment = next(iter(owners.values()))
        missing = [r for r in ids if r not in deployment.robot_ids]
        if missing:
            deployment.robot_ids = [*deployment.robot_ids, *missing]
            deployment.observed = {
                **deployment.observed,
                "active_revision_ids": {
                    **deployment.observed.get("active_revision_ids", {}),
                    **dict.fromkeys(missing),
                },
            }
            deployment.version += 1
            deployment.updated_at = utcnow()
        return deployment
    taken = {d.name for d in deployments}
    base = f"{name}-{site.name}"[:56].rstrip("-")
    chosen, suffix = base, 2
    while chosen in taken:
        chosen, suffix = f"{base}-{suffix}", suffix + 1
    deployment = Deployment(
        id=new_id(Kind.DEPLOYMENT),
        project_id=project.id,
        site_id=site.id,
        name=chosen,
        display_name=f"{name} at {site.display_name}",
        robot_ids=ids,
        desired_revision_id=None,
        rollout={"strategy": "all_at_task_boundary"},
        capture={"mode": "off"},
        release_gates={"evaluations": []},
        labels={},
        observed={"phase": "Pending", "active_revision_ids": dict.fromkeys(ids)},
        created_by=principal.subject,
    )
    session.add(deployment)
    await session.flush()
    await records.event(
        session,
        project_id=project.id,
        type="resource.created",
        resource=deployment.id,
        version=1,
    )
    return deployment


async def _refuse_overlap(session: AsyncSession, deployment: Deployment, ids: list[str]) -> None:
    for other in await _active_rollouts(session, deployment.id):
        overlap = set(other.robot_ids) & set(ids)
        if overlap:
            robots = await _robots(session, sorted(overlap))
            raise ProblemError(
                ReasonCode.API_CONFLICT,
                f"rollout {other.id} is still in progress for {_names(robots)}: wait for it, "
                "or cancel it",
            )


async def plan_deploy(
    session: AsyncSession,
    project: Project,
    principal: Principal,
    *,
    site: Site,
    robots: Sequence[Robot],
    version: PolicyVersion,
    shadow_s: float | None,
    note: str = "",
    kind: str = "deploy",
) -> Rollout:
    """Start a deploy (or a try) of ``version`` to ``robots``. Raises a problem for what can be
    told at once; anything the site must establish stops the rollout with its reason."""
    if not robots:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, "name at least one robot")
    elsewhere = sorted(r.name for r in robots if r.site_id != site.id)
    if elsewhere:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, f"{', '.join(elsewhere)} are not at {site.name}"
        )
    if kind == "try" and site.kind != "bench":
        raise ProblemError(
            ReasonCode.API_CONFLICT,
            f"{site.name} is a production site: try only runs on a bench site; deploy instead",
        )
    policy = await session.get(Policy, version.policy_id)
    assert policy is not None
    deployment = await _deployment_for(
        session, project, site, robots, name=policy.name, principal=principal
    )
    ids = [r.id for r in robots]
    await _refuse_overlap(session, deployment, ids)
    active = deployment.observed.get("active_revision_ids", {})
    baseline = {r: active.get(r) for r in ids}
    if shadow_s is None:
        shadow_s = DEFAULT_SHADOW_S if site.kind == "production" else 0
    rollout = Rollout(
        id=new_id(Kind.ROLLOUT),
        project_id=project.id,
        site_id=site.id,
        deployment_id=deployment.id,
        kind=kind,
        policy_version_id=version.id,
        robot_ids=ids,
        options={"shadow_s": shadow_s, "note": note},
        state="checking",
        steps=[{"name": n, "state": "pending", "summary": ""} for n in DEPLOY_STEPS],
        message="",
        baseline_revision_ids=baseline,
        created_by=principal.subject,
    )
    session.add(rollout)
    await session.flush()
    what = await describe_version(session, version.id)
    await records.event(
        session,
        project_id=project.id,
        type="resource.created",
        resource=rollout.id,
        version=1,
        summary=f"{principal.subject} started {kind}ing {what} to {_names(robots)} at {site.name}",
        site_id=site.id,
        rollout_id=rollout.id,
    )
    await advance(session, rollout)
    return rollout


async def rollback_target(session: AsyncSession, robot: Robot) -> str | None:
    """What ``robot`` ran before the last rollout that reached it, if anything."""
    previous = (
        await session.scalars(
            select(Rollout)
            .where(Rollout.state == "live", Rollout.robot_ids.contains([robot.id]))
            .order_by(Rollout.id.desc())
            .limit(1)
        )
    ).first()
    if previous is None:
        return None
    target = previous.baseline_revision_ids.get(robot.id)
    return str(target) if target else None


async def plan_rollback(
    session: AsyncSession,
    project: Project,
    principal: Principal,
    *,
    site: Site,
    robots: Sequence[Robot],
    note: str = "",
) -> Rollout:
    if not robots:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, "name at least one robot")
    targets = {r.id: await rollback_target(session, r) for r in robots}
    nothing = sorted(r.name for r in robots if targets[r.id] is None)
    if nothing:
        raise ProblemError(
            ReasonCode.API_CONFLICT,
            f"{', '.join(nothing)} ran nothing before their last rollout: there is no version to "
            "go back to",
        )
    if len(set(targets.values())) > 1:
        raise ProblemError(
            ReasonCode.API_CONFLICT,
            "these robots ran different versions before: roll each back on its own",
        )
    target_id = next(iter(targets.values()))
    assert target_id is not None
    revision = await session.get(Revision, target_id)
    if revision is None or revision.phase != "Prepared":
        raise ProblemError(
            ReasonCode.API_CONFLICT,
            "the previous version is no longer prepared at the site: deploy it again instead",
        )
    deployment = await session.get(Deployment, revision.deployment_id)
    assert deployment is not None
    ids = [r.id for r in robots]
    missing = sorted(r.name for r in robots if r.id not in deployment.robot_ids)
    if missing:
        raise ProblemError(
            ReasonCode.API_CONFLICT,
            f"{', '.join(missing)} no longer run in {deployment.name}; deploy instead",
        )
    await _refuse_overlap(session, deployment, ids)
    active = deployment.observed.get("active_revision_ids", {})
    version = (
        await session.scalars(
            select(PolicyVersion).where(
                PolicyVersion.project_id == project.id,
                PolicyVersion.model_artifact_id == revision.model_artifact_id,
            )
        )
    ).first()
    rollout = Rollout(
        id=new_id(Kind.ROLLOUT),
        project_id=project.id,
        site_id=site.id,
        deployment_id=deployment.id,
        kind="rollback",
        policy_version_id=version.id if version else None,
        robot_ids=ids,
        options={"note": note},
        state="awaiting_approval",
        steps=[{"name": n, "state": "pending", "summary": ""} for n in ROLLBACK_STEPS],
        message="",
        revision_id=revision.id,
        baseline_revision_ids={r: active.get(r) for r in ids},
        created_by=principal.subject,
    )
    session.add(rollout)
    await session.flush()
    what = await describe_version(session, rollout.policy_version_id)
    await records.event(
        session,
        project_id=project.id,
        type="resource.created",
        resource=rollout.id,
        version=1,
        summary=f"{principal.subject} started rolling {_names(robots)} back to {what}",
        site_id=site.id,
        rollout_id=rollout.id,
    )
    await _request_activation(session, rollout, project, command_kind="rollback")
    return rollout


async def cancel(session: AsyncSession, rollout: Rollout, principal: Principal) -> None:
    """Stop a rollout before its robots switch. A pending approval at the site is withdrawn;
    robots that already switched stay on what they run (roll back to undo that)."""
    if rollout.state in TERMINAL:
        raise ProblemError(ReasonCode.API_CONFLICT, f"the rollout is already {rollout.state}")
    running = next((s["name"] for s in rollout.steps if s["state"] == "running"), None)
    operation = await _operation(session, rollout)
    await _stop(
        session,
        rollout,
        running,
        ReasonCode.ROLLOUT_CANCELLED.value,
        f"cancelled by {principal.subject}",
        state="cancelled",
    )
    if operation is not None and operation.state in ("Pending", "Running"):
        from neura_control.services.site_reports import update_operation

        session.add(_cancel_command(operation))
        await update_operation(
            session, operation.id, "Cancelled", message=f"cancelled by {principal.subject}"
        )


def _cancel_command(operation: Operation) -> Any:
    from neura_control.db.models import SiteCommand

    return SiteCommand(
        site_id=operation.site_id,
        command_id=new_id(Kind.OPERATION).replace("op_", "cmd_", 1),
        operation_id=operation.id,
        command={"kind": "cancel", "operation_id": operation.id},
    )


# --- advancing ------------------------------------------------------------------------------


async def advance(session: AsyncSession, rollout: Rollout) -> None:
    """Take the rollout as far as it can go now. Idempotent: it only acts on what changed."""
    for _ in range(8):  # each pass moves at most one step; a step never repeats
        before = (rollout.state, rollout.operation_id)
        if rollout.state not in ACTIVE:
            return
        project = await session.get(Project, rollout.project_id)
        assert project is not None
        if rollout.state == "checking":
            await _check(session, rollout, project)
        elif rollout.state == "preparing":
            await _prepared(session, rollout, project)
        elif rollout.state == "shadow_testing":
            await _shadowed(session, rollout, project)
        elif rollout.state in ("awaiting_approval", "switching"):
            await _activation(session, rollout)
        if (rollout.state, rollout.operation_id) == before:
            return


async def _check(session: AsyncSession, rollout: Rollout, project: Project) -> None:
    _set_step(rollout, "check", "running")
    version = await session.get(PolicyVersion, rollout.policy_version_id)
    assert version is not None
    robots = await _robots(session, rollout.robot_ids)
    profiles = {r.robot_profile_id for r in robots}
    if len(profiles) > 1:
        await _stop(
            session,
            rollout,
            "check",
            ReasonCode.ROLLOUT_ROBOT_TYPES_DIFFER.value,
            "these robots are of different types: deploy to each type on its own",
        )
        return
    artifact_row = await session.get(ContractDocument, version.model_artifact_id)
    robot_row = await session.get(ContractDocument, next(iter(profiles)))
    assert artifact_row is not None
    assert robot_row is not None
    device = await _device(session, rollout.site_id)
    adapter = artifact_row.document["spec"]["adapter"]["name"]
    precisions = next(
        (
            d["precisions"]
            for d in artifact_row.document["spec"]["devices"]
            if d["device"] == device
        ),
        ["fp32"],
    )
    precision = "bf16" if device == "cuda" and "bf16" in precisions else precisions[0]
    rollout.options = {**rollout.options, "device": device, "precision": precision}
    try:
        profile_document, limits = derive_execution_profile(
            artifact_row.document,
            robot_row.document,
            nominal_timing(adapter, device),
            device=device,
            precision=precision,
            robots=len(robots),
        )
        profile_row, _ = await contracts.register(
            session, project.id, profile_document, created_by=DRIVER.subject
        )
        artifact, robot_profile, profile = (
            parse_document(row.document) for row in (artifact_row, robot_row, profile_row)
        )
    except InferenceError as error:
        await _stop(session, rollout, "check", ReasonCode.ROLLOUT_TIMING_UNFIT.value, str(error))
        return
    except ContractError as error:
        await _stop(session, rollout, "check", error.code.value, str(error))
        return
    report = check_compatibility(artifact, robot_profile, profile)  # type: ignore[arg-type]
    if not report.compatible:
        await _stop(
            session,
            rollout,
            "check",
            report.issues[0].code.value,
            "; ".join(i.message for i in report.issues),
        )
        return
    revision = await _revision(session, rollout, artifact_row, robot_row, profile_row, report)
    rollout.provisional_revision_id = revision.id
    robot_spec = robot_row.document["spec"]
    hz = round(1_000_000 / robot_spec["control_period_us"])
    _set_step(
        rollout,
        "check",
        "passed",
        f"fits {len(robots)} {robot_row.document['metadata']['name']} robot"
        f"{'' if len(robots) == 1 else 's'}: {hz} Hz, {limits.actions_per_chunk}-action chunks",
    )
    await _prepare(session, rollout, project, revision)
    what = await describe_version(session, rollout.policy_version_id)
    await _emit(session, rollout, f"{what}: fits the robots; preparing at the site")


async def _revision(
    session: AsyncSession,
    rollout: Rollout,
    artifact_row: ContractDocument,
    robot_row: ContractDocument,
    profile_row: ContractDocument,
    report: Any,
) -> Revision:
    existing = (
        await session.scalars(
            select(Revision).where(
                Revision.deployment_id == rollout.deployment_id,
                Revision.revision_digest == revision_digest(report),
                Revision.phase.in_(("Created", "Preparing", "Prepared")),
            )
        )
    ).first()
    if existing is not None:
        return existing
    revision = Revision(
        id=new_id(Kind.REVISION),
        project_id=rollout.project_id,
        deployment_id=rollout.deployment_id,
        model_artifact_id=artifact_row.id,
        execution_profile_id=profile_row.id,
        robot_profile_id=robot_row.id,
        notes=f"rollout {rollout.id}",
        revision_digest=revision_digest(report),
        compatibility=report_json(report),
        phase="Created",
        created_by=DRIVER.subject,
    )
    session.add(revision)
    await session.flush()
    await records.event(
        session,
        project_id=rollout.project_id,
        type="resource.created",
        resource=revision.id,
        version=1,
    )
    return revision


async def _prepare(
    session: AsyncSession, rollout: Rollout, project: Project, revision: Revision
) -> None:
    rollout.state = "preparing"
    _set_step(rollout, "prepare", "running", "verifying the weights and timing the model")
    rollout.revision_id = revision.id
    if revision.phase == "Prepared":
        rollout.operation_id = None  # prepared before: nothing to wait for
        return
    if revision.phase == "Created":
        revision.phase = "Preparing"
        revision.version += 1
        revision.updated_at = utcnow()
    await _start(
        session,
        rollout,
        project,
        kind="prepare_revision",
        target=revision.id,
        command={
            "kind": "prepare",
            "revision_id": revision.id,
            "revision_digest": revision.revision_digest,
        },
    )


async def _operation(session: AsyncSession, rollout: Rollout) -> Operation | None:
    if rollout.operation_id is None:
        return None
    return await session.get(Operation, rollout.operation_id)


async def _prepared(session: AsyncSession, rollout: Rollout, project: Project) -> None:
    operation = await _operation(session, rollout)
    revision = await session.get(Revision, rollout.revision_id)
    assert revision is not None
    if operation is not None and operation.state in ("Pending", "Running"):
        return
    if operation is not None and operation.state != "Succeeded":
        await _stop(
            session,
            rollout,
            "prepare",
            operation.reason or ReasonCode.ROLLOUT_PREPARE_FAILED.value,
            operation.message or f"the site could not prepare the revision ({operation.state})",
        )
        return
    if revision.phase != "Prepared":
        return
    robots = len(rollout.robot_ids)
    capacity = revision.capacity or {}
    timing = _measured(revision)
    final = revision
    if timing is not None and revision.id == rollout.provisional_revision_id:
        # Timed: the limits the measurement allows, prepared once more if they differ.
        artifact_row = await session.get(ContractDocument, revision.model_artifact_id)
        robot_row = await session.get(ContractDocument, revision.robot_profile_id)
        assert artifact_row is not None
        assert robot_row is not None
        try:
            document, limits = derive_execution_profile(
                artifact_row.document,
                robot_row.document,
                timing,
                device=rollout.options.get("device", "cuda"),
                precision=rollout.options.get("precision", "fp32"),
                robots=robots,
            )
        except InferenceError as error:
            await _stop(
                session, rollout, "prepare", ReasonCode.ROLLOUT_TIMING_UNFIT.value, str(error)
            )
            return
        profile_row, _ = await contracts.register(
            session, project.id, document, created_by=DRIVER.subject
        )
        if profile_row.id != revision.execution_profile_id:
            report = check_compatibility(
                parse_document(artifact_row.document),  # type: ignore[arg-type]
                parse_document(robot_row.document),  # type: ignore[arg-type]
                parse_document(profile_row.document),  # type: ignore[arg-type]
            )
            if not report.compatible:
                await _stop(
                    session,
                    rollout,
                    "prepare",
                    report.issues[0].code.value,
                    "; ".join(i.message for i in report.issues),
                )
                return
            final = await _revision(session, rollout, artifact_row, robot_row, profile_row, report)
            rollout.options = {**rollout.options, "timing": limits.summary()}
            if final.phase != "Prepared":
                await _prepare(session, rollout, project, final)
                return
        rollout.options = {**rollout.options, "timing": limits.summary()}
    rollout.revision_id = final.id
    final_capacity = final.capacity or capacity
    most = final_capacity.get("max_robots")
    if most is not None and most < robots:
        await _stop(
            session,
            rollout,
            "prepare",
            ReasonCode.ADMISSION_CAPACITY_EXCEEDED.value,
            f"this site's GPU serves {most} robot{'' if most == 1 else 's'} of this version, not "
            f"{robots} (limited by {final_capacity.get('limiting_factor', 'its GPU')}): deploy to "
            "fewer robots, or add a GPU",
        )
        return
    timing_text = rollout.options.get("timing") or (
        "timed at the site (its measurements stay there)"
        if final_capacity
        else "prepared at the site"
    )
    _set_step(rollout, "prepare", "passed", f"weights verified; {timing_text}")
    if rollout.provisional_revision_id and rollout.provisional_revision_id != final.id:
        await _retire(session, rollout.provisional_revision_id)
    await _shadow(session, rollout, project)


async def _shadow(session: AsyncSession, rollout: Rollout, project: Project) -> None:
    deployment = await session.get(Deployment, rollout.deployment_id)
    assert deployment is not None
    running = Counter(r for r in rollout.baseline_revision_ids.values() if r)
    baseline = next((r for r, _ in running.most_common() if r != rollout.revision_id), None)
    duration = float(rollout.options.get("shadow_s") or 0)
    if rollout.kind == "try" or duration <= 0:
        _set_step(rollout, "shadow", "skipped", "not asked for")
        await _request_activation(session, rollout, project)
        return
    if baseline is None:
        _set_step(rollout, "shadow", "skipped", "nothing runs on these robots yet to compare with")
        await _request_activation(session, rollout, project)
        return
    evaluation = Evaluation(
        id=new_id(Kind.EVALUATION),
        project_id=project.id,
        deployment_id=deployment.id,
        candidate_revision_id=rollout.revision_id,
        baseline_revision_id=baseline,
        suite={
            "kind": "shadow",
            "duration_s": duration,
            "min_samples": 200,
            "every_nth": 1,
            "tolerance": 0.05,
        },
        phase="Pending",
        created_by=DRIVER.subject,
    )
    session.add(evaluation)
    await session.flush()
    operation = await _start(
        session,
        rollout,
        project,
        kind="evaluate",
        target=evaluation.id,
        command={
            "kind": "evaluate",
            "evaluation_id": evaluation.id,
            "candidate_revision_id": rollout.revision_id,
            "baseline_revision_id": baseline,
            "suite": evaluation.suite,
        },
    )
    evaluation.operation_id = operation.id
    rollout.evaluation_id = evaluation.id
    rollout.state = "shadow_testing"
    minutes = duration / 60
    _set_step(
        rollout,
        "shadow",
        "running",
        f"beside the running version for {minutes:g} min; its actions are never sent",
    )
    what = await describe_version(session, rollout.policy_version_id)
    await _emit(session, rollout, f"{what}: shadow test started ({minutes:g} min)")


async def _shadowed(session: AsyncSession, rollout: Rollout, project: Project) -> None:
    evaluation = await session.get(Evaluation, rollout.evaluation_id)
    if evaluation is None or evaluation.phase in ("Pending", "Running"):
        return
    if evaluation.phase != "Succeeded":
        if evaluation.reason == ReasonCode.AGENT_JOB_DEVICE_BUSY.value:
            _set_step(
                rollout,
                "shadow",
                "skipped",
                "no free GPU for a shadow test at the site; the person approving sees this",
            )
            await _request_activation(session, rollout, project)
            return
        await _stop(
            session,
            rollout,
            "shadow",
            evaluation.reason or ReasonCode.ROLLOUT_SHADOW_FAILED.value,
            f"the shadow test failed: {evaluation.message}",
        )
        return
    verdict = evaluation.verdict
    samples = (evaluation.report or {}).get("samples")
    seen = f" over {samples} observations" if samples is not None else ""
    if verdict in PASSING_VERDICTS:
        _set_step(rollout, "shadow", "passed", f"close to the running version{seen}")
    elif verdict == "inconclusive":
        _set_step(
            rollout,
            "shadow",
            "passed",
            f"inconclusive{seen}: too few observations to compare; the person approving sees this",
        )
    else:
        await _stop(
            session,
            rollout,
            "shadow",
            ReasonCode.ROLLOUT_SHADOW_INFERIOR.value,
            f"the shadow test found it far from the running version{seen} ({verdict})",
        )
        return
    await _request_activation(session, rollout, project)


async def _request_activation(
    session: AsyncSession,
    rollout: Rollout,
    project: Project,
    *,
    command_kind: str = "request_activation",
) -> None:
    deployment = await session.get(Deployment, rollout.deployment_id)
    site = await session.get(Site, rollout.site_id)
    assert deployment is not None
    assert site is not None
    assert rollout.revision_id is not None
    if command_kind == "request_activation":
        missing = await release_gates.unmet(
            session, deployment, rollout.revision_id, list(rollout.robot_ids)
        )
        if missing:
            await _stop(
                session,
                rollout,
                "approve",
                ReasonCode.API_RELEASE_GATE_UNMET.value,
                "the deployment's release gates require " + "; ".join(missing),
            )
            return
    kind = "activate_revision" if command_kind == "request_activation" else "rollback"
    command: dict[str, Any] = (
        {"kind": "request_activation", "revision_id": rollout.revision_id}
        if command_kind == "request_activation"
        else {"kind": "rollback", "to_revision_id": rollout.revision_id}
    )
    command["robot_ids"] = list(rollout.robot_ids)
    operation = await _start(
        session, rollout, project, kind=kind, target=rollout.revision_id, command=command
    )
    session.add(
        ActivationRequest(
            id=new_id(Kind.ACTIVATION_REQUEST),
            project_id=project.id,
            deployment_id=deployment.id,
            revision_id=rollout.revision_id,
            robot_ids=list(rollout.robot_ids),
            operation_id=operation.id,
            state="AwaitingLocalApproval",
            note=str(rollout.options.get("note", "")),
            created_by=rollout.created_by,
        )
    )
    rollout.state = "awaiting_approval"
    who = (
        "the bench site's agent approves it if its operator allowed that, else a person at "
        f"{site.name} runs: neura approve"
        if site.kind == "bench"
        else f"a person at {site.name} approves it on the site's server: neura approve"
    )
    _set_step(rollout, "approve", "running", who)
    what = await describe_version(session, rollout.policy_version_id)
    await _emit(session, rollout, f"{what}: waiting for approval at {site.name}")


async def _activation(session: AsyncSession, rollout: Rollout) -> None:
    operation = await _operation(session, rollout)
    if operation is None:
        return
    approved_by = operation.result.get("approved_by")
    what = await describe_version(session, rollout.policy_version_id)
    robots = await _robots(session, rollout.robot_ids)
    if rollout.state == "awaiting_approval" and approved_by and operation.state != "Failed":
        rollout.approved_by = str(approved_by)
        _set_step(rollout, "approve", "passed", f"approved by {approved_by}")
        _set_step(rollout, "switch", "running", "at each robot's next task boundary")
        rollout.state = "switching"
        await _emit(session, rollout, f"{what}: approved by {approved_by}; robots switching")
    if operation.state in ("Pending", "Running"):
        return
    if operation.state == "Succeeded":
        if rollout.state == "awaiting_approval":
            _set_step(rollout, "approve", "passed", f"approved by {approved_by or 'the site'}")
            rollout.approved_by = str(approved_by) if approved_by else None
        _set_step(rollout, "switch", "passed", f"{_names(robots)} switched")
        rollout.state, rollout.finished_at = "live", utcnow()
        rollout.message = f"{_names(robots)} run {what}"
        await _emit(session, rollout, f"{_names(robots)} now run {what}")
        await _retire_unused(session, rollout.deployment_id)
        return
    refused = operation.result.get("refused") or (
        operation.reason == ReasonCode.AGENT_ACTIVATION_REFUSED.value
    )
    step_name = "approve" if rollout.state == "awaiting_approval" else "switch"
    if operation.state == "Cancelled":
        await _stop(
            session,
            rollout,
            step_name,
            ReasonCode.ROLLOUT_CANCELLED.value,
            operation.message or "cancelled",
            state="cancelled",
        )
        return
    await _stop(
        session,
        rollout,
        step_name,
        operation.reason or ReasonCode.ROLLOUT_SWITCH_FAILED.value,
        operation.message or ("refused at the site" if refused else "the robots did not switch"),
    )


# --- bookkeeping ----------------------------------------------------------------------------


async def _retire(session: AsyncSession, revision_id: str) -> None:
    revision = await session.get(Revision, revision_id, with_for_update=True)
    if revision is None or revision.phase == "Retired":
        return
    deployment = await session.get(Deployment, revision.deployment_id)
    if (
        deployment is not None
        and revision_id in deployment.observed.get("active_revision_ids", {}).values()
    ):
        return
    revision.phase = "Retired"
    revision.version += 1
    revision.updated_at = utcnow()
    await records.event(
        session,
        project_id=revision.project_id,
        type="resource.updated",
        resource=revision.id,
        version=revision.version,
    )


async def _retire_unused(session: AsyncSession, deployment_id: str) -> None:
    """Retire the revisions a rollout created that nothing needs any more, so the site's
    manifest does not grow with every rollout. Kept: what any robot runs, what each robot would
    roll back to, and what a rollout in progress uses."""
    deployment = await session.get(Deployment, deployment_id)
    if deployment is None:
        return
    keep = {r for r in deployment.observed.get("active_revision_ids", {}).values() if r}
    rollouts = (
        await session.scalars(select(Rollout).where(Rollout.deployment_id == deployment_id))
    ).all()
    for robot_id in deployment.robot_ids:
        robot = await session.get(Robot, robot_id)
        if robot is not None:
            target = await rollback_target(session, robot)
            if target:
                keep.add(target)
    for row in rollouts:
        if row.state in ACTIVE:
            keep.update(r for r in (row.revision_id, row.provisional_revision_id) if r)
    created = {r.revision_id for r in rollouts if r.revision_id} | {
        r.provisional_revision_id for r in rollouts if r.provisional_revision_id
    }
    for revision_id in sorted(created - keep):
        await _retire(session, revision_id)


async def on_operation(session: AsyncSession, operation: Operation) -> None:
    """A site report moved ``operation``: advance the rollout waiting on it."""
    rollouts = (
        await session.scalars(
            select(Rollout)
            .where(Rollout.operation_id == operation.id, Rollout.state.in_(ACTIVE))
            .with_for_update()
        )
    ).all()
    for rollout in rollouts:
        await advance(session, rollout)


async def sweep(sessions: async_sessionmaker[AsyncSession]) -> int:
    """Advance every rollout in progress, one transaction each. Returns how many moved."""
    async with sessions() as session:
        ids = list(
            (await session.scalars(select(Rollout.id).where(Rollout.state.in_(ACTIVE)))).all()
        )
    moved = 0
    for rollout_id in ids:
        try:
            async with sessions() as session, session.begin():
                rollout = await session.get(Rollout, rollout_id, with_for_update=True)
                if rollout is None:
                    continue
                before = rollout.version
                await advance(session, rollout)
                moved += rollout.version != before
        except Exception:  # one rollout's failure must not stop the others
            log.exception("advancing rollout %s failed", rollout_id)
    return moved
