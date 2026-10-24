"""`neura policies`, `neura policy push|show`, `neura check`: what robots can run."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, cast

from neura_cli import keys, sources, weights
from neura_cli.local_site import LocalSite
from neura_cli.session import CliError, Context, Session
from neura_cli.ui import when
from neura_contracts import authoring, parse_document, signing
from neura_contracts.inference import (
    NOMINAL_PASS_US,
    InferenceError,
    PolicyDeclaration,
    Timing,
    derive_limits,
    nominal_timing,
)
from neura_sdk.models import ArtifactSignature, ContractDocument, PolicyVersionCreate

RUNS = 5  # calls `neura check --run` makes, to time the policy


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sub.add_parser("policies", help="the workspace's policies").set_defaults(run=run_list)

    policy = sub.add_parser("policy", help="policies: push a version, show one")
    actions = policy.add_subparsers(dest="action", metavar="ACTION", required=True)
    push = actions.add_parser(
        "push",
        help="add a version of a policy",
        description="Add a version: a LeRobot checkpoint, a .py file with a @neura.policy "
        "class, or a container image pinned by digest. Neura works out what it reads and "
        "returns, checks it fits the robot, signs it with this machine's publisher key and "
        "puts its files where this machine's sites find them. The files never reach Neura.",
    )
    push.add_argument("path", help="checkpoint directory, policy.py[:Class], or image@sha256:...")
    push.add_argument("--name", required=True, help="the policy (created on first push)")
    _source_options(push)
    push.add_argument("--notes", default="", help="what changed, for the version list")
    push.add_argument(
        "--seal-key",
        type=Path,
        help="also upload the files to Neura's registry, encrypted with this key file",
    )
    push.set_defaults(run=run_push)

    show = actions.add_parser("show", help="a policy's versions and where they run")
    show.add_argument("name")
    show.set_defaults(run=run_show)

    check = sub.add_parser(
        "check",
        help="check a policy fits a robot, without pushing it",
        description="Read a policy as `neura policy push` would and check it against the "
        "robot: what it reads, what it returns, and whether it keeps up. With --run, a "
        "@neura.policy class is also called once on a made-up observation.",
    )
    check.add_argument("path")
    _source_options(check)
    check.add_argument(
        "--run", dest="call_once", action="store_true", help="call a @neura.policy class once"
    )
    check.add_argument("--robots", type=int, default=1, help="robots sharing one GPU")
    check.set_defaults(run=run_check)


def _source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--for", dest="robot", help="a robot or robot profile (default: the one)")
    parser.add_argument("--declare", type=Path, help="a container's declaration (YAML)")
    parser.add_argument("--profile", type=Path, help="a robot profile file, instead of --for")
    parser.add_argument(
        "--image",
        help="the worker image it runs in, pinned by digest (name@sha256:...): Neura's, with "
        "your dependencies added (default: Neura's worker image)",
    )


def _profile(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    if args.profile is not None:
        from neura_contracts import load_document

        try:
            document = load_document(args.profile.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CliError(f"Could not read {args.profile}.", str(error)) from error
        return document.model_dump(mode="json", exclude_none=True)
    return ctx.session().robot_profile(args.robot)


def _describe(source: sources.Source) -> str:
    declaration = source.declaration
    reads = []
    if declaration.state:
        reads.append("state")
    if declaration.cameras is None:
        reads.append("every camera")
    else:
        reads.extend(f"{role} {w}x{h}" for role, (w, h) in sorted(declaration.cameras.items()))
    if declaration.language:
        reads.append("a task")
    return f"reads {', '.join(reads) or 'nothing'}; returns {declaration.chunk_length} actions"


def _size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} GB"


# --- push --------------------------------------------------------------------------------------


def run_push(args: argparse.Namespace, ctx: Context) -> int:
    session, out = ctx.session(), ctx.out
    robot = _profile(ctx, args)
    robot_name = robot["metadata"]["name"]
    out.header(f"Pushing {args.name}")
    source = sources.load(args.path, robot, declare=args.declare, image=args.image)
    artifact = source.artifact(robot, args.name)
    digest = parse_document(artifact).digest()
    out.ok(f"Fits {robot_name}", _describe(source))

    key = keys.load(ctx.config, create=True)
    assert key is not None
    key_id, new_key = keys.ensure_registered(session, key)
    signature = cast(ArtifactSignature, signing.sign(digest, key))
    out.ok("Signed", key_id + (" (new key, registered with the workspace)" if new_key else ""))

    policy = session.policy(args.name, create=True)
    known = {
        v["status"]["artifact_digest"]: v
        for v in session.client.paginate(
            session.client.list_policy_versions, policy["metadata"]["id"]
        )
    }
    body: PolicyVersionCreate = {
        "spec": {
            "source": {"kind": source.kind, "reference": source.reference[:2000]},
            "model_artifact": {
                "document": cast(ContractDocument, artifact),
                "signatures": [signature],
            },
            "notes": args.notes,
        }
    }
    version = session.client.create_policy_version(policy["metadata"]["id"], body=body)
    name = version["status"]["name"]

    delivered = _deliver(ctx, session, source, args.seal_key)
    out.data({"version": version, "delivered_to": delivered})
    out.line()
    if digest in known:
        out.line(f"  {out.s.bold(name)} already had these files: nothing new.")
    else:
        about = f"{source.kind} · {_size(source.total_bytes())}"
        out.line(f"  {out.s.bold(name)}  {out.s.quiet(about)}")
    out.line()
    out.line(f"  Deploy it:  neura deploy {args.name} --to ROBOT")
    out.line()
    return 0


def _deliver(
    ctx: Context, session: Session, source: sources.Source, seal_key: Path | None
) -> list[str]:
    out = ctx.out
    places = []
    for site_name, directory in sorted(ctx.config.local_sites.items()):
        local = LocalSite.at(site_name, directory)
        if not local.exists():
            continue
        added, present = weights.to_directory(source.files, local.weights_dir)
        out.ok(
            f"Files at {site_name}",
            f"{added} added, {present} already there (this machine)"
            if added
            else f"all {present} already there (this machine)",
        )
        places.append(site_name)
    if seal_key is not None:
        uploaded = weights.sealed_upload(session, source.files, seal_key)
        out.ok("Sealed files uploaded", f"{uploaded} files, readable only with {seal_key.name}")
        places.append("neura-registry")
    if not places:
        out.hint(
            "The files are not on any site yet: sites find them in their own store, "
            "or push with --seal-key, or run `neura-agent import-weights` there."
        )
    return places


# --- show, list --------------------------------------------------------------------------------


def run_list(args: argparse.Namespace, ctx: Context) -> int:
    session, out = ctx.session(), ctx.out
    policies = session.policies()
    out.data(policies)
    out.line()
    if not policies:
        out.line("  No policies yet. Push one: neura policy push CHECKPOINT --name pick")
        out.line()
        return 0
    robots = session.robots()
    out.table(
        ("POLICY", "LATEST", "VERSIONS", "RUNNING ON"),
        (
            (
                p["spec"]["name"],
                f"v{p['status']['latest_version']}" if p["status"].get("latest_version") else "—",
                str(p["status"]["versions"]),
                str(sum(1 for r in robots if r["status"].get("policy_name") == p["spec"]["name"]))
                + " robots",
            )
            for p in policies
        ),
    )
    out.line()
    return 0


def run_show(args: argparse.Namespace, ctx: Context) -> int:
    session, out = ctx.session(), ctx.out
    policy = session.policy(args.name)
    versions = list(
        session.client.paginate(session.client.list_policy_versions, policy["metadata"]["id"])
    )
    robots = {r["metadata"]["id"]: r["spec"]["name"] for r in session.robots()}
    out.data({"policy": policy, "versions": versions})
    out.header(policy["spec"]["name"])
    if not versions:
        out.line(f"  No versions yet. Push one: neura policy push PATH --name {args.name}")
        out.line()
        return 0
    out.table(
        ("VERSION", "SOURCE", "ADDED", "RUNNING ON"),
        (
            (
                f"v{v['status']['number']}",
                f"{v['spec']['source']['kind']}"
                + ("" if v["status"].get("signed") else " (unsigned)"),
                when(v["metadata"].get("created_at")),
                ", ".join(robots.get(r, r) for r in v["status"]["running_robot_ids"]) or "—",
            )
            for v in versions
        ),
    )
    out.line()
    return 0


# --- check -------------------------------------------------------------------------------------


def run_check(args: argparse.Namespace, ctx: Context) -> int:
    out = ctx.out
    robot = _profile(ctx, args)
    robot_name = robot["metadata"]["name"]
    out.header(f"Checking {args.path}")
    source = sources.load(args.path, robot, declare=args.declare, image=args.image)
    artifact = source.artifact(robot, "check")
    out.ok(f"Fits {robot_name}", _describe(source))
    ok = True
    timing: Timing | None = None
    label = ""
    if args.call_once:
        if source.kind != "python":
            out.hint("--run calls @neura.policy classes; this is a " + source.kind + " policy.")
        else:
            measured_us = _run_once(ctx, source, robot)
            ok = measured_us is not None
            if measured_us is not None:
                timing, label = Timing({1: measured_us}), "measured on this machine"
    if timing is None and source.declaration.adapter in NOMINAL_PASS_US:
        timing, label = nominal_timing(source.declaration.adapter, "cuda"), "estimated, GPU"
    if timing is None:
        out.hint("How fast it runs is measured at the site when it prepares; --run times it here.")
    elif ok:
        try:
            limits = derive_limits(
                artifact,
                robot,
                timing,
                robots=args.robots,
                executed_actions=source.declaration.executed_actions,
            )
            out.ok(f"Keeps up ({label})", limits.summary())
        except InferenceError as error:
            out.fail(f"Does not keep up ({label})", *error.problems)
            ok = False
    out.data({"fits": True, "keeps_up": ok, "artifact": artifact})
    out.line()
    out.line(f"  Push it:  neura policy push {args.path} --name NAME")
    out.line()
    return 0 if ok else 1


def synthetic_observation(
    declaration: PolicyDeclaration, robot: dict[str, Any], *, seed: int = 0
) -> dict[str, Any]:
    """An observation shaped as the robot sends it: state in its order, images at the sizes the
    policy reads, and a task when it reads language."""
    import numpy as np

    rng = np.random.default_rng(seed)
    spec = robot["spec"]
    observation: dict[str, Any] = {}
    if declaration.state:
        features = spec["state_features"]
        observation["observation.state"] = rng.uniform(-1, 1, len(features)).astype(np.float32)
    cameras = declaration.cameras
    if cameras is None:
        cameras = {c["role"]: (int(c["width"]), int(c["height"])) for c in spec.get("cameras", [])}
    for role, (width, height) in cameras.items():
        observation[f"observation.images.{role}"] = rng.integers(
            0, 256, (height, width, 3), dtype=np.uint8
        )
    if declaration.language:
        observation["task"] = "pick up the cube"
    return observation


def _run_once(ctx: Context, source: sources.Source, robot: dict[str, Any]) -> int | None:
    """Call the class on made-up observations and check what it returns. Returns the slowest
    call in µs, or None when it fails."""
    import numpy as np

    out = ctx.out
    path_text, _, name = source.reference.rpartition(":")
    path = Path(path_text).resolve()
    durations = []
    try:
        cls = authoring.load_class(path.parent, f"{path.stem}:{name}")
        declared = authoring.authored(cls)
        assert declared is not None
        weights_dir = path.parent / declared.weights if declared.weights else path.parent
        instance = authoring.instantiate(cls, weights=weights_dir, device="cpu")
        for seed in range(RUNS):
            observation = synthetic_observation(source.declaration, robot, seed=seed)
            started = time.perf_counter()
            actions = np.asarray(instance.act(observation), dtype=np.float32)
            durations.append(time.perf_counter() - started)
    except Exception as error:  # the customer's code: anything can go wrong
        out.fail("Calling it failed", f"{type(error).__name__}: {error}")
        return None
    features = len(robot["spec"]["command_interface"])
    expected = (source.declaration.chunk_length, features)
    if actions.shape != expected:
        out.fail(
            "It returned the wrong shape",
            f"{tuple(actions.shape)}; the robot needs (chunk_length, action features) = {expected}",
        )
        return None
    if not np.isfinite(actions).all():
        out.fail("It returned NaN or infinite actions")
        return None
    slowest = max(durations)
    out.ok("Ran on this machine", f"{expected[0]}x{expected[1]} actions, {slowest * 1000:.0f} ms")
    return max(1, round(slowest * 1_000_000))
