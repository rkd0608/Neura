"""The site agent's decisions (design §13, §16, §17).

Transport-free: the connection module delivers desired state and commands and sends what this class
puts in ``outbox``; the local API calls ``approve`` and ``refuse``. Every instruction from the cloud
is checked against local boundaries, recorded in the local audit log, and executed at most once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from neura.v1 import common_pb2, site_pb2
from neura_agent import placement, site_pki, trust
from neura_agent.artifacts import ArtifactError, ArtifactStore, load_keys
from neura_agent.gateway import (
    GatewayRunner,
    build_gateway_config,
    revision_documents,
    sandbox_config,
    write_gateway_config,
    write_revision_documents,
)
from neura_agent.identity import Identity, atomic_write
from neura_agent.outbound import Outbound, site_outbound
from neura_agent.releases import UpgradeState
from neura_agent.settings import AgentSettings
from neura_agent.site_pki import SitePki, default_gateway_names, unexpired
from neura_agent.state import Activation, AgentState, Assignment, AuditLog, StateStore
from neura_agent.upgrade import Upgrader
from neura_contracts import (
    ContractError,
    ExecutionProfile,
    ModelArtifact,
    ReasonCode,
    RobotProfile,
    canonical_json,
    content_digest,
    parse_document,
    pki,
    signing,
)
from neura_contracts.site_protocol import (
    CONTACT_NONCE_BYTES,
    MANIFEST_API_VERSION,
    contact_proof_payload,
)
from neura_runtime.binding import ExecutionBinding, IncompatibleBindingError
from neura_server import capacity
from neura_server.config import ServingConfig, load_profile_points
from neura_server.sandbox import ImageUnavailableError, ensure_image, launcher_for

log = logging.getLogger("neura.agent")
OUTBOX_LIMIT = 1000
# Nonces awaiting a contact proof; older ones are forgotten (a proof for them no longer counts).
OUTSTANDING_NONCES = 8
# Contact time is kept in memory on every proof but written to disk at most this often.
CONTACT_PERSIST_INTERVAL_S = 60.0


class LocalActionError(Exception):
    """A local API request cannot be carried out. ``code`` explains why."""

    def __init__(self, code: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reason(code: ReasonCode | None, message: str = "") -> common_pb2.Reason:
    return common_pb2.Reason(code=code.value if code else "", message=message)


class AgentCore:
    def __init__(
        self,
        settings: AgentSettings,
        identity: Identity,
        gateway: GatewayRunner,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        cuda_indexes: Callable[[], list[int]] = list,
        outbound: Outbound | None = None,
    ) -> None:
        self.settings = settings
        self.outbound = outbound or site_outbound(settings)
        # Set by the runtime when this agent can upgrade itself (an installed agent).
        self.upgrader: Upgrader | None = None
        # For the agent's metrics: operations by final state, artifact checks by result.
        self.operations: Counter[str] = Counter()
        self.artifact_checks: Counter[str] = Counter()
        self.identity = identity
        self.gateway = gateway
        self._clock = clock
        self._monotonic = monotonic
        # The CUDA GPUs this agent may place workers on (detected and allowed).
        self._cuda_indexes = cuda_indexes
        token = settings.artifact_source_token
        self.artifacts = ArtifactStore(
            settings.state_dir / "artifacts",
            settings.artifact_sources,
            token.get_secret_value() if token is not None else None,
            keys=load_keys(settings.artifact_keys),
        )
        if not settings.require_artifact_signatures:
            log.warning("artifact signatures are not required (development only)")
        self.store = StateStore(settings.state_dir / "state.json")
        self.audit = AuditLog(settings.state_dir / "audit.jsonl")
        self.gateway_dir = settings.state_dir / "gateway"
        self.pki = SitePki(settings.state_dir / "pki", identity.site_id)
        self.gateway_config_path: Path = self.gateway_dir / "site.yaml"
        self.state: AgentState = self.store.load()
        self.outbox: asyncio.Queue[site_pb2.AgentMessage] = asyncio.Queue(maxsize=OUTBOX_LIMIT)
        self._manifest_key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(identity.manifest_public_key_hex)
        )
        self.gateway_suspended = False
        self._nonces: list[bytes] = []
        self._contact_persisted_at: float | None = None
        # Offline time is state.offline_elapsed_s at this monotonic instant, plus what passed since.
        self._elapsed_mark = monotonic()
        self._accounted_at: float | None = None
        # Robot name -> the revision of its live session, from the gateway's last status.
        self._running: dict[str, str] = {}

    # --- helpers ------------------------------------------------------------------------------

    def _save(self) -> None:
        self.store.save(self.state)

    def _send(self, message: site_pb2.AgentMessage) -> None:
        if self.outbox.full():
            self.outbox.get_nowait()  # drop the oldest; operation reports are also persisted
        self.outbox.put_nowait(message)

    def manifest(self) -> dict[str, Any]:
        return self.state.manifest() or {"robots": [], "deployments": []}

    def robot_names(self) -> dict[str, str]:
        """control-plane robot id -> site robot name."""
        return {r["id"]: r["name"] for r in self.manifest().get("robots", [])}

    def note_contact(self) -> None:
        now = self._clock()
        self.state.last_contact_unix = now
        self.state.offline_elapsed_s = 0.0
        self._elapsed_mark = self._monotonic()
        if (
            self._contact_persisted_at is None
            or now - self._contact_persisted_at >= CONTACT_PERSIST_INTERVAL_S
        ):
            self._contact_persisted_at = now
            self._save()

    def contact_nonce(self) -> bytes:
        """A fresh challenge for the control plane to sign (sent with each status report)."""
        nonce = secrets.token_bytes(CONTACT_NONCE_BYTES)
        self._nonces = [*self._nonces, nonce][-OUTSTANDING_NONCES:]
        return nonce

    def on_contact_proof(self, proof: site_pb2.ContactProof) -> bool:
        """Renew offline authorization if the proof signs a nonce this agent issued."""
        if (
            proof.nonce not in self._nonces
            or proof.signature.key_id != self.identity.manifest_key_id
        ):
            return False
        try:
            self._manifest_key.verify(
                proof.signature.value, contact_proof_payload(self.identity.site_id, proof.nonce)
            )
        except InvalidSignature:
            log.warning("contact proof with an invalid signature ignored")
            return False
        self._nonces.remove(proof.nonce)
        self.note_contact()
        return True

    def offline_elapsed_s(self) -> float | None:
        """Time since the last verified contact: the wall clock's measure, but never less than
        the monotonic time the agent has itself counted since then, across restarts (design §13).
        A wall clock moved backwards therefore cannot extend offline authorization."""
        if self.state.last_contact_unix is None:
            return None
        counted = self.state.offline_elapsed_s + max(0.0, self._monotonic() - self._elapsed_mark)
        return max(self._clock() - self.state.last_contact_unix, counted)

    def _account_offline_time(self) -> None:
        """Fold the time counted so far into the persisted measure."""
        elapsed = self.offline_elapsed_s()
        if elapsed is not None:
            self.state.offline_elapsed_s = elapsed
            self._elapsed_mark = self._monotonic()

    def offline_remaining_s(self) -> float | None:
        manifest = self.state.manifest()
        elapsed = self.offline_elapsed_s()
        if manifest is None or elapsed is None:
            return None
        allowed = float(manifest.get("offline_authorization_hours", 0)) * 3600
        return max(0.0, allowed - elapsed)

    def authorization_expired(self) -> bool:
        remaining = self.offline_remaining_s()
        return remaining is not None and remaining <= 0

    def report(
        self,
        command_id: str,
        operation_id: str,
        state: str,
        *,
        reason: ReasonCode | None = None,
        message: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        """Report an operation's progress (persisted, and resent after a reconnect)."""
        self._report(command_id, operation_id, state, reason=reason, message=message, result=result)

    def _report(
        self,
        command_id: str,
        operation_id: str,
        state: str,
        *,
        reason: ReasonCode | None = None,
        message: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        report = {
            "command_id": command_id,
            "operation_id": operation_id,
            "state": state,
            "reason": reason.value if reason else "",
            "message": message,
            "result": result or {},
            "at": self._clock(),
        }
        self.state.reports[command_id] = report
        cutoff = self._clock() - 86_400
        self.state.reports = {k: v for k, v in self.state.reports.items() if v["at"] >= cutoff}
        self._save()
        self.audit.write("operation_report", **{k: v for k, v in report.items() if k != "at"})
        if state in ("Succeeded", "Failed", "Cancelled"):
            self.operations[state] += 1
        self._send(self.report_message(report))

    @staticmethod
    def report_message(report: dict[str, Any]) -> site_pb2.AgentMessage:
        reason = ReasonCode(report["reason"]) if report["reason"] else None
        return site_pb2.AgentMessage(
            operation=site_pb2.OperationReport(
                command_id=report["command_id"],
                operation_id=report["operation_id"],
                state=report["state"],
                reason=_reason(reason),
                message=report["message"],
                result_json=canonical_json(report["result"]) if report["result"] else b"",
            )
        )

    def pending_reports(self) -> list[site_pb2.AgentMessage]:
        """Reports to resend after a reconnect; the control plane ignores ones it has applied."""
        return [
            self.report_message(r)
            for r in sorted(self.state.reports.values(), key=lambda r: r["at"])
        ]

    def recover(self) -> None:
        """After a restart: fail operations that were running when the agent stopped. Commands run
        at most once, so an interrupted one is reported rather than resumed; activations awaiting
        approval or a task boundary are durable and carry on."""
        # An agent upgrade restarts the agent on purpose; the new (or restored) agent reports it.
        upgrading = UpgradeState.load(self.settings.state_dir)
        for command_id, report in sorted(self.state.reports.items(), key=lambda i: i[1]["at"]):
            if upgrading is not None and command_id == upgrading.command_id:
                continue
            if report["state"] == "Running" and command_id not in self.state.activations:
                self._report(
                    command_id,
                    report["operation_id"],
                    "Failed",
                    reason=ReasonCode.AGENT_OPERATION_INTERRUPTED,
                    message="the site agent restarted while this operation was running",
                )

    # --- desired state ------------------------------------------------------------------------

    async def on_desired_state(self, desired: site_pb2.DesiredState) -> site_pb2.AgentMessage:
        def ack(
            accepted: bool, code: ReasonCode | None = None, message: str = ""
        ) -> site_pb2.AgentMessage:
            self.audit.write(
                "desired_state",
                generation=int(desired.generation),
                accepted=accepted,
                reason=code.value if code else None,
                detail=message,
            )
            return site_pb2.AgentMessage(
                ack=site_pb2.Acknowledgement(
                    desired_state_generation=desired.generation,
                    accepted=accepted,
                    reason=_reason(code, message),
                )
            )

        if desired.signature.key_id != self.identity.manifest_key_id:
            return ack(False, ReasonCode.AGENT_MANIFEST_SIGNATURE_INVALID, "unknown signing key")
        try:
            self._manifest_key.verify(desired.signature.value, desired.manifest)
        except InvalidSignature:
            return ack(
                False, ReasonCode.AGENT_MANIFEST_SIGNATURE_INVALID, "signature does not verify"
            )
        try:
            manifest = json.loads(desired.manifest)
        except ValueError:
            return ack(False, ReasonCode.PROTOCOL_MESSAGE_INVALID, "manifest is not JSON")
        if (
            not isinstance(manifest, dict)
            or manifest.get("api_version") != MANIFEST_API_VERSION
            or manifest.get("site_id") != self.identity.site_id
            or manifest.get("generation") != desired.generation
        ):
            return ack(False, ReasonCode.PROTOCOL_MESSAGE_INVALID, "manifest is not for this site")
        if desired.generation < self.state.manifest_generation:
            return ack(
                False,
                ReasonCode.AGENT_GENERATION_STALE,
                f"committed {self.state.manifest_generation}",
            )
        if (
            desired.generation == self.state.manifest_generation
            and self.state.manifest_payload is not None
        ):
            return ack(True)

        self.state.manifest_payload = desired.manifest.decode("utf-8")
        self.state.manifest_signature = desired.signature.value.hex()
        self.state.manifest_generation = int(desired.generation)
        self._prune_to_manifest(manifest)
        # A newer signed generation proves contact as well; it can be committed only once, so a
        # replay of it extends nothing.
        self.note_contact()
        self._save()
        await self.reconcile()
        return ack(True)

    def _prune_to_manifest(self, manifest: dict[str, Any]) -> None:
        names = {r["name"] for r in manifest.get("robots", [])}
        live = {
            rev["id"] for d in manifest.get("deployments", []) for rev in d.get("revisions", [])
        }
        self.state.prepared = {k: v for k, v in self.state.prepared.items() if k in live}
        self.state.placements = {k: v for k, v in self.state.placements.items() if k in live}
        for name in list(self.state.assignments):
            assignment = self.state.assignments[name]
            if name not in names:
                del self.state.assignments[name]
                continue
            if assignment.current not in live:
                assignment.current = None
            if assignment.next not in live:
                assignment.next = None

    async def reconcile(self) -> None:
        """Bring the gateway in line with committed state (start, reload or keep it suspended)."""
        if self.state.manifest_payload is None:
            return
        config = self.gateway_config()
        changed = write_gateway_config(config, self.gateway_config_path)
        if self.gateway_suspended:
            return
        if not self.gateway.running:
            await self.gateway.start(self.gateway_config_path)
        elif changed:
            await self.gateway.reload()

    # --- commands -----------------------------------------------------------------------------

    async def on_command(self, command: site_pb2.Command) -> None:
        command_id = command.command_id
        kind = command.WhichOneof("kind") or ""
        ack = site_pb2.AgentMessage(
            ack=site_pb2.Acknowledgement(command_id=command_id, accepted=True)
        )
        if command_id in self.state.executed:
            self._send(ack)  # a redelivery: acknowledge again, never execute twice
            if command_id in self.state.reports:
                self._send(self.report_message(self.state.reports[command_id]))
            return
        self.state.mark_executed(command_id)
        self._save()
        self.audit.write(
            "command", command_id=command_id, kind=kind, operation_id=command.operation_id
        )
        self._send(ack)
        operation_id = command.operation_id
        if kind == "upgrade_agent" and command.upgrade_agent.server_id != self.identity.server_id:
            return  # for another server of this site, which reports it
        name = {
            "prepare": "prepare",
            "request_activation": "request_activation",
            "rollback": "rollback",
            "collect_diagnostics": "collect_diagnostics",
            "cancel": "cancel",
            "upgrade_agent": "upgrade_agent",
        }.get(kind)
        if name is None or name not in self.settings.allowed_operations:
            self._report(
                command_id,
                operation_id,
                "Failed",
                reason=ReasonCode.AGENT_BOUNDARY_DENIED,
                message=f"{kind or 'unknown'} is not allowed by this site's boundaries",
            )
            return
        if kind == "prepare":
            await self._prepare(command_id, operation_id, command.prepare.revision_id)
        elif kind == "request_activation":
            self._request_activation(
                command_id,
                operation_id,
                "request_activation",
                command.request_activation.revision_id,
                list(command.request_activation.robot_ids),
            )
        elif kind == "rollback":
            self._request_activation(
                command_id,
                operation_id,
                "rollback",
                command.rollback.to_revision_id,
                list(command.rollback.robot_ids),
            )
        elif kind == "cancel":
            await self._cancel(command.cancel.operation_id)
        elif kind == "collect_diagnostics":
            self._report(command_id, operation_id, "Succeeded", result=self.diagnostics())
        elif kind == "upgrade_agent":
            if self.upgrader is None:
                self._report(
                    command_id,
                    operation_id,
                    "Failed",
                    reason=ReasonCode.AGENT_UPGRADE_REFUSED,
                    message="this agent cannot upgrade itself",
                )
            else:
                self.upgrader.start(command_id, operation_id, command.upgrade_agent)

    async def _prepare(self, command_id: str, operation_id: str, revision_id: str) -> None:
        self._report(command_id, operation_id, "Running", message="verifying contracts")
        manifest = self.manifest()
        revision = next(
            (
                r
                for d in manifest.get("deployments", [])
                for r in d.get("revisions", [])
                if r["id"] == revision_id
            ),
            None,
        )
        if revision is None:
            self._report(
                command_id,
                operation_id,
                "Failed",
                reason=ReasonCode.DEPLOYMENT_NOT_ACTIVE,
                message="the revision is not in this site's committed desired state",
            )
            return
        parsed = {}
        for role, kind in (
            ("model_artifact", ModelArtifact),
            ("robot_profile", RobotProfile),
            ("execution_profile", ExecutionProfile),
        ):
            entry = revision["documents"][role]
            try:
                document = parse_document(entry["document"])
            except ContractError as exc:
                self._report(command_id, operation_id, "Failed", reason=exc.code, message=str(exc))
                return
            if not isinstance(document, kind) or document.digest() != entry["digest"]:
                self._report(
                    command_id,
                    operation_id,
                    "Failed",
                    reason=ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH,
                    message=f"{role} does not match its declared digest",
                )
                return
            parsed[role] = document
        expected_digest = content_digest(
            {
                "model_artifact": revision["documents"]["model_artifact"]["digest"],
                "robot_profile": revision["documents"]["robot_profile"]["digest"],
                "execution_profile": revision["documents"]["execution_profile"]["digest"],
            }
        )
        if expected_digest != revision["revision_digest"]:
            self._report(
                command_id,
                operation_id,
                "Failed",
                reason=ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH,
                message="revision digest does not match its documents",
            )
            return
        try:
            self._verify_publisher(revision["documents"]["model_artifact"])
        except signing.SignatureError as exc:
            self.artifact_checks[exc.code.value] += 1
            self.audit.write(
                "artifact.refused", revision=revision_id, reason=exc.code.value, detail=str(exc)
            )
            self._report(command_id, operation_id, "Failed", reason=exc.code, message=str(exc))
            return
        artifact, robot, profile = (
            parsed["model_artifact"],
            parsed["robot_profile"],
            parsed["execution_profile"],
        )
        assert isinstance(artifact, ModelArtifact)  # checked against each role's kind above
        assert isinstance(robot, RobotProfile)
        assert isinstance(profile, ExecutionProfile)
        try:
            binding = ExecutionBinding(artifact, robot, profile)
        except IncompatibleBindingError as exc:
            first = exc.report.issues[0]
            self._report(
                command_id,
                operation_id,
                "Failed",
                reason=first.code,
                message=f"incompatible at this site: {first.message}",
            )
            return
        serving = self.serving_config()
        estimate_args: dict[str, Any] = {}
        if self.settings.gateway_backend == "workers":
            try:
                placed = await self._place(
                    command_id, operation_id, revision_id, binding, revision["documents"]
                )
            except ArtifactError as exc:
                self._report(command_id, operation_id, "Failed", reason=exc.code, message=str(exc))
                return
            estimate_args = {
                "measured": load_profile_points(Path(placed["profile"])),
                "sharing_case": placed["sharing_case"],
                "device": placed["device"],
                "co_located_groups": placed["co_located_groups"],
            }
        self.state.prepared[revision_id] = revision["revision_digest"]
        self._save()
        await self.reconcile()
        self._report(
            command_id,
            operation_id,
            "Succeeded",
            result={
                "revision_digest": revision["revision_digest"],
                "capacity": capacity.estimate(
                    binding,
                    serving,
                    rtt_p99_ms=self.settings.capacity_rtt_p99_ms,
                    **estimate_args,
                ),
            },
        )

    def _verify_publisher(self, entry: dict[str, Any]) -> None:
        """Before anything of the artifact is pulled or run: a publisher this server trusts must
        have signed its digest (design §17.3, §17.4). Raises SignatureError."""
        if not self.settings.require_artifact_signatures:
            return
        try:
            trusted = trust.trusted_publishers(self.settings)
        except (OSError, signing.SignatureError) as exc:
            raise signing.SignatureError(
                f"the trusted publisher keys cannot be read: {exc}",
                ReasonCode.AGENT_ARTIFACT_UNTRUSTED,
            ) from exc
        signer = signing.trusted_signer(entry["digest"], entry.get("signatures", []), trusted)
        self.artifact_checks["verified"] += 1
        self.audit.write("artifact.verified", artifact=entry["digest"], publisher=signer)

    def _placed_groups(self, excluding: str) -> list[placement.PlacedGroup]:
        """Batch groups of the other prepared revisions, and their devices."""
        return [
            placement.PlacedGroup(p["group"], p["device"], p["adapter_name"])
            for revision_id, p in sorted(self.state.placements.items())
            if revision_id != excluding and revision_id in self.state.prepared
        ]

    async def _place(
        self,
        command_id: str,
        operation_id: str,
        revision_id: str,
        binding: ExecutionBinding,
        documents: dict[str, Any],
    ) -> dict[str, Any]:
        """Choose the device, materialize the weights, and measure the model there (design §13).
        Records the placement; raises ArtifactError with the reason prepare fails for."""
        others = self._placed_groups(revision_id)
        device = placement.choose_device(binding, others, self._cuda_indexes())
        adapter = self.settings.dev_adapter_override or binding.artifact.spec.adapter.name
        checkpoint: Path | None = None
        if adapter != "mock":
            self._report(command_id, operation_id, "Running", message="verifying model weights")
            checkpoint = await asyncio.to_thread(self.artifacts.checkout, binding.artifact)
        paths = write_revision_documents(self.gateway_dir, revision_id, documents)
        max_batch = self.settings.gateway_max_batch
        sandbox = sandbox_config(self.settings)
        image = binding.artifact.spec.runtime_image.reference
        if sandbox.kind == "container":
            # Pull, verify, then run (design §17.3): the image is fetched here, by its digest,
            # and never when a worker starts.
            run_image = sandbox.image_override or image
            self._report(command_id, operation_id, "Running", message=f"pulling {run_image}")
            try:
                await ensure_image(
                    sandbox.runtime,
                    run_image,
                    pull_timeout_s=self.settings.worker_image_pull_timeout_s,
                )
            except ImageUnavailableError as exc:
                raise ArtifactError(ReasonCode.WORKER_IMAGE_UNAVAILABLE, str(exc)) from exc
        profile = placement.profile_path(
            self.gateway_dir / "profiles", binding, device, max_batch, adapter
        )
        if not profile.is_file():
            self._report(
                command_id, operation_id, "Running", message=f"loading and profiling on {device}"
            )
            await placement.measure(
                launcher_for(sandbox, self.settings.worker_command, image),
                documents=paths,
                device=device,
                checkpoint=checkpoint,
                out=profile,
                max_batch=max_batch,
                iterations=self.settings.profile_iterations,
                timeout_s=self.settings.profile_timeout_s,
                adapter=self.settings.dev_adapter_override,
            )
        sharing = placement.sharing_case(binding, device, others)
        record = {
            "device": device,
            "group": placement.group_key(binding),
            "adapter_name": binding.artifact.spec.adapter.name,
            "adapter": self.settings.dev_adapter_override,
            "checkpoint": str(checkpoint) if checkpoint is not None else None,
            "profile": str(profile),
        }
        self.state.placements[revision_id] = record
        co_located = len({g.key for g in others if g.device == device} - {record["group"]})
        return {**record, "sharing_case": sharing, "co_located_groups": co_located}

    def _request_activation(
        self, command_id: str, operation_id: str, kind: str, revision_id: str, robot_ids: list[str]
    ) -> None:
        if self.authorization_expired():
            self._report(
                command_id,
                operation_id,
                "Failed",
                reason=ReasonCode.AGENT_AUTHORIZATION_EXPIRED,
                message="offline authorization expired; reconnect the site first",
            )
            return
        if revision_id not in self.state.prepared:
            self._report(
                command_id,
                operation_id,
                "Failed",
                reason=ReasonCode.DEPLOYMENT_NOT_ACTIVE,
                message="the revision is not prepared at this site",
            )
            return
        names = self.robot_names()
        unknown = [r for r in robot_ids if r not in names]
        if unknown or not robot_ids:
            self._report(
                command_id,
                operation_id,
                "Failed",
                reason=ReasonCode.PROTOCOL_MESSAGE_INVALID,
                message=f"robots {unknown} are not at this site",
            )
            return
        self.state.activations[command_id] = Activation(
            command_id=command_id,
            operation_id=operation_id,
            kind=kind,
            revision_id=revision_id,
            robot_names=[names[r] for r in robot_ids],
            requested_at=self._clock(),
        )
        self._save()
        self._report(
            command_id, operation_id, "Running", message="awaiting local approval at the site"
        )

    async def _cancel(self, operation_id: str) -> None:
        for command_id, activation in list(self.state.activations.items()):
            if activation.operation_id == operation_id:
                self._revert(activation)
                del self.state.activations[command_id]
                self._save()
                await self.reconcile()
                self._report(
                    command_id,
                    operation_id,
                    "Cancelled",
                    message="cancelled from the control plane",
                )

    def _revert(self, activation: Activation) -> None:
        for name in activation.robot_names:
            assignment = self.state.assignments.get(name)
            if assignment is not None and assignment.next == activation.revision_id:
                assignment.next = None

    # --- local approval -----------------------------------------------------------------------

    def pending_approvals(self) -> list[Activation]:
        return sorted(self.state.activations.values(), key=lambda a: a.requested_at)

    async def approve(self, command_id: str, operator: str) -> Activation:
        activation = self.state.activations.get(command_id)
        if activation is None:
            raise LocalActionError(ReasonCode.API_NOT_FOUND, "no such pending activation")
        if activation.approved_by is not None:
            return activation
        if self.authorization_expired():
            raise LocalActionError(
                ReasonCode.AGENT_AUTHORIZATION_EXPIRED, "offline authorization expired"
            )
        activation.approved_by = operator
        activation.approved_at = self._clock()
        for name in activation.robot_names:
            assignment = self.state.assignments.setdefault(name, Assignment())
            if assignment.next is not None and self._running.get(name) == assignment.next:
                # It already switched for an activation still in progress: that revision is the
                # one it runs, and the gateway keeps serving it until the robot switches again.
                assignment.current = assignment.next
            assignment.next = (
                None if assignment.current == activation.revision_id else activation.revision_id
            )
        superseded = self._supersede(activation)
        self._save()
        self.audit.write(
            "approval", command_id=command_id, operator=operator, revision_id=activation.revision_id
        )
        await self.reconcile()
        for older in superseded:
            self._report(
                older.command_id,
                older.operation_id,
                "Failed",
                reason=ReasonCode.AGENT_ACTIVATION_SUPERSEDED,
                message=f"superseded by the activation of {activation.revision_id}, approved by "
                f"{operator} before every robot switched",
            )
        self._report(
            command_id,
            activation.operation_id,
            "Running",
            message=f"approved by {operator}; robots switch at their next task boundary",
        )
        await self.watch_activations({})
        return activation

    def _supersede(self, activation: Activation) -> list[Activation]:
        """Approved activations that can no longer complete: another revision was just approved
        for some of their robots. They end without reverting anything."""
        robots = set(activation.robot_names)
        superseded = [
            a
            for a in self.state.activations.values()
            if a.command_id != activation.command_id
            and a.approved_at is not None
            and a.revision_id != activation.revision_id
            and robots & set(a.robot_names)
        ]
        for older in superseded:
            del self.state.activations[older.command_id]
        return superseded

    async def refuse(self, command_id: str, operator: str, note: str = "") -> None:
        activation = self.state.activations.pop(command_id, None)
        if activation is None:
            raise LocalActionError(ReasonCode.API_NOT_FOUND, "no such pending activation")
        self._revert(activation)
        self._save()
        await self.reconcile()
        self.audit.write("refusal", command_id=command_id, operator=operator, note=note)
        self._report(
            command_id,
            activation.operation_id,
            "Failed",
            reason=ReasonCode.AGENT_ACTIVATION_REFUSED,
            message=f"refused by {operator}" + (f": {note}" if note else ""),
            result={"refused": True, "refused_by": operator},
        )

    async def watch_activations(self, gateway_status: dict[str, Any] | None) -> None:
        """Complete approved activations once every robot runs the new revision; time out others."""
        running = {
            s["robot_id"]: s["revision_id"] for s in (gateway_status or {}).get("sessions", [])
        }
        if gateway_status:
            self._running = running
        ids = {name: rid for rid, name in self.robot_names().items()}
        for command_id, activation in list(self.state.activations.items()):
            if activation.approved_at is None:
                continue
            done = [
                n
                for n in activation.robot_names
                if running.get(n) == activation.revision_id
                or (
                    self.state.assignments.get(n, Assignment()).current == activation.revision_id
                    and self.state.assignments[n].next is None
                )
            ]
            if len(done) == len(activation.robot_names):
                for name in activation.robot_names:
                    assignment = self.state.assignments.setdefault(name, Assignment())
                    assignment.current, assignment.next = activation.revision_id, None
                del self.state.activations[command_id]
                self._save()
                await self.reconcile()
                self._report(
                    command_id,
                    activation.operation_id,
                    "Succeeded",
                    result={
                        "approved_by": activation.approved_by,
                        "revision_id": activation.revision_id,
                        "robot_ids": [ids[n] for n in activation.robot_names if n in ids],
                    },
                )
            elif self._clock() - activation.approved_at > self.settings.activation_timeout_s:
                self._revert(activation)
                del self.state.activations[command_id]
                self._save()
                await self.reconcile()
                missing = sorted(set(activation.robot_names) - set(done))
                self._report(
                    command_id,
                    activation.operation_id,
                    "Failed",
                    reason=ReasonCode.AGENT_ACTIVATION_TIMED_OUT,
                    message=f"robots {missing} did not switch in time",
                )

    # --- periodic work ------------------------------------------------------------------------

    async def enforce_authorization(self) -> None:
        """Stop serving when offline authorization runs out; resume when contact returns."""
        now = self._monotonic()
        if self._accounted_at is None or now - self._accounted_at >= CONTACT_PERSIST_INTERVAL_S:
            self._accounted_at = now
            self._account_offline_time()
            self._save()
        if self.authorization_expired():
            if not self.gateway_suspended:
                log.error("offline authorization expired: stopping the data plane")
                self.audit.write("authorization_expired")
                self.gateway_suspended = True
                await self.gateway.stop()
        elif self.gateway_suspended:
            self.gateway_suspended = False
            self.audit.write("authorization_restored")
            await self.reconcile()

    def status_message(self, gateway_status: dict[str, Any] | None) -> site_pb2.AgentMessage:
        sessions = []
        faults = []
        for s in (gateway_status or {}).get("sessions", []):
            sessions.append(
                site_pb2.RobotSessionStatus(
                    session_id=s["session_id"],
                    robot_name=s["robot_id"],
                    revision_id=s["revision_id"],
                    lease_generation=int(s["lease_generation"]),
                    task_epoch=int(s["task_epoch"]),
                    state=s.get("robot_state") or "Ready",
                    reason=common_pb2.Reason(code=s.get("fault_reason", "")),
                    metrics={
                        "chunks": float(s.get("chunks", 0)),
                        "observations": float(s.get("observations", 0)),
                        "rtt_p99_us": float(s.get("rtt_p99_us", 0)),
                        "dispatched_actions": float(s.get("dispatched_actions", 0)),
                        "queued_actions": float(s.get("queued_actions", 0)),
                    },
                )
            )
            if s.get("fault_reason"):
                faults.append(
                    site_pb2.FaultReport(
                        fault_key=f"{s['session_id']}:{s['task_epoch']}:{s['fault_reason']}",
                        robot_name=s["robot_id"],
                        session_id=s["session_id"],
                        revision_id=s["revision_id"],
                        reason=common_pb2.Reason(code=s["fault_reason"]),
                        latched=True,
                        occurred_at_unix_us=int(self._clock() * 1e6),
                    )
                )
        remaining = self.offline_remaining_s()
        return site_pb2.AgentMessage(
            status=site_pb2.SiteStatus(
                agent_time_us=time.monotonic_ns() // 1000,
                sessions=sessions,
                faults=faults,
                offline_authorization_remaining_s=int(remaining or 0),
                contact_nonce=self.contact_nonce(),
            )
        )

    def telemetry_message(
        self, gateway_status: dict[str, Any] | None, sequence: int
    ) -> site_pb2.AgentMessage:
        points = []
        for group in (gateway_status or {}).get("groups", {}).values():
            dispatched = max(1, int(group.get("dispatched_requests", 0)))
            points.append(
                site_pb2.MetricPoint(
                    name="deadline_miss_rate",
                    value=float(group.get("deadline_misses", 0)) / dispatched,
                    agent_time_us=time.monotonic_ns() // 1000,
                )
            )
        sessions = len((gateway_status or {}).get("sessions", []))
        gpus = len(self.settings.allowed_gpu_indexes or []) or 1
        points.append(
            site_pb2.MetricPoint(
                name="robots_per_gpu",
                value=sessions / gpus,
                agent_time_us=time.monotonic_ns() // 1000,
            )
        )
        return site_pb2.AgentMessage(
            telemetry=site_pb2.TelemetryBatch(schema_version="1", sequence=sequence, metrics=points)
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "manifest_generation": self.state.manifest_generation,
            "prepared": sorted(self.state.prepared),
            "assignments": {
                k: {"current": v.current, "next": v.next} for k, v in self.state.assignments.items()
            },
            "pending_activations": [a.command_id for a in self.pending_approvals()],
            "gateway_running": self.gateway.running,
            "gateway_suspended": self.gateway_suspended,
        }

    def serving_config(self) -> ServingConfig:
        """The serving settings this site's gateway runs with (and capacity is estimated for)."""
        serving = build_gateway_config(self.state, self.settings, self.gateway_dir)["serving"]
        return ServingConfig.model_validate(serving)

    # --- robot certificates (design §17.3) ----------------------------------------------------

    def gateway_config(self) -> dict[str, Any]:
        """The gateway's configuration, with mutual TLS from the site CA unless disabled."""
        if not self.settings.gateway_tls:
            return build_gateway_config(
                self.state, self.settings, self.gateway_dir, site_id=self.identity.site_id
            )
        names = sorted({*default_gateway_names(), *self.settings.gateway_tls_names})
        tls = self.pki.gateway_tls(self.gateway_dir / "tls", names)
        if self.settings.dev_robot_certificate_dir is not None:
            self._issue_dev_robot_certificates(self.settings.dev_robot_certificate_dir)
        return build_gateway_config(
            self.state, self.settings, self.gateway_dir, tls=tls, site_id=self.identity.site_id
        )

    def _robot_names(self) -> set[str]:
        return {r["name"] for r in self.manifest().get("robots", [])}

    def issue_robot_certificate(self, robot: str, request_pem: bytes) -> tuple[bytes, bytes]:
        """Sign a robot's CSR (the site operator asked for it). Returns (certificate, site CA)."""
        if robot not in self._robot_names():
            raise LocalActionError(ReasonCode.API_NOT_FOUND, f"{robot} is not a robot of this site")
        try:
            certificate = self.pki.issue_robot(robot, request_pem)
        except pki.CertificateError as exc:
            raise LocalActionError(ReasonCode.PROTOCOL_MESSAGE_INVALID, str(exc)) from exc
        self._record_robot_certificate(robot, certificate)
        self.audit.write(
            "robot_certificate_issued", robot=robot, sha256=pki.fingerprint(certificate)
        )
        return certificate, self.pki.ca_pem

    def revoke_robot_certificates(self, robot: str) -> int:
        """The gateway stops accepting the robot's certificates; its sessions end."""
        revoked = len(self.state.robot_certificates.pop(robot, []))
        self._save()
        self.audit.write("robot_certificates_revoked", robot=robot, count=revoked)
        return revoked

    def _record_robot_certificate(self, robot: str, certificate: bytes) -> None:
        records = [r for r in self.state.robot_certificates.get(robot, []) if unexpired([r])]
        records.append(site_pki.record(certificate))
        self.state.robot_certificates[robot] = records
        self._save()

    def _issue_dev_robot_certificates(self, directory: Path) -> None:
        """DEVELOPMENT ONLY: keys and certificates for simulated robots, written to a directory
        they read."""
        allowed = {
            name: set(unexpired(records)) for name, records in self.state.robot_certificates.items()
        }
        ca = directory / "ca.pem"
        if not ca.exists() or ca.read_bytes() != self.pki.ca_pem:
            atomic_write(ca, self.pki.ca_pem, 0o644)
        for robot in sorted(self._robot_names()):
            cert_path = directory / f"{robot}.pem"
            if cert_path.exists() and pki.fingerprint(cert_path.read_bytes()) in allowed.get(
                robot, set()
            ):
                continue
            key_pem, certificate = self.pki.issue_robot_bundle(robot)
            atomic_write(directory / f"{robot}.key", key_pem, 0o600)
            atomic_write(cert_path, certificate, 0o644)
            self._record_robot_certificate(robot, certificate)

    def assignment_for(self, robot_name: str) -> dict[str, Any] | None:
        """What a robot should run: its next revision during a switch, else its current one."""
        assignment = self.state.assignments.get(robot_name)
        revision_id = assignment.preferred() if assignment else None
        if revision_id is None:
            return None
        documents = revision_documents(self.manifest(), revision_id)
        if documents is None:
            return None
        return {
            "revision_id": revision_id,
            "documents": {role: entry["document"] for role, entry in documents.items()},
        }
