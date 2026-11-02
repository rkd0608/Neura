"""A real model, SmolVLA (Hugging Face's 450M-parameter vision-language-action model,
``lerobot/smolvla_base``), on real frames: a real SO-101's recorded cameras and joints
(``lerobot/svla_so101_pickplace``, played by ``neura_robots.recorded``).

``measure`` times its answers on this machine. ``through_neura`` drives the recorded arm with it
through neura-robot's runtime in real time, under the SmolVLA SO-101 contracts in
``examples/contracts`` (written for a GPU: an answer may be at most 150 ms old), and says what
the runtime did with each answer. The recording has two cameras and SmolVLA reads three: the
third is the wrist camera again. Needs the ``lerobot`` extra; the model (about 0.9 GB, with its
vision-language backbone about 2 GB) and the recording are fetched from the Hub once.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_runtime.policy import ModelInputs

MODEL = "lerobot/smolvla_base"
DATASET = "lerobot/svla_so101_pickplace"


class SmolVLA:
    """SmolVLA on this machine's CPU (or Apple's GPU), called with one observation at a time."""

    def __init__(self, device: str | None = None) -> None:
        import torch
        from lerobot.policies.factory import make_pre_post_processors
        from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy

        self.torch = torch
        if device is None:
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.device = device
        self.policy = SmolVLAPolicy.from_pretrained(MODEL).to(device).eval()
        here = {"device_processor": {"device": device}}
        self.pre, self.post = make_pre_post_processors(
            self.policy.config, MODEL, preprocessor_overrides=here, postprocessor_overrides=here
        )

    def chunk(self, state: Any, front: Any, wrist: Any, task: str) -> NDArray[np.float32]:
        """The next chunk (50 x 6) for joint ``state`` and two camera images (C x H x W, 0..1)."""
        batch = self.pre(
            {
                "observation.state": state.unsqueeze(0),
                "observation.images.camera1": front.unsqueeze(0),
                "observation.images.camera2": wrist.unsqueeze(0),
                "observation.images.camera3": wrist.unsqueeze(0),
                "task": [task],
            }
        )
        with self.torch.inference_mode():
            actions = self.post(self.policy.predict_action_chunk(batch))
        return np.asarray(actions[0].cpu().numpy(), dtype=np.float32)

    def __call__(self, inputs: ModelInputs) -> NDArray[np.float32]:
        """As an in-process policy (``neura_runtime.policy.InProcessPolicy``)."""
        torch = self.torch

        def image(role: str) -> Any:
            hwc = torch.from_numpy(np.array(inputs.images[role]))  # a copy torch may write
            return hwc.permute(2, 0, 1).float() / 255.0

        state = torch.from_numpy(np.asarray(inputs.state["observation.state"], dtype=np.float32))
        return self.chunk(state, image("front"), image("wrist"), inputs.instruction)


def measure(model: SmolVLA, calls: int = 12) -> list[int]:
    """Answer times in µs on frames of the recording, the first (warm-up) call left out."""
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    dataset = LeRobotDataset(DATASET, episodes=[0])
    times = []
    for i in range(calls + 1):
        item = dataset[(i * 7) % len(dataset)]
        started = time.perf_counter()
        model.chunk(
            item["observation.state"],
            item["observation.images.up"],
            item["observation.images.side"],
            str(item["task"]),
        )
        times.append(round((time.perf_counter() - started) * 1e6))
    return times[1:]


@dataclass
class Outcome:
    """What the runtime did with a real model's answers."""

    seconds: float
    requests: int = 0
    accepted: int = 0
    refused: Counter[str] = field(default_factory=Counter)
    counters: dict[str, int] = field(default_factory=dict)  # the runtime's: timeouts and the like
    commands: int = 0
    states: list[str] = field(default_factory=list)
    fault: str = ""
    timeline: str = ""


async def through_neura(model: SmolVLA, contracts: Path, seconds: float = 20.0) -> Outcome:
    """Drive the recorded SO-101 with ``model`` through the runtime for ``seconds`` (real time)."""
    from neura_robots.process import ArmProcess
    from neura_robots.recorded import RecordedArm, RecordedEpisode
    from neura_runtime import replay, timeline
    from neura_runtime.binding import load_binding
    from neura_runtime.policy import InProcessPolicy
    from neura_runtime.runtime import RuntimeState

    binding = load_binding(
        contracts / "smolvla-so101.artifact.yaml",
        contracts / "so101.robot.yaml",
        contracts / "smolvla-so101-30hz.profile.yaml",
    )
    episode = await asyncio.to_thread(RecordedEpisode, DATASET, 0)
    arm = RecordedArm(episode, binding.robot, {"front": "up", "wrist": "side"})
    await asyncio.to_thread(arm.connect)
    outcome = Outcome(seconds)
    records = Path(contracts.parent.parent / "incidents")
    process = ArmProcess(
        arm,
        binding,
        "so101-smolvla",
        "",
        "",
        "",
        say=lambda line: None,
        instruction=episode.task,
        policy=InProcessPolicy(model),
        incidents=records,
    )
    runtime = process.runtime

    class Watch:
        def on_command(self, *args: Any) -> None:
            outcome.commands += 1

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
                outcome.accepted += 1
            else:
                outcome.refused[reason.value if reason else "?"] += 1

        def on_epoch(self, *args: Any) -> None:
            return None

        def on_fault(self, fault: Any) -> None:
            outcome.fault = fault.reason.value

    runtime.observers.append(Watch())
    stop = asyncio.Event()
    serving = asyncio.create_task(process.connection.run(stop))
    try:
        deadline = time.monotonic() + 30
        while runtime.state is not RuntimeState.READY and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        process.inbox.submit("arm")
        ends = time.monotonic() + seconds
        while time.monotonic() < ends:
            if not outcome.states or outcome.states[-1] != runtime.state.value:
                outcome.states.append(runtime.state.value)
            await asyncio.sleep(0.02)
        outcome.counters = dict(runtime.counters)
        outcome.requests = outcome.accepted + sum(outcome.refused.values())
        if process.incidents is not None and not outcome.fault:
            process.incidents.write_soon("smolvla")  # the record of the run, fault or not
    finally:
        stop.set()
        await serving
        arm.close()
        if process.incidents is not None:
            process.incidents.close()
            written = process.incidents.written
            if written:
                outcome.timeline = timeline.tell(replay.read(written[-1]))
    return outcome
