"""The two loops compared, on one clock, the same simulated arm and the same policy:

- ``naive``: the loop most teams write first, and LeRobot's own synchronous loop: when the queue of
  actions is empty, take an observation, call the policy (the loop waits for it), then send its
  chunk's actions one per control period, from the first;
- ``neura``: the same policy through neura-robot's runtime (``RobotRuntime``), its answers handed
  to the runtime when they arrive, as ``neura://`` would.

Time is simulated: each control period the arm's physics advances one step (a MuJoCo Menagerie
SO-ARM100) and a policy answer arrives when the condition says (``compare.scenarios``), so a run is
deterministic and takes seconds whatever the answer times. The policy is scripted: from the time
of its observation it plans where each joint should be for the next chunk, along a smooth path,
as a trained policy plans from what it saw. What is measured does not depend on what it plans:
how old the observation behind each action sent was, how late each action was for the moment it
was planned for, how long the arm went without a new command, how far one command jumped from the
last, and what was sent after a takeover or a crash.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura.v1 import common_pb2, dataplane_pb2
from neura_contracts import (
    ExecutionProfile,
    ModelArtifact,
    ReasonCode,
    RobotProfile,
    parse_document,
)
from neura_contracts.inference import (
    InferenceError,
    PolicyDeclaration,
    Timing,
    declared_artifact,
    derive_execution_profile,
)
from neura_robots.compare.scenarios import Condition, S
from neura_robots.sim.arm import SimArm
from neura_robots.sim.menagerie import MenagerieScene
from neura_robots.sim.world import LockstepWorld
from neura_runtime import tensors
from neura_runtime.binding import ExecutionBinding
from neura_runtime.runtime import RobotRuntime, RuntimeState, SendObservation
from neura_runtime.serving import chunk_from

CHUNK = 50  # actions per chunk, as SmolVLA's and ACT's
DURATION_US = 10 * S
ARM_AT_US = 1 * S  # the operator arms the robot one second in
CYCLE_S = 4.0  # the planned path: each joint sways on a 4-second cycle
BLEND = 10  # a plan joins the path from where the arm was seen within this many actions


@dataclass
class Sent:
    at_us: int  # when the command went to the arm
    observed_us: int  # when the observation behind it was taken
    planned_us: int  # the moment the policy planned it for
    action: NDArray[np.float64]


@dataclass
class Run:
    loop: str
    condition: str
    sent: list[Sent] = field(default_factory=list)
    positions: list[tuple[int, NDArray[np.float64]]] = field(default_factory=list)
    task_until_us: int = DURATION_US  # when the task stopped: a takeover, a fault, the loop dying
    stopped_why: str = ""
    record: bool = False  # whether the run left a record of why it stopped
    chunks_refused: int = 0
    answers: int = 0


class Policy:
    """A smooth planned path for every joint but the gripper, about the arm's home pose."""

    def __init__(self, scene: MenagerieScene) -> None:
        home = scene.qpos()
        low = np.array([c.low for c in scene.commands])
        high = np.array([c.high for c in scene.commands])
        room = np.minimum(home - low, high - home)
        self.home = home
        self.amplitude = np.minimum(0.35, 0.8 * room)
        self.amplitude[-1] = 0.0  # the gripper stays

    def at(self, t_us: int) -> NDArray[np.float64]:
        phase = math.sin(2 * math.pi * t_us / S / CYCLE_S)
        return np.asarray(self.home + self.amplitude * phase, dtype=np.float64)

    def chunk(
        self, observed_us: int, period_us: int, state: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        """The plan from an observation, as a trained policy plans from what it sees: from where
        the arm was observed, onto the path within BLEND actions; action k for k + 1 periods
        after the observation."""
        plan = []
        for k in range(CHUNK):
            weight = min(1.0, (k + 1) / BLEND)
            plan.append(state + weight * (self.at(observed_us + (k + 1) * period_us) - state))
        return np.stack(plan)


def contracts(scene: MenagerieScene, pass_us: int = 120_000) -> ExecutionBinding:
    """The contracts deployed for a GPU server answering in ``pass_us`` (p99): the same for every
    condition, as a robot's contracts do not change when its network misbehaves."""
    robot = scene.profile()
    robot["spec"]["cameras"] = []  # the policy reads the joints only
    model = declared_artifact(
        PolicyDeclaration(adapter="python", chunk_length=CHUNK, cameras={}),
        robot,
        name="planned-path",
        version="1",
        weights=[{"path": "policy.py", "digest": "sha256:" + "0" * 64, "size_bytes": 1}],
    )
    profile, _ = derive_execution_profile(
        model, robot, Timing({1: pass_us}), device="cuda", precision="fp32"
    )
    a, r, p = (parse_document(d) for d in (model, robot, profile))
    assert isinstance(a, ModelArtifact)
    assert isinstance(r, RobotProfile)
    assert isinstance(p, ExecutionProfile)
    return ExecutionBinding(a, r, p)


def refusal(scene: MenagerieScene, pass_us: int) -> str | None:
    """Why neura's contracts would refuse a policy this slow before the arm moves, or None."""
    try:
        contracts(scene, pass_us)
    except InferenceError as error:
        return "; ".join(error.problems)
    return None


def _world(model: str = "so_arm100") -> tuple[MenagerieScene, LockstepWorld[MenagerieScene]]:
    scene = MenagerieScene(model)
    scene.reset()
    return scene, LockstepWorld(scene)


def naive(condition: Condition) -> Run:
    scene, world = _world()
    policy = Policy(scene)
    period = round(scene.dt * S)
    run = Run("naive", condition.name)
    queue: deque[tuple[int, int, NDArray[np.float64]]] = deque()
    t, request = 0, 0

    def step() -> None:
        nonlocal t
        world.advance()
        t += period
        run.positions.append((t, world.qpos()))

    while t < ARM_AT_US:
        step()
    while t < DURATION_US:
        if condition.takeover_at_us is not None and t >= condition.takeover_at_us:
            run.task_until_us, run.stopped_why = condition.takeover_at_us, "the operator took over"
            break
        if not queue:
            observed, seen = t, world.qpos()
            answer = condition.answer_time(request, observed)
            request += 1
            if answer is None:  # the policy raised: the loop ends, the arm keeps its last goal
                run.task_until_us, run.stopped_why = t, "the policy failed: the loop ended"
                break
            for _ in range(math.ceil(answer / period)):  # the loop waits for the answer
                step()
            run.answers += 1
            plan = policy.chunk(observed, period, seen)
            queue.extend((observed, observed + (k + 1) * period, plan[k]) for k in range(CHUNK))
        observed, planned, action = queue.popleft()
        world.set_targets(action)
        run.sent.append(Sent(t, observed, planned, action))
        step()
    while t < DURATION_US:  # the arm, left alone, holds its last goal
        step()
    return run


def neura(condition: Condition, binding: ExecutionBinding | None = None) -> Run:
    scene, world = _world()
    policy = Policy(scene)
    binding = binding or contracts(scene)
    period = binding.profile.spec.control_period_us
    arm = SimArm(world, binding.robot)
    arm.connect()
    runtime = RobotRuntime(binding, arm, arm)
    run = Run("neura", condition.name, record=True)
    observed_at: dict[int, int] = {}  # observation sequence -> when it was taken
    chunks: list[tuple[int, int]] = []  # accepted chunks: (start index, observation time)
    arrivals: list[tuple[int, int, Callable[[int], None]]] = []  # (due, order, deliver(now))
    t, request = 0, 0

    class Watch:  # the runtime's observer: what it sends, what it accepts
        def on_command(self, now: int, epoch: int, index: int, action: NDArray[np.float64]) -> None:
            start, observed = max((c for c in chunks if c[0] <= index), default=(index, now))
            planned = observed + (index - start + 1) * period
            run.sent.append(Sent(now - origin, observed - origin, planned - origin, action.copy()))

        def on_chunk(
            self,
            now: int,
            epoch: int,
            sequence: int,
            start: int,
            accepted: bool,
            reason: Any,
            trimmed: int,
        ) -> None:
            if accepted:
                chunks.append((start, observed_at[sequence]))
            else:
                run.chunks_refused += 1

        def on_epoch(self, previous: int, current: int, cause: Any) -> None:
            chunks.clear()

        def on_fault(self, fault: Any) -> None:
            if run.task_until_us == DURATION_US:
                run.task_until_us = fault.at_us - origin
                run.stopped_why = f"fault: {fault.reason.value}"

    origin = 10 * S  # the runtime's clock starts elsewhere; times reported from origin
    runtime.observers.append(Watch())
    runtime.attach_session("compare", 1)

    def deliver_due(now: int) -> None:
        for arrival in sorted(a for a in arrivals if a[0] <= t):
            arrivals.remove(arrival)
            arrival[2](now)

    armed = taken_over = False
    while t < DURATION_US:
        now = origin + t
        if not armed and t >= ARM_AT_US and runtime.state is RuntimeState.READY:
            runtime.arm(now)
            armed = True
        takeover = condition.takeover_at_us
        if not taken_over and takeover is not None and t >= takeover:
            runtime.operator_takeover(now)
            taken_over = True
            run.task_until_us, run.stopped_why = t, "the operator took over"
        deliver_due(now)
        runtime.tick(now)
        for item in runtime.outbox:
            if isinstance(item, SendObservation):
                message = item.message
                observed_at[message.envelope.sequence] = message.capture_time_us
                answer = condition.answer_time(request, t)
                request += 1
                due = t + (answer if answer is not None else 50_000)
                deliver = _answer(runtime, message, answer, policy, period, origin, binding, run)
                arrivals.append((due, request, deliver))
        runtime.outbox.clear()
        world.advance()
        t += period
        run.positions.append((t, world.qpos()))
    if not run.sent and not run.stopped_why:
        run.stopped_why = "held the arm: no answer was fresh enough"
    return run


def _answer(
    runtime: RobotRuntime,
    message: dataplane_pb2.Observation,
    answer: int | None,
    policy: Policy,
    period: int,
    origin: int,
    binding: ExecutionBinding,
    run: Run,
) -> Callable[[int], None]:
    """What happens when the answer to ``message`` arrives: its chunk, or the policy's failure."""

    def deliver(now: int) -> None:
        if answer is None:
            rejected = dataplane_pb2.RequestRejected(
                envelope=message.envelope,
                reason=common_pb2.Reason(
                    code=ReasonCode.WORKER_INFERENCE_FAILED.value, message="the policy failed"
                ),
            )
            runtime.on_rejected(rejected, now)
            return
        seen = tensors.decode(message.state["observation.state"]).astype(np.float64)
        plan = policy.chunk(message.capture_time_us - origin, period, seen).astype(np.float32)
        chunk = chunk_from(message.envelope, message.anchor_index, plan, binding)
        assert chunk is not None
        run.answers += 1
        runtime.on_chunk(chunk, now)

    return deliver
