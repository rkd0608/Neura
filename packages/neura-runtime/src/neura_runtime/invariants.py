"""An independent observer of the safety invariants (design §5-§7, §21).

The monitor watches a simulated site from outside: what each robot client sent, what reached it,
what the gateway emitted and what each controller was told to do. It keeps its own model of what
every robot was allowed to accept and execute and records a violation whenever the system did
something that model forbids. It never asks the runtime whether a decision was right; it only
reads the facts a robot holds (its session, lease generation, task epoch and state) at the moment
a message arrives.

Every violation is the class of failure §21 calls Sev-1: an infrastructure-caused acceptance of a
stale, duplicated or unauthorized action, an action executed out of order or out of range, or a
latched fault that cleared without an explicit local reset.

State is bounded: per client only the current task epoch's chunks and the latest observation per
session and epoch are kept, so the monitor can follow a multi-day soak.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from neura.v1 import dataplane_pb2
from neura_runtime import tensors
from neura_runtime.action_queue import MergeKind
from neura_runtime.binding import ExecutionBinding
from neura_runtime.runtime import ACTIVE_STATES, FaultRecord, RuntimeState
from neura_runtime.session import Outbound, SessionClient

MAX_DETAILS = 200


class Lease(Protocol):
    """What the monitor reads of a gateway's lease (``neura_server.leases.Lease``)."""

    @property
    def robot_id(self) -> str: ...
    @property
    def generation(self) -> int: ...
    @property
    def expires_at_us(self) -> int: ...


class Leases(Protocol):
    def get(self, session_id: str) -> Lease | None: ...


# Violation codes. Each names the rule that was broken.
CHUNK_LEASE = "chunk_accepted_under_wrong_lease"
CHUNK_NOT_GRANTED = "chunk_accepted_for_session_not_granted_to_client"
CHUNK_NOT_AUTHORIZED = "chunk_accepted_that_gateway_did_not_emit_under_live_lease"
CHUNK_EPOCH = "chunk_accepted_for_other_epoch"
CHUNK_SEQUENCE = "chunk_accepted_for_superseded_request"
CHUNK_DUPLICATE = "chunk_accepted_twice"
CHUNK_STALE = "chunk_accepted_from_stale_observation"
CHUNK_REVISION = "chunk_accepted_for_other_revision"
CHUNK_ANCHOR = "chunk_accepted_at_wrong_index"
CHUNK_INTEGRITY = "chunk_accepted_with_bad_digest"
CHUNK_STATE = "chunk_accepted_while_inactive"
DISPATCH_ORDER = "action_dispatched_out_of_order"
DISPATCH_EPOCH = "epoch_went_backwards"
DISPATCH_PROVENANCE = "action_dispatched_without_accepted_chunk"
DISPATCH_VALUE = "action_dispatched_differs_from_accepted_chunk"
DISPATCH_RANGE = "action_dispatched_out_of_range"
DISPATCH_STATE = "action_dispatched_while_not_running"
DISPATCH_FAULTED = "action_dispatched_while_fault_latched"
FAULT_CLEARED = "latched_fault_cleared_without_reset"
FAULT_CHANGED = "latched_fault_record_changed"
LEASE_NOT_MONOTONIC = "lease_generation_not_increasing"
EMIT_UNLEASED = "gateway_emitted_chunk_without_live_lease"
EMIT_OLD_GENERATION = "gateway_emitted_chunk_for_old_lease_generation"

CODES = (
    CHUNK_LEASE,
    CHUNK_NOT_GRANTED,
    CHUNK_NOT_AUTHORIZED,
    CHUNK_EPOCH,
    CHUNK_SEQUENCE,
    CHUNK_DUPLICATE,
    CHUNK_STALE,
    CHUNK_REVISION,
    CHUNK_ANCHOR,
    CHUNK_INTEGRITY,
    CHUNK_STATE,
    DISPATCH_ORDER,
    DISPATCH_EPOCH,
    DISPATCH_PROVENANCE,
    DISPATCH_VALUE,
    DISPATCH_RANGE,
    DISPATCH_STATE,
    DISPATCH_FAULTED,
    FAULT_CLEARED,
    FAULT_CHANGED,
    LEASE_NOT_MONOTONIC,
    EMIT_UNLEASED,
    EMIT_OLD_GENERATION,
)


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    client: str
    at_us: int  # true (simulation) time
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "client": self.client,
            "at_us": self.at_us,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class _Observation:
    sequence: int
    anchor: int
    capture_time_us: int  # robot clock


@dataclass(slots=True)
class _Accepted:
    start: int
    rows: NDArray[np.float64]  # robot space, executable rows only


@dataclass(slots=True)
class _ChunkArrival:
    client: _Client
    message: dataplane_pb2.ActionChunk
    session_id: str | None
    lease_generation: int | None
    task_epoch: int
    state: RuntimeState
    accepted_before: int
    now_us: int


@dataclass(frozen=True, slots=True)
class _Contract:
    """What one robot client was bound to: the limits its chunks and actions are checked against."""

    binding: ExecutionBinding
    max_age_us: int
    actions_per_chunk: int
    merge: MergeKind
    low: NDArray[np.float64]
    high: NDArray[np.float64]

    @classmethod
    def of(cls, binding: ExecutionBinding) -> _Contract:
        spec = binding.profile.spec
        commands = binding.robot.spec.command_interface
        return cls(
            binding=binding,
            max_age_us=spec.freshness.max_observation_age_at_acceptance_us,
            actions_per_chunk=spec.chunk.actions_per_chunk,
            merge=MergeKind(spec.merge.mode),
            low=np.array([c.min for c in commands], dtype=np.float64),
            high=np.array([c.max for c in commands], dtype=np.float64),
        )


@dataclass(slots=True)
class _Client:
    label: str
    robot_id: str
    contract: _Contract
    owner: object  # the watched object; held so its id is never reused while watched
    grants: set[tuple[str, int]] = field(default_factory=set)
    # Latest observation sent per (session, epoch); older epochs are dropped.
    observations: dict[tuple[str, int], _Observation] = field(default_factory=dict)
    accepted_keys: set[tuple[str, int, int]] = field(default_factory=set)
    epoch: int = 0
    next_index: int = 0
    chunks: list[_Accepted] = field(default_factory=list)
    state: RuntimeState = RuntimeState.DISCONNECTED
    waiting_since_us: int | None = None
    latched: FaultRecord | None = None
    resets: int = 0
    accepted: int = 0
    dispatched: int = 0


class InvariantMonitor:
    """Feed it the site's events (see ``neura_sim.site``); read ``violations`` at any time."""

    def __init__(
        self,
        binding: ExecutionBinding | None = None,
        *,
        clock: Any = None,
        gateway_observed: bool = True,
    ) -> None:
        """``binding``: the default for clients registered without one. ``gateway_observed``:
        whether the gateway's emissions and grants are fed in too (the simulator); against a real
        gateway process only the robot side is visible, and grants are taken from what robots
        receive."""
        self.binding = binding
        self._clock = clock  # a callable returning a timestamp for violations
        self._gateway_observed = gateway_observed
        self._contracts: dict[int, _Contract] = {}
        self._clients: dict[int, _Client] = {}
        self._labels: Counter[str] = Counter()
        # Latest lease generation the gateway granted per robot identity (survives restarts).
        self._generations: dict[str, int] = {}
        # Chunks the gateway emitted under a live lease, by (session, generation, epoch, seq).
        self._authorized: dict[tuple[str, int, int, int], str] = {}
        self.counts: Counter[str] = Counter()
        self.by_client: Counter[str] = Counter()
        self.details: list[Violation] = []
        self.checked: Counter[str] = Counter()

    # --- results ------------------------------------------------------------------------------

    @property
    def violations(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> dict[str, Any]:
        return {
            "violations": self.violations,
            "by_code": dict(sorted(self.counts.items())),
            "checked": dict(sorted(self.checked.items())),
            "first": [v.as_dict() for v in self.details[:20]],
        }

    def _violate(self, code: str, client: str, detail: str) -> None:
        self.counts[code] += 1
        self.by_client[client] += 1
        if len(self.details) < MAX_DETAILS:
            now = int(self._clock()) if self._clock is not None else 0
            self.details.append(Violation(code, client, now, detail))

    # --- clients --------------------------------------------------------------------------------

    def client(
        self, key: object, robot_id: str, binding: ExecutionBinding | None = None
    ) -> _Client:
        """The record for one robot client process, identified by the object ``key``."""
        found = self._clients.get(id(key))
        if found is None:
            bound = binding or self.binding
            if bound is None:
                raise ValueError("no binding for this client")
            contract = self._contracts.get(id(bound))
            if contract is None:
                contract = self._contracts[id(bound)] = _Contract.of(bound)
            self._labels[robot_id] += 1
            n = self._labels[robot_id]
            label = robot_id if n == 1 else f"{robot_id}#{n}"
            found = _Client(label=label, robot_id=robot_id, contract=contract, owner=key)
            self._clients[id(key)] = found
        return found

    # --- robot side -----------------------------------------------------------------------------

    def session_granted(self, client: _Client, session_id: str, generation: int) -> None:
        """The client received an accepted OpenSession response."""
        client.grants.add((session_id, generation))
        if not self._gateway_observed:
            self.lease_granted(client.robot_id, session_id, generation)

    def observation_sent(self, client: _Client, message: dataplane_pb2.Observation) -> None:
        env = message.envelope
        key = (env.session_id, env.task_epoch)
        if env.task_epoch > client.epoch:
            self._enter_epoch(client, env.task_epoch)
        client.observations[key] = _Observation(
            env.sequence, int(message.anchor_index), int(message.capture_time_us)
        )

    def chunk_arriving(
        self,
        client: _Client,
        chunk: dataplane_pb2.ActionChunk,
        *,
        session_id: str | None,
        lease_generation: int | None,
        task_epoch: int,
        state: RuntimeState,
        accepted_before: int,
        now_us: int,
    ) -> _ChunkArrival:
        return _ChunkArrival(
            client,
            chunk,
            session_id,
            lease_generation,
            task_epoch,
            state,
            accepted_before,
            now_us,
        )

    def chunk_handled(
        self, arrival: _ChunkArrival, accepted_after: int, state: RuntimeState
    ) -> None:
        client = arrival.client
        contract = client.contract
        client.state = state
        if accepted_after == arrival.accepted_before:
            return
        self.checked["chunks_accepted"] += 1
        client.accepted += 1
        chunk = arrival.message
        env = chunk.envelope
        label = client.label
        key = (env.session_id, env.lease_generation, env.task_epoch, env.sequence)
        if (env.session_id, env.lease_generation) != (arrival.session_id, arrival.lease_generation):
            self._violate(
                CHUNK_LEASE,
                label,
                f"chunk for {env.session_id}/g{env.lease_generation}, robot held "
                f"{arrival.session_id}/g{arrival.lease_generation}",
            )
        if (env.session_id, env.lease_generation) not in client.grants:
            self._violate(CHUNK_NOT_GRANTED, label, f"{env.session_id}/g{env.lease_generation}")
        if self._gateway_observed and key not in self._authorized:
            self._violate(CHUNK_NOT_AUTHORIZED, label, f"{key}")
        if env.task_epoch != arrival.task_epoch:
            self._violate(
                CHUNK_EPOCH, label, f"chunk epoch {env.task_epoch}, robot {arrival.task_epoch}"
            )
        if arrival.state not in ACTIVE_STATES:
            self._violate(CHUNK_STATE, label, f"accepted in {arrival.state.value}")
        accepted_key = (env.session_id, env.task_epoch, env.sequence)
        if accepted_key in client.accepted_keys:
            self._violate(CHUNK_DUPLICATE, label, f"{accepted_key}")
        client.accepted_keys.add(accepted_key)
        observation = client.observations.get((env.session_id, env.task_epoch))
        if observation is None or observation.sequence != env.sequence:
            latest = observation.sequence if observation else None
            self._violate(
                CHUNK_SEQUENCE, label, f"chunk answers {env.sequence}, latest request {latest}"
            )
        else:
            age = arrival.now_us - observation.capture_time_us
            if age > contract.max_age_us:
                self._violate(CHUNK_STALE, label, f"observation {age} us old at acceptance")
            if (
                arrival.state is RuntimeState.WAITING
                and client.waiting_since_us is not None
                and observation.capture_time_us < client.waiting_since_us
            ):
                self._violate(CHUNK_STALE, label, "observation taken before the robot starved")
            if int(chunk.start_index) != observation.anchor:
                self._violate(
                    CHUNK_ANCHOR, label, f"start {chunk.start_index}, anchor {observation.anchor}"
                )
        binding = contract.binding
        if (
            env.artifact_digest != binding.artifact_digest
            or env.execution_profile_digest != binding.execution_profile_digest
        ):
            self._violate(CHUNK_REVISION, label, "digests differ from the active revision")
        if tensors.payload_digest(chunk.actions.data) != chunk.actions_digest.sha256:
            self._violate(CHUNK_INTEGRITY, label, "payload digest mismatch")
            return
        try:
            model = tensors.decode(
                chunk.actions, shape=(binding.chunk_length, binding.action_dim)
            ).astype(np.float64)
        except tensors.TensorError as exc:
            self._violate(CHUNK_INTEGRITY, label, f"undecodable actions: {exc}")
            return
        rows = binding.action_transform.to_robot(model)[: contract.actions_per_chunk]
        if env.task_epoch > client.epoch:
            self._enter_epoch(client, env.task_epoch)
        if env.task_epoch == client.epoch:
            client.chunks.append(_Accepted(int(chunk.start_index), rows))

    def dispatched(
        self,
        client: _Client,
        epoch: int,
        index: int,
        action: NDArray[np.float64] | None,
        state: RuntimeState,
    ) -> None:
        self.checked["actions_dispatched"] += 1
        client.dispatched += 1
        label = client.label
        if client.latched is not None:
            self._violate(DISPATCH_FAULTED, label, f"epoch {epoch} index {index}")
        if state is not RuntimeState.RUNNING:
            self._violate(DISPATCH_STATE, label, f"dispatched in {state.value}")
        if epoch < client.epoch:
            self._violate(DISPATCH_EPOCH, label, f"epoch {epoch} after {client.epoch}")
            return
        if epoch > client.epoch:
            self._enter_epoch(client, epoch)
        if index != client.next_index:
            self._violate(
                DISPATCH_ORDER, label, f"epoch {epoch}: index {index}, expected {client.next_index}"
            )
        client.next_index = index + 1
        covering = [c for c in client.chunks if c.start <= index < c.start + len(c.rows)]
        if not covering:
            self._violate(DISPATCH_PROVENANCE, label, f"epoch {epoch} index {index}")
        elif action is not None:
            if client.contract.merge is MergeKind.BLEND:
                candidates = np.stack([c.rows[index - c.start] for c in covering])
                lowest, highest = candidates.min(axis=0), candidates.max(axis=0)
                if np.any(action < lowest - 1e-9) or np.any(action > highest + 1e-9):
                    self._violate(DISPATCH_VALUE, label, f"epoch {epoch} index {index} (blend)")
            elif not np.array_equal(action, covering[-1].rows[index - covering[-1].start]):
                self._violate(DISPATCH_VALUE, label, f"epoch {epoch} index {index}")
        low, high = client.contract.low, client.contract.high
        if action is not None and (np.any(action < low) or np.any(action > high)):
            self._violate(DISPATCH_RANGE, label, f"epoch {epoch} index {index}")
        # Chunks that end before the next index can no longer matter.
        client.chunks = [c for c in client.chunks if c.start + len(c.rows) > client.next_index]

    def ticked(
        self,
        client: _Client,
        now_us: int,
        state: RuntimeState,
        epoch: int,
        fault: FaultRecord | None,
    ) -> None:
        label = client.label
        if client.latched is not None:
            if state is not RuntimeState.FAULTED:
                self._violate(
                    FAULT_CLEARED, label, f"{client.latched.reason.value} -> {state.value}"
                )
                client.latched = None
            elif fault != client.latched:
                self._violate(FAULT_CHANGED, label, f"{client.latched} -> {fault}")
                client.latched = fault
        elif state is RuntimeState.FAULTED:
            client.latched = fault
            self.checked["faults_latched"] += 1
        if state is RuntimeState.WAITING and client.state is not RuntimeState.WAITING:
            client.waiting_since_us = now_us
        client.state = state
        if epoch < client.epoch:
            self._violate(DISPATCH_EPOCH, label, f"epoch {epoch} after {client.epoch}")
        elif epoch > client.epoch:
            self._enter_epoch(client, epoch)

    def reset(self, client: _Client) -> None:
        """An operator performed the explicit local reset; the latch may now clear."""
        client.latched = None
        client.resets += 1
        self.checked["resets"] += 1

    def _enter_epoch(self, client: _Client, epoch: int) -> None:
        client.epoch = epoch
        client.next_index = 0
        client.chunks = []
        client.accepted_keys = {k for k in client.accepted_keys if k[1] >= epoch}
        client.observations = {k: v for k, v in client.observations.items() if k[1] >= epoch}

    # --- gateway side ---------------------------------------------------------------------------

    def lease_granted(self, robot_id: str, session_id: str, generation: int) -> None:
        self.checked["leases_granted"] += 1
        previous = self._generations.get(robot_id, 0)
        if generation <= previous:
            self._violate(LEASE_NOT_MONOTONIC, robot_id, f"granted g{generation} after g{previous}")
        self._generations[robot_id] = max(previous, generation)

    def chunk_emitted(
        self, chunk: dataplane_pb2.ActionChunk, leases: Leases, gateway_now_us: int
    ) -> None:
        self.checked["chunks_emitted"] += 1
        env = chunk.envelope
        lease = leases.get(env.session_id)
        robot = lease.robot_id if lease is not None else "?"
        if (
            lease is None
            or lease.generation != env.lease_generation
            or lease.expires_at_us <= gateway_now_us
        ):
            self._violate(EMIT_UNLEASED, robot, f"{env.session_id}/g{env.lease_generation}")
            return
        latest = self._generations.get(lease.robot_id, 0)
        if env.lease_generation != latest:
            self._violate(EMIT_OLD_GENERATION, robot, f"g{env.lease_generation}, latest g{latest}")
            return
        key = (env.session_id, env.lease_generation, env.task_epoch, env.sequence)
        self._authorized[key] = robot
        if len(self._authorized) > 50_000:
            # Oldest first (dicts keep insertion order); anything this old is long since stale.
            for stale in list(self._authorized)[:25_000]:
                del self._authorized[stale]


def watch_robot(
    monitor: InvariantMonitor,
    owner: object,
    robot_id: str,
    client: SessionClient,
    last_command: Callable[[], Any],
) -> Any:
    """Wire a robot's session client and runtime to ``monitor``: every observation it sends,
    session it is granted, chunk it receives and action it dispatches. ``last_command`` returns
    the command vector the controller was last given. Returns the monitor's record."""
    runtime = client.runtime
    record = monitor.client(owner, robot_id, runtime.binding)
    drain = client.drain
    on_open = client.on_open_response
    on_message = client.on_gateway_message

    def watched_drain() -> list[Outbound]:
        items = drain()  # SessionClient.tick drains through this too
        for item in items:
            message = item.message
            if (
                isinstance(message, dataplane_pb2.RobotMessage)
                and message.WhichOneof("body") == "observation"
            ):
                monitor.observation_sent(record, message.observation)
        return items

    def watched_open(response: dataplane_pb2.OpenSessionResponse, now_us: int) -> None:
        if response.WhichOneof("result") == "accepted":
            accepted = response.accepted
            monitor.session_granted(record, accepted.session_id, accepted.lease_generation)
        on_open(response, now_us)

    def watched_message(message: dataplane_pb2.GatewayMessage, now_us: int) -> None:
        if message.WhichOneof("body") != "action_chunk":
            on_message(message, now_us)
            return
        arrival = monitor.chunk_arriving(
            record,
            message.action_chunk,
            session_id=runtime.session_id,
            lease_generation=runtime.lease_generation,
            task_epoch=runtime.task_epoch,
            state=runtime.state,
            accepted_before=runtime.counters["chunks_accepted"],
            now_us=now_us,
        )
        on_message(message, now_us)
        monitor.chunk_handled(arrival, runtime.counters["chunks_accepted"], runtime.state)

    client.drain = watched_drain  # type: ignore[method-assign]
    client.on_open_response = watched_open  # type: ignore[method-assign]
    client.on_gateway_message = watched_message  # type: ignore[method-assign]
    runtime.dispatch_listener = lambda epoch, index: monitor.dispatched(
        record, epoch, index, last_command(), runtime.state
    )
    return record
