"""The site gateway core (design §6, §8, §11, §12).

Sans-IO: adapters authenticate the robot, measure message sizes, read the gateway clock and move
messages; this class makes every decision. Each method returns the messages to deliver as
``Delivery`` items addressed to sessions.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura.v1 import common_pb2, dataplane_pb2
from neura_contracts import ReasonCode
from neura_runtime import tensors
from neura_runtime.binding import ExecutionBinding
from neura_server.admission import AdmissionController, SessionDemand
from neura_server.inputs import InvalidObservation, decode_observation
from neura_server.leases import Lease, LeaseDenied, LeaseTable
from neura_server.policy import PolicyError
from neura_server.scheduler import Batch, Dropped, InferenceRequest, Scheduler

MAX_REPORTED_RTT_US = 10_000_000


@dataclass(frozen=True, slots=True)
class Deployment:
    revision_id: str
    binding: ExecutionBinding
    robots: frozenset[str]

    @property
    def group_key(self) -> str:
        """Requests share a forward pass only within the same artifact, device and precision."""
        runtime = self.binding.profile.spec.runtime
        return f"{self.binding.artifact_digest}:{runtime.device.value}:{runtime.precision.value}"


@dataclass(slots=True)
class BatchGroup:
    key: str
    scheduler: Scheduler
    admission: AdmissionController


@dataclass(slots=True)
class Session:
    lease: Lease
    deployment: Deployment
    group: BatchGroup
    task_epoch: int = 0
    rtt_p99_us: int = 0
    rtt_recent_us: int = 0
    observations: int = 0
    chunks: int = 0
    rejections: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True, slots=True)
class Delivery:
    session_id: str
    message: dataplane_pb2.GatewayMessage


def _reason(code: ReasonCode, message: str = "") -> common_pb2.Reason:
    return common_pb2.Reason(code=code.value, message=message)


class GatewayCore:
    def __init__(
        self,
        *,
        deployments: list[Deployment],
        groups: dict[str, BatchGroup],
        leases: LeaseTable,
        max_message_bytes: int = 16 * 1024 * 1024,
        default_rtt_us: int = 20_000,
    ) -> None:
        self._deployments_by_robot: dict[str, Deployment] = {}
        for deployment in deployments:
            if deployment.group_key not in groups:
                raise ValueError(f"no batch group for {deployment.group_key}")
            for robot in deployment.robots:
                if robot in self._deployments_by_robot:
                    raise ValueError(f"robot {robot} is in more than one deployment")
                self._deployments_by_robot[robot] = deployment
        self.groups = groups
        self.leases = leases
        self.max_message_bytes = max_message_bytes
        self.default_rtt_us = default_rtt_us
        self.sessions: dict[str, Session] = {}
        self.counters: Counter[str] = Counter()

    # --- sessions -----------------------------------------------------------------------------

    def open_session(
        self, robot_id: str, request: dataplane_pb2.OpenSessionRequest, now_us: int
    ) -> dataplane_pb2.OpenSessionResponse:
        def reject(code: ReasonCode, message: str = "") -> dataplane_pb2.OpenSessionResponse:
            self.counters[code.value] += 1
            return dataplane_pb2.OpenSessionResponse(rejected=_reason(code, message))

        if request.protocol_version != common_pb2.PROTOCOL_VERSION_1:
            return reject(ReasonCode.PROTOCOL_VERSION_MISMATCH)
        if request.robot_id and request.robot_id != robot_id:
            return reject(ReasonCode.AUTH_DEVICE_UNKNOWN, "robot_id differs from the credential")
        deployment = self._deployments_by_robot.get(robot_id)
        if deployment is None:
            return reject(ReasonCode.DEPLOYMENT_NOT_ACTIVE, "no deployment includes this robot")
        binding = deployment.binding
        if (
            request.revision_id != deployment.revision_id
            or request.artifact_digest != binding.artifact_digest
            or request.execution_profile_digest != binding.execution_profile_digest
            or request.robot_profile_digest != binding.robot_profile_digest
        ):
            return reject(
                ReasonCode.DEPLOYMENT_NOT_ACTIVE,
                f"active revision is {deployment.revision_id} with different contract digests",
            )

        granted = self.leases.acquire(robot_id, now_us)
        if isinstance(granted, LeaseDenied):
            # Clean up a previous session of ours the table no longer knows about.
            return reject(granted.reason, f"held by {granted.holder_session_id}")
        for stale_id in [s for s, info in self.sessions.items() if info.lease.robot_id == robot_id]:
            self._drop_session(stale_id, ReasonCode.LEASE_EXPIRED)

        group = self.groups[deployment.group_key]
        demand = SessionDemand.from_binding(
            granted.session_id, binding, rtt_p99_us=self.default_rtt_us
        )
        decision = group.admission.try_admit(demand)
        if not decision.admitted:
            self.leases.release(granted.session_id)
            assert decision.reason is not None
            return reject(decision.reason, decision.detail)

        self.sessions[granted.session_id] = Session(
            lease=granted, deployment=deployment, group=group
        )
        self.counters["sessions_opened"] += 1
        return dataplane_pb2.OpenSessionResponse(
            accepted=dataplane_pb2.SessionAccepted(
                session_id=granted.session_id,
                lease_generation=granted.generation,
                lease_ttl_us=self.leases.ttl_us,
                max_message_bytes=self.max_message_bytes,
            )
        )

    def heartbeat(
        self, robot_id: str, request: dataplane_pb2.HeartbeatRequest, now_us: int
    ) -> dataplane_pb2.HeartbeatResponse:
        session = self._authorized(robot_id, request.session_id, request.lease_generation, now_us)
        if isinstance(session, ReasonCode):
            return dataplane_pb2.HeartbeatResponse(rejected=_reason(session))
        self.leases.renew(request.session_id, request.lease_generation, now_us)
        session.rtt_p99_us = min(int(request.rtt_p99_us), MAX_REPORTED_RTT_US)
        session.rtt_recent_us = min(int(request.rtt_recent_us), MAX_REPORTED_RTT_US)
        return dataplane_pb2.HeartbeatResponse(
            ack=dataplane_pb2.HeartbeatAck(
                robot_send_time_us=request.robot_send_time_us,
                lease_remaining_us=self.leases.ttl_us,
            )
        )

    def control(
        self, robot_id: str, request: dataplane_pb2.ControlRequest, now_us: int
    ) -> dataplane_pb2.ControlResponse:
        env = request.envelope
        if env.protocol_version != common_pb2.PROTOCOL_VERSION_1:
            return dataplane_pb2.ControlResponse(
                rejected=_reason(ReasonCode.PROTOCOL_VERSION_MISMATCH)
            )
        session = self._authorized(robot_id, env.session_id, env.lease_generation, now_us)
        if isinstance(session, ReasonCode):
            return dataplane_pb2.ControlResponse(rejected=_reason(session))
        command = request.WhichOneof("command")
        if command == "epoch_advanced":
            session.task_epoch = max(session.task_epoch, env.task_epoch)
            session.group.scheduler.advance_epoch(env.session_id, session.task_epoch)
        elif command == "invalidate":
            session.group.scheduler.cancel_session(env.session_id, ReasonCode.REQUEST_FENCED)
        elif command == "close":
            self._drop_session(env.session_id, ReasonCode.SESSION_UNKNOWN)
            self.leases.release(env.session_id)
            return dataplane_pb2.ControlResponse(
                ack=dataplane_pb2.ControlAck(task_epoch=env.task_epoch)
            )
        else:
            return dataplane_pb2.ControlResponse(
                rejected=_reason(ReasonCode.PROTOCOL_MESSAGE_INVALID, "no command")
            )
        return dataplane_pb2.ControlResponse(
            ack=dataplane_pb2.ControlAck(task_epoch=session.task_epoch)
        )

    def _authorized(
        self, robot_id: str, session_id: str, generation: int, now_us: int
    ) -> Session | ReasonCode:
        checked = self.leases.check(session_id, generation, now_us)
        if isinstance(checked, LeaseDenied):
            if checked.reason is ReasonCode.LEASE_EXPIRED:
                self._drop_session(session_id, ReasonCode.LEASE_EXPIRED)
            return checked.reason
        if checked.robot_id != robot_id:
            return ReasonCode.AUTH_DEVICE_UNKNOWN
        session = self.sessions.get(session_id)
        if session is None:
            return ReasonCode.SESSION_UNKNOWN
        return session

    def _drop_session(self, session_id: str, reason: ReasonCode) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            session.group.scheduler.cancel_session(session_id, reason)
            session.group.admission.remove(session_id)

    # --- data plane ---------------------------------------------------------------------------

    def receive(
        self,
        robot_id: str,
        message: dataplane_pb2.RobotMessage,
        size_bytes: int,
        now_us: int,
    ) -> list[Delivery]:
        if message.WhichOneof("body") != "observation":
            return []
        observation = message.observation
        env = observation.envelope

        def reject(code: ReasonCode, detail: str = "") -> list[Delivery]:
            self.counters[code.value] += 1
            if env.session_id in self.sessions:
                self.sessions[env.session_id].rejections[code.value] += 1
            rejected = dataplane_pb2.RequestRejected(envelope=env, reason=_reason(code, detail))
            return [Delivery(env.session_id, dataplane_pb2.GatewayMessage(rejected=rejected))]

        if size_bytes > self.max_message_bytes:
            return reject(ReasonCode.PROTOCOL_MESSAGE_TOO_LARGE)
        if env.protocol_version != common_pb2.PROTOCOL_VERSION_1:
            return reject(ReasonCode.PROTOCOL_VERSION_MISMATCH)
        session = self._authorized(robot_id, env.session_id, env.lease_generation, now_us)
        if isinstance(session, ReasonCode):
            return reject(session)
        binding = session.deployment.binding
        if (
            env.artifact_digest != binding.artifact_digest
            or env.execution_profile_digest != binding.execution_profile_digest
        ):
            return reject(ReasonCode.DEPLOYMENT_NOT_ACTIVE)
        if env.task_epoch < session.task_epoch:
            return reject(ReasonCode.SESSION_EPOCH_STALE)
        decoded = decode_observation(observation, binding)
        if isinstance(decoded, InvalidObservation):
            return reject(decoded.reason, decoded.detail)

        session.task_epoch = env.task_epoch
        session.observations += 1
        freshness = binding.profile.spec.freshness
        # Durations only: the robot's budget and freshness limits are converted to gateway time at
        # receipt, minus link allowances (design §8). The recent RTT is used here because these
        # bounds only avoid wasting GPU time on doomed requests; the robot's own acceptance check
        # is the authority. Capacity planning uses the p99 instead.
        rtt = session.rtt_recent_us or self.default_rtt_us
        deadline = now_us + int(observation.budget_us) - 2 * rtt
        dispatch_by = min(
            now_us + freshness.max_observation_age_at_dispatch_us - rtt,
            now_us + freshness.request_timeout_us - 2 * rtt,
        )
        request = InferenceRequest(
            session_id=env.session_id,
            envelope=env,
            anchor_index=int(observation.anchor_index),
            inputs=decoded,
            received_at_us=now_us,
            deadline_us=deadline,
            dispatch_by_us=dispatch_by,
        )
        dropped = session.group.scheduler.submit(request)
        return self._rejections(dropped)

    def next_batch(self, group_key: str, now_us: int) -> tuple[Batch | None, list[Delivery]]:
        batch, dropped = self.groups[group_key].scheduler.next_batch(now_us)
        return batch, self._rejections(dropped)

    def complete(
        self,
        group_key: str,
        batch: Batch,
        outputs: list[NDArray[np.float32]] | PolicyError,
        now_us: int,
    ) -> list[Delivery]:
        scheduler = self.groups[group_key].scheduler
        if isinstance(outputs, PolicyError):
            code = (
                ReasonCode.WORKER_OUT_OF_MEMORY
                if outputs.out_of_memory
                else ReasonCode.WORKER_CRASHED
            )
            return self._rejections([Dropped(r, code) for r in scheduler.fail(batch)])
        if len(outputs) != len(batch.requests):
            failed = scheduler.fail(batch)
            return self._rejections([Dropped(r, ReasonCode.WORKER_OUTPUT_INVALID) for r in failed])
        results = dict(zip((id(r) for r in batch.requests), outputs, strict=True))
        deliveries: list[Delivery] = []
        for request in scheduler.complete(batch, now_us):
            session = self.sessions.get(request.session_id)
            if session is None or session.lease.generation != request.envelope.lease_generation:
                continue
            output = results[id(request)]
            binding = session.deployment.binding
            expected = (binding.chunk_length, binding.action_dim)
            if output.shape != expected or not np.isfinite(output).all():
                deliveries.extend(
                    self._rejections([Dropped(request, ReasonCode.WORKER_OUTPUT_INVALID)])
                )
                continue
            actions = tensors.encode(np.ascontiguousarray(output, dtype=np.float32))
            chunk = dataplane_pb2.ActionChunk(
                envelope=request.envelope,
                start_index=request.anchor_index,
                actions=actions,
                actions_digest=common_pb2.PayloadDigest(
                    sha256=tensors.payload_digest(actions.data)
                ),
                trace_id=f"batch-{batch.batch_id}",
            )
            session.chunks += 1
            deliveries.append(
                Delivery(request.session_id, dataplane_pb2.GatewayMessage(action_chunk=chunk))
            )
        return deliveries

    def sweep(self, now_us: int) -> None:
        """Expire leases and forget their sessions."""
        for lease in self.leases.expire(now_us):
            self._drop_session(lease.session_id, ReasonCode.LEASE_EXPIRED)

    def _rejections(self, dropped: list[Dropped]) -> list[Delivery]:
        deliveries = []
        for item in dropped:
            self.counters[item.reason.value] += 1
            session = self.sessions.get(item.request.session_id)
            if session is not None:
                session.rejections[item.reason.value] += 1
            rejected = dataplane_pb2.RequestRejected(
                envelope=item.request.envelope, reason=_reason(item.reason)
            )
            deliveries.append(
                Delivery(item.request.session_id, dataplane_pb2.GatewayMessage(rejected=rejected))
            )
        return deliveries

    def status(self) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "session_id": sid,
                    "robot_id": s.lease.robot_id,
                    "revision_id": s.deployment.revision_id,
                    "lease_generation": s.lease.generation,
                    "task_epoch": s.task_epoch,
                    "rtt_p99_us": s.rtt_p99_us,
                    "rtt_recent_us": s.rtt_recent_us,
                    "observations": s.observations,
                    "chunks": s.chunks,
                    "rejections": dict(s.rejections),
                }
                for sid, s in sorted(self.sessions.items(), key=lambda kv: kv[1].lease.robot_id)
            ],
            "groups": {
                key: {
                    "policy": g.scheduler.policy.value,
                    "max_batch": g.scheduler.max_batch,
                    "queued": g.scheduler.queued,
                    "in_flight": g.scheduler.in_flight,
                    "batches": g.scheduler.stats.batches,
                    "dispatched_requests": g.scheduler.stats.dispatched_requests,
                    "batch_sizes": dict(sorted(g.scheduler.stats.batch_sizes.items())),
                    "deadline_misses": g.scheduler.stats.deadline_misses,
                    "starved_on_arrival": g.scheduler.stats.starved_on_arrival,
                    "dropped": dict(g.scheduler.stats.dropped),
                    "admitted_sessions": len(g.admission.admitted),
                }
                for key, g in self.groups.items()
            },
            "counters": dict(self.counters),
        }
