"""Runs the agent: waits for enrollment, then keeps the control connection, status reporting,
activation watching and authorization enforcement going."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from neura.v1 import common_pb2, site_pb2, site_pb2_grpc
from neura_agent.connection import ControlConnection, agent_version, open_channel
from neura_agent.core import AgentCore
from neura_agent.gateway import GatewayRunner, SubprocessGateway
from neura_agent.identity import Identity, IdentityStore
from neura_agent.preflight import Runner, collect_facts, run_command
from neura_agent.settings import AgentSettings
from neura_contracts import ReasonCode

log = logging.getLogger("neura.agent")


class EnrollmentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class AgentRuntime:
    def __init__(
        self,
        settings: AgentSettings,
        *,
        gateway: GatewayRunner | None = None,
        command_runner: Runner = run_command,
    ) -> None:
        self.settings = settings
        self.identities = IdentityStore(settings.state_dir)
        self.gateway: GatewayRunner = gateway or SubprocessGateway(
            settings.gateway_command, settings.gateway_status_listen or "127.0.0.1:8091"
        )
        self._runner = command_runner
        self.core: AgentCore | None = None
        self.connection: ControlConnection | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.last_gateway_status: dict[str, object] | None = None

    def facts(self) -> site_pb2.ServerFacts:
        return collect_facts(
            state_dir=self.settings.state_dir,
            require_gpu=self.settings.require_gpu,
            min_free_disk_mib=self.settings.min_free_disk_mib,
            allowed_gpu_indexes=self.settings.allowed_gpu_indexes,
            runner=self._runner,
        )

    def cuda_indexes(self) -> list[int]:
        """GPUs present on this server that the agent may use (design §17.4)."""
        present = [g.index for g in self.facts().gpus]
        allowed = self.settings.allowed_gpu_indexes
        return [i for i in present if allowed is None or i in allowed]

    async def enroll(self, endpoint: str, token: str) -> Identity:
        """Enroll this server: generate the key here, send only the public key (design §17.2)."""
        if self.identities.load() is not None:
            raise EnrollmentError(
                "ALREADY_ENROLLED", f"identity exists in {self.settings.state_dir}"
            )
        facts = self.facts()
        failed = [c for c in facts.preflight if not c.passed]
        if failed:
            details = "; ".join(f"{c.name}: {c.detail} (fix: {c.fix})" for c in failed)
            raise EnrollmentError(ReasonCode.AGENT_PREFLIGHT_FAILED.value, details)
        key = self.identities.load_or_create_key()
        async with open_channel(endpoint, self.settings) as channel:
            response = await site_pb2_grpc.SiteControlStub(channel).Enroll(
                site_pb2.EnrollRequest(
                    protocol_version=common_pb2.PROTOCOL_VERSION_1,
                    enrollment_token=token,
                    public_key=IdentityStore.public_key_raw(key),
                    agent_version=agent_version(),
                    facts=facts,
                ),
                timeout=15,
            )
        if response.WhichOneof("result") != "enrolled":
            raise EnrollmentError(
                response.rejected.code, response.rejected.message or "enrollment refused"
            )
        enrolled = response.enrolled
        identity = Identity(
            server_id=enrolled.server_id,
            site_id=enrolled.site_id,
            project_id=enrolled.project_id,
            endpoint=endpoint,
            manifest_public_key_hex=enrolled.manifest_public_key.hex(),
            manifest_key_id=enrolled.manifest_key_id,
        )
        self.identities.save(identity)
        return identity

    async def start(self) -> None:
        self._stop.clear()
        self._tasks.append(asyncio.create_task(self._main(), name="agent-main"))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        await self.gateway.stop()

    async def _main(self) -> None:
        identity = self.identities.load()
        while identity is None and not self._stop.is_set():
            await asyncio.sleep(1.0)
            identity = self.identities.load()
        if identity is None:
            return
        key = self.identities.load_or_create_key()
        self.core = AgentCore(self.settings, identity, self.gateway, cuda_indexes=self.cuda_indexes)
        self.core.recover()
        await self.core.reconcile()  # resume serving committed state immediately, cloud or not
        self.connection = ControlConnection(
            self.core, key, self.facts, max_backoff_s=self.settings.reconnect_max_backoff_s
        )
        await asyncio.gather(
            self.connection.run(self._stop),
            self._periodic(self.settings.status_interval_s, self._report_status),
            self._periodic(1.0, self._watch),
        )

    async def _periodic(self, interval: float, work: Callable[[], object]) -> None:
        while not self._stop.is_set():
            try:
                result = work()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("periodic agent task failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    async def _watch(self) -> None:
        assert self.core is not None
        await self.core.enforce_authorization()
        self.last_gateway_status = await self.gateway.status()
        await self.core.watch_activations(self.last_gateway_status)

    _telemetry_sequence = 0

    async def _report_status(self) -> None:
        if self.core is None or self.connection is None or not self.connection.connected:
            return
        status = await self.gateway.status()
        self.core.outbox.put_nowait(self.core.status_message(status))
        self._telemetry_sequence += 1
        self.core.outbox.put_nowait(self.core.telemetry_message(status, self._telemetry_sequence))
