"""The control plane's side of the site-agent protocol (design §17.2-§17.3, §20.8).

Agents dial out; nothing here connects to a site. ``Enroll`` turns a single-use token into a server
identity bound to an on-host key. ``Connect`` is the long-lived stream: signed desired state and
commands go down, acknowledgements, operation reports, status and telemetry come up. Every report
is applied through ``services.site_reports``, the only writer of observed state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, TypeVar

import grpc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neura.v1 import common_pb2, site_pb2, site_pb2_grpc
from neura_contracts import ReasonCode
from neura_contracts.site_protocol import CONTACT_NONCE_BYTES, contact_proof_payload
from neura_control.clock import iso, utcnow
from neura_control.db.models import (
    EnrollmentToken,
    Fault,
    Operation,
    Project,
    Robot,
    Server,
    Site,
    SiteCommand,
)
from neura_control.ids import Kind, new_id
from neura_control.security import agent_tokens
from neura_control.security.principal import Principal, PrincipalKind
from neura_control.security.signing import ManifestSigner
from neura_control.security.tokens import ENROLLMENT_TOKEN_PREFIX, parse_token, verify_secret
from neura_control.services import records, site_reports
from neura_control.services.manifests import current_manifest
from neura_control.settings import ControlPlaneSettings

T = TypeVar("T")
log = logging.getLogger("neura.control.sites")

# Only these metrics are accepted from sites: the published telemetry schema (design §18.2).
TELEMETRY_METRICS = frozenset(
    {
        "deadline_miss_rate",
        "observation_age_p99_ms",
        "starvation_events",
        "robots_per_gpu",
        "gpu_utilization",
        "gpu_hours_per_robot_hour",
    }
)
RUNTIME_PHASE = {
    "Running": "Running",
    "Waiting": "Waiting",
    "Faulted": "NeedsAttention",
    "Ready": "Ready",
    "Armed": "Ready",
    "Disconnected": "Offline",
}
SESSION_STATES = frozenset({"Disconnected", "Ready", "Armed", "Running", "Waiting", "Faulted"})


def _reason(code: ReasonCode, message: str = "") -> common_pb2.Reason:
    return common_pb2.Reason(code=code.value, message=message)


def _code(reason: common_pb2.Reason) -> ReasonCode | None:
    if not reason.code:
        return None
    try:
        return ReasonCode(reason.code)
    except ValueError:
        return None


class SiteControlService(site_pb2_grpc.SiteControlServicer):  # type: ignore[misc]
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        signer: ManifestSigner,
        settings: ControlPlaneSettings,
    ) -> None:
        self._sessions = sessions
        self._signer = signer
        self._settings = settings
        self.connected: dict[str, int] = {}  # server id -> open streams
        # Database work in progress. An RPC cancelled midway (a stream ending, shutdown) must not
        # abandon a transaction: SQLAlchemy cannot return a connection cancelled mid-operation to
        # its pool, so it would leak. Work runs shielded and close() waits for it.
        self._inflight: set[asyncio.Future[Any]] = set()
        # Open streams. A cancelled stream still runs its cleanup (recording the disconnect)
        # after the gRPC server's stop() returns, so close() waits for streams to finish first.
        self._streams = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def _shielded(self, work: Coroutine[Any, Any, T]) -> T:
        task = asyncio.ensure_future(work)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return await asyncio.shield(task)

    async def close(self, timeout: float = 5.0) -> None:
        """Wait for streams to finish and for database work started by RPCs. Call after the
        gRPC server stopped, before the database engine is disposed."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout)
        if self._inflight:
            await asyncio.wait(set(self._inflight), timeout=timeout)

    # --- enrollment ---------------------------------------------------------------------------

    async def Enroll(  # noqa: N802 - gRPC method name
        self, request: site_pb2.EnrollRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> site_pb2.EnrollResponse:
        def reject(code: ReasonCode, message: str = "") -> site_pb2.EnrollResponse:
            return site_pb2.EnrollResponse(rejected=_reason(code, message))

        if request.protocol_version != common_pb2.PROTOCOL_VERSION_1:
            return reject(ReasonCode.PROTOCOL_VERSION_MISMATCH)
        if len(request.public_key) != 32:
            return reject(
                ReasonCode.PROTOCOL_MESSAGE_INVALID, "public_key must be a raw Ed25519 key"
            )
        parsed = parse_token(request.enrollment_token)
        if parsed is None or parsed.prefix != ENROLLMENT_TOKEN_PREFIX:
            return reject(ReasonCode.AGENT_ENROLLMENT_INVALID)
        return await self._shielded(self._enroll(request, parsed.resource_id, parsed.secret))

    async def _enroll(
        self, request: site_pb2.EnrollRequest, token_id: str, secret: str
    ) -> site_pb2.EnrollResponse:
        async with self._sessions.begin() as session:
            token = await session.get(EnrollmentToken, token_id, with_for_update=True)
            now = utcnow()
            if (
                token is None
                or token.used_at is not None
                or token.expires_at <= now
                or not verify_secret(secret, token.secret_hash)
            ):
                return site_pb2.EnrollResponse(
                    rejected=_reason(ReasonCode.AGENT_ENROLLMENT_INVALID)
                )
            site = await session.get(Site, token.site_id)
            assert site is not None
            token.used_at = now
            facts = request.facts
            server = Server(
                id=new_id(Kind.SERVER),
                project_id=site.project_id,
                site_id=site.id,
                display_name=(facts.hostname or "server")[:200],
                labels={},
                allocated_gpu_indexes=[],
                observed=_facts_observed(facts),
                agent_public_key=request.public_key.hex(),
                created_by=f"enrollment:{token.id}",
            )
            session.add(server)
            await session.flush()
            principal = Principal(subject=f"site_agent:{server.id}", kind=PrincipalKind.SITE_AGENT)
            organization_id = await _organization_of(session, site.project_id)
            await records.audit(
                session,
                principal=principal,
                organization_id=organization_id,
                project_id=site.project_id,
                action="server.enroll",
                target=server.id,
                request_id=f"enroll:{token.id}",
            )
            await records.event(
                session,
                project_id=site.project_id,
                type="resource.created",
                resource=server.id,
                version=1,
            )
            return site_pb2.EnrollResponse(
                enrolled=site_pb2.Enrolled(
                    server_id=server.id,
                    site_id=site.id,
                    project_id=site.project_id,
                    manifest_public_key=self._signer.public_key,
                    manifest_key_id=self._signer.key_id,
                )
            )

    # --- connection ---------------------------------------------------------------------------

    async def _authenticate(self, context: grpc.aio.ServicerContext[Any, Any]) -> Server:
        metadata = dict(context.invocation_metadata() or ())
        header = str(metadata.get("authorization", ""))
        token = header.removeprefix("Bearer ").strip()
        try:
            server_id = agent_tokens.unverified_server_id(token)
            server = await self._shielded(self._server(server_id))
            if server is None:
                raise agent_tokens.AgentTokenError("unknown server")
            agent_tokens.verify(
                token, server.agent_public_key, max_age_s=self._settings.agent_token_max_age_s
            )
        except agent_tokens.AgentTokenError:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED, ReasonCode.AGENT_UNAUTHENTICATED.value
            )
            raise AssertionError("unreachable") from None
        return server

    async def _server(self, server_id: str) -> Server | None:
        async with self._sessions.begin() as session:
            return await session.get(Server, server_id)

    async def Connect(  # noqa: N802 - gRPC method name
        self,
        request_iterator: AsyncIterator[site_pb2.AgentMessage],
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> AsyncIterator[site_pb2.ControlPlaneMessage]:
        self._streams += 1
        self._idle.clear()
        try:
            server = await self._authenticate(context)
            server_id, site_id = server.id, server.site_id
            self.connected[server_id] = self.connected.get(server_id, 0) + 1
            agent_version: dict[str, str | None] = {"value": None}
            # Contact challenges from the agent's status reports, answered by the send loop below
            # only after it has checked that the server is still enrolled.
            nonces: list[bytes] = []
            wake = asyncio.Event()

            def challenge(nonce: bytes) -> None:
                if len(nonce) == CONTACT_NONCE_BYTES:
                    nonces[:] = [*nonces, nonce][-8:]
                    wake.set()

            reader = asyncio.create_task(
                self._read(server_id, site_id, request_iterator, agent_version, challenge)
            )
            reader.add_done_callback(lambda _: wake.set())
            sent_generation = -1
            sent_commands: set[str] = set()
            try:
                while not reader.done():
                    wake.clear()
                    pending, nonces[:] = list(nonces), []
                    polled = await self._shielded(
                        self._poll(server_id, site_id, pending, sent_generation, sent_commands)
                    )
                    if polled is None:
                        break  # unenrolled while connected
                    outgoing, sent_generation = polled
                    for message in outgoing:
                        yield message
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            wake.wait(), timeout=self._settings.agent_poll_interval_s
                        )
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
                self.connected[server_id] -= 1
                if self.connected[server_id] <= 0:
                    del self.connected[server_id]
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._shielded(self._disconnected(site_id, agent_version["value"]))
        finally:
            self._streams -= 1
            if self._streams == 0:
                self._idle.set()

    async def _poll(
        self,
        server_id: str,
        site_id: str,
        nonces: list[bytes],
        sent_generation: int,
        sent_commands: set[str],
    ) -> tuple[list[site_pb2.ControlPlaneMessage], int] | None:
        """What to send next: contact proofs, desired state if its generation moved, and commands
        not yet sent. None when the server is no longer enrolled."""
        outgoing: list[site_pb2.ControlPlaneMessage] = []
        async with self._sessions.begin() as session:
            if await session.get(Server, server_id) is None:
                return None
            outgoing.extend(self._contact_proof(site_id, nonce) for nonce in nonces)
            manifest = await current_manifest(session, site_id, self._signer)
            if manifest.generation != sent_generation:
                outgoing.append(
                    site_pb2.ControlPlaneMessage(
                        desired_state=site_pb2.DesiredState(
                            generation=manifest.generation,
                            manifest=manifest.payload,
                            signature=site_pb2.Signature(
                                key_id=manifest.key_id, value=manifest.signature
                            ),
                        )
                    )
                )
            commands = (
                await session.scalars(
                    select(SiteCommand)
                    .where(SiteCommand.site_id == site_id, SiteCommand.acknowledged_at.is_(None))
                    .order_by(SiteCommand.id)
                )
            ).all()
            for command in commands:
                if command.command_id in sent_commands:
                    continue
                outgoing.append(site_pb2.ControlPlaneMessage(command=_command_message(command)))
                sent_commands.add(command.command_id)
                command.delivered_at = command.delivered_at or utcnow()
        return outgoing, manifest.generation

    def _contact_proof(self, site_id: str, nonce: bytes) -> site_pb2.ControlPlaneMessage:
        signature = self._signer.sign(contact_proof_payload(site_id, nonce))
        return site_pb2.ControlPlaneMessage(
            contact=site_pb2.ContactProof(
                nonce=nonce,
                signature=site_pb2.Signature(key_id=self._signer.key_id, value=signature),
            )
        )

    async def _disconnected(self, site_id: str, agent_version: str | None) -> None:
        try:
            async with self._sessions.begin() as session:
                await site_reports.report_site(
                    session, site_id, connected=False, agent_version=agent_version
                )
        except Exception:
            log.exception("failed to record that site %s disconnected", site_id)

    async def _read(
        self,
        server_id: str,
        site_id: str,
        request_iterator: AsyncIterator[site_pb2.AgentMessage],
        agent_version: dict[str, str | None],
        challenge: Callable[[bytes], None],
    ) -> None:
        async for message in request_iterator:
            kind = message.WhichOneof("body")
            if kind == "status" and message.status.contact_nonce:
                challenge(message.status.contact_nonce)
            try:
                await self._shielded(self._apply(server_id, site_id, message, agent_version))
            except Exception:
                # One bad report must not end the stream; it is logged and the next one applied.
                log.exception("failed to apply %s from server %s", kind, server_id)

    async def _apply(
        self,
        server_id: str,
        site_id: str,
        message: site_pb2.AgentMessage,
        agent_version: dict[str, str | None],
    ) -> None:
        kind = message.WhichOneof("body")
        async with self._sessions.begin() as session:
            if kind == "hello":
                agent_version["value"] = message.hello.agent_version[:64]
                await site_reports.report_site(
                    session, site_id, connected=True, agent_version=agent_version["value"]
                )
            elif kind == "ack":
                await self._ack(session, site_id, message.ack)
            elif kind == "operation":
                await self._operation(session, site_id, message.operation)
            elif kind == "facts":
                await site_reports.report_server(session, server_id, _facts_observed(message.facts))
            elif kind == "status":
                await self._status(session, site_id, message.status)
            elif kind == "telemetry":
                await self._telemetry(session, site_id, message.telemetry)

    async def _ack(
        self, session: AsyncSession, site_id: str, ack: site_pb2.Acknowledgement
    ) -> None:
        if ack.WhichOneof("subject") == "desired_state_generation":
            await site_reports.report_site_ack(
                session,
                site_id,
                generation=int(ack.desired_state_generation),
                accepted=bool(ack.accepted),
                reason=ack.reason.code or None,
            )
        elif ack.WhichOneof("subject") == "command_id":
            command = (
                await session.scalars(
                    select(SiteCommand).where(
                        SiteCommand.command_id == ack.command_id, SiteCommand.site_id == site_id
                    )
                )
            ).first()
            if command is not None and command.acknowledged_at is None:
                command.acknowledged_at = utcnow()

    async def _operation(
        self, session: AsyncSession, site_id: str, report: site_pb2.OperationReport
    ) -> None:
        operation = await session.get(Operation, report.operation_id)
        if operation is None or operation.site_id != site_id:
            log.warning(
                "site %s reported an operation it does not own: %s", site_id, report.operation_id
            )
            return
        if report.state not in ("Running", "Succeeded", "Failed", "Cancelled"):
            return
        result: dict[str, Any] | None = None
        if report.result_json:
            loaded = json.loads(report.result_json)
            result = loaded if isinstance(loaded, dict) else None
        await site_reports.update_operation(
            session,
            operation.id,
            report.state,
            reason=_code(report.reason),
            message=report.message[:2000],
            result=result,
        )

    async def _status(
        self, session: AsyncSession, site_id: str, status: site_pb2.SiteStatus
    ) -> None:
        await site_reports.report_site_heartbeat(
            session,
            site_id,
            offline_authorization_remaining_s=int(status.offline_authorization_remaining_s),
        )
        robots = {
            r.name: r
            for r in (await session.scalars(select(Robot).where(Robot.site_id == site_id))).all()
        }
        seen: set[str] = set()
        for item in status.sessions:
            robot = robots.get(item.robot_name)
            if robot is None or not item.session_id.startswith("ses_"):
                continue
            seen.add(robot.name)
            state = item.state if item.state in SESSION_STATES else "Ready"
            await site_reports.upsert_session(
                session,
                session_id=item.session_id,
                robot_id=robot.id,
                revision_id=item.revision_id,
                lease_generation=int(item.lease_generation),
                task_epoch=int(item.task_epoch),
                state=state,
                reason=item.reason.code or None,
                metrics={k: float(v) for k, v in item.metrics.items()},
            )
            await site_reports.report_robot(
                session,
                robot.id,
                phase=RUNTIME_PHASE.get(state, "Unknown"),
                reason=item.reason.code or None,
                lease_holder_session_id=item.session_id,
                task_epoch=int(item.task_epoch),
                active_revision_id=item.revision_id or robot.observed.get("active_revision_id"),
            )
        for name, robot in robots.items():
            if name not in seen and robot.observed.get("phase") not in (None, "Unknown", "Offline"):
                await site_reports.report_robot(
                    session, robot.id, phase="Offline", reason=None, lease_holder_session_id=None
                )
        for fault in status.faults:
            robot = robots.get(fault.robot_name)
            if robot is None or not fault.fault_key:
                continue
            exists = await session.scalar(
                select(Fault.id).where(
                    Fault.project_id == robot.project_id,
                    Fault.site_fault_key == fault.fault_key[:128],
                )
            )
            if exists:
                continue
            occurred = (
                datetime.fromtimestamp(fault.occurred_at_unix_us / 1e6, UTC)
                if fault.occurred_at_unix_us
                else utcnow()
            )
            created = await site_reports.raise_fault(
                session,
                robot_id=robot.id,
                reason=fault.reason.code or ReasonCode.PROTOCOL_MESSAGE_INVALID.value,
                message=fault.reason.message[:2000],
                latched=fault.latched,
                local_response=fault.local_response or None,
                session_id=fault.session_id or None,
                revision_id=fault.revision_id or None,
                occurred_at=occurred,
            )
            created.site_fault_key = fault.fault_key[:128]

    async def _telemetry(
        self, session: AsyncSession, site_id: str, batch: site_pb2.TelemetryBatch
    ) -> None:
        site = await session.get(Site, site_id)
        if site is None:
            return
        robots = {
            r.name: r.id
            for r in (await session.scalars(select(Robot).where(Robot.site_id == site_id))).all()
        }
        now = utcnow()
        points = []
        for point in batch.metrics:
            if point.name not in TELEMETRY_METRICS:
                continue  # outside the published schema: never stored
            robot_name = point.labels.get("robot")
            points.append(
                {
                    "metric": point.name,
                    "value": point.value,
                    "ts": now,
                    "robot_id": robots.get(robot_name) if robot_name else None,
                    "revision_id": point.labels.get("revision"),
                }
            )
        await site_reports.record_metrics(
            session, project_id=site.project_id, site_id=site_id, points=points
        )


def _facts_observed(facts: site_pb2.ServerFacts) -> dict[str, Any]:
    return {
        "phase": "Ready" if all(p.passed for p in facts.preflight) else "NeedsAttention",
        "hostname": facts.hostname[:200],
        "driver_version": facts.driver_version or None,
        "cuda_version": facts.cuda_version or None,
        "gpus": [
            {
                "index": g.index,
                "uuid": g.uuid,
                "model": g.model,
                "memory_mib": g.memory_mib,
                "mig_enabled": g.mig_enabled,
            }
            for g in facts.gpus
        ],
        "preflight": [
            {
                "name": p.name,
                "passed": p.passed,
                "reason": p.reason or None,
                "detail": p.detail,
                "fix": p.fix,
            }
            for p in facts.preflight
        ],
        "observed_at": iso(utcnow()),
    }


def _command_message(command: SiteCommand) -> site_pb2.Command:
    data = command.command
    kind = data.get("kind")
    message = site_pb2.Command(
        command_id=command.command_id, operation_id=command.operation_id or ""
    )
    if kind == "prepare":
        message.prepare.CopyFrom(site_pb2.PrepareRevision(revision_id=data["revision_id"]))
    elif kind == "request_activation":
        message.request_activation.CopyFrom(
            site_pb2.RequestActivation(revision_id=data["revision_id"], robot_ids=data["robot_ids"])
        )
    elif kind == "rollback":
        message.rollback.CopyFrom(
            site_pb2.Rollback(to_revision_id=data["to_revision_id"], robot_ids=data["robot_ids"])
        )
    elif kind == "cancel":
        message.cancel.CopyFrom(site_pb2.CancelOperation(operation_id=data["operation_id"]))
    else:
        message.collect_diagnostics.CopyFrom(
            site_pb2.CollectDiagnostics(scope=str(data.get("scope", "")))
        )
    return message


async def _organization_of(session: AsyncSession, project_id: str) -> str:
    project = await session.get(Project, project_id)
    assert project is not None
    return project.organization_id


async def start_agent_server(
    sessions: async_sessionmaker[AsyncSession],
    signer: ManifestSigner,
    settings: ControlPlaneSettings,
) -> tuple[grpc.aio.Server, SiteControlService, int]:
    assert settings.agent_grpc_listen is not None
    server = grpc.aio.server(
        options=[
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
            ("grpc.max_receive_message_length", 8 * 1024 * 1024),
        ]
    )
    service = SiteControlService(sessions, signer, settings)
    site_pb2_grpc.add_SiteControlServicer_to_server(service, server)
    if settings.agent_tls_cert_file is not None and settings.agent_tls_key_file is not None:
        credentials = grpc.ssl_server_credentials(
            [(settings.agent_tls_key_file.read_bytes(), settings.agent_tls_cert_file.read_bytes())]
        )
        port = server.add_secure_port(settings.agent_grpc_listen, credentials)
    else:
        port = server.add_insecure_port(settings.agent_grpc_listen)
    await server.start()
    log.info("site-agent endpoint listening on %s", settings.agent_grpc_listen)
    return server, service, port
