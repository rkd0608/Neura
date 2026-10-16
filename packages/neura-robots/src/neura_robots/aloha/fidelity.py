"""Step-for-step comparison of ``neura_robots.aloha`` with gym-aloha itself.

Run one side per interpreter: gym-aloha needs dm_control, which installs on Python 3.12 but not
3.13, so this file runs on its own (no imports from Neura) for the reference:

    python3.12 -I fidelity.py record --impl gym-aloha --task transfer_cube --out ref.npz
    python3.13 -m neura_robots.aloha.fidelity record --impl neura --task transfer_cube \
        --out ours.npz
    python3.13 -m neura_robots.aloha.fidelity compare ref.npz ours.npz

Both play the same scripted actions (smooth joint motions with the grippers opening and closing,
deterministic from the seed) from the same seeded scene, and record, at every control step, all
of qpos and qvel (arms and objects), the contact pairs, the reward, and some rendered frames.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

START = np.array([0, -0.96, 1.16, 0, -0.3, 0, 1.0, 0, -0.96, 1.16, 0, -0.3, 0, 1.0])
FRAMES_AT = (0, 100, 250, 399)


def scripted_actions(steps: int, seed: int) -> NDArray[np.float64]:
    """Smooth sinusoidal joint motions around the start pose; grippers open and close."""
    rng = np.random.RandomState(seed)
    t = np.arange(steps) * 0.02
    amplitude = rng.uniform(0.05, 0.35, size=14)
    frequency = rng.uniform(0.2, 0.8, size=14)
    phase = rng.uniform(0, 2 * np.pi, size=14)
    wave = np.sin(2 * np.pi * frequency * t[:, None] + phase)
    actions: NDArray[np.float64] = START + amplitude * wave
    for gripper in (6, 13):
        actions[:, gripper] = 0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 0.4 * t + phase[gripper]))
    return actions


def _record_gym_aloha(task: str, seed: int, actions: NDArray[np.float64]) -> dict[str, Any]:
    import gym_aloha  # noqa: F401 - registers the environments
    import gymnasium as gym

    name = {"transfer_cube": "AlohaTransferCube-v0", "insertion": "AlohaInsertion-v0"}[task]
    env: Any = gym.make(f"gym_aloha/{name}", obs_type="pixels_agent_pos", max_episode_steps=10_000)
    env.reset(seed=seed)
    physics = env.unwrapped._env.physics
    model = physics.model

    def contacts() -> list[str]:
        pairs = set()
        for i in range(physics.data.ncon):
            c = physics.data.contact[i]
            pairs.add(f"{model.id2name(c.geom1, 'geom')}|{model.id2name(c.geom2, 'geom')}")
        return sorted(pairs)

    out: dict[str, Any] = {"qpos": [], "qvel": [], "reward": [], "contacts": [], "frames": {}}
    out["initial_qpos"] = physics.data.qpos.copy()
    for step, action in enumerate(actions):
        observation, reward, _, _, _ = env.step(action)
        out["qpos"].append(physics.data.qpos.copy())
        out["qvel"].append(physics.data.qvel.copy())
        out["reward"].append(int(float(reward)))
        out["contacts"].append(contacts())
        if step in FRAMES_AT:
            out["frames"][step] = observation["pixels"]["top"].copy()
    env.close()
    return out


def _record_neura(task: str, seed: int, actions: NDArray[np.float64]) -> dict[str, Any]:
    from neura_robots.aloha import AlohaSim

    sim = AlohaSim(task)  # type: ignore[arg-type]
    sim.reset(seed)
    names = sim._geom_names
    out: dict[str, Any] = {"qpos": [], "qvel": [], "reward": [], "contacts": [], "frames": {}}
    out["initial_qpos"] = sim.data.qpos.copy()
    for step, action in enumerate(actions):
        result = sim.step(action)
        out["qpos"].append(sim.data.qpos.copy())
        out["qvel"].append(sim.data.qvel.copy())
        out["reward"].append(result.reward)
        pairs = {
            f"{names[sim.data.contact[i].geom1]}|{names[sim.data.contact[i].geom2]}"
            for i in range(sim.data.ncon)
        }
        out["contacts"].append(sorted(pairs))
        if step in FRAMES_AT:
            out["frames"][step] = sim.render("top")
    sim.close()
    return out


def record(impl: str, task: str, seed: int, steps: int, path: Path) -> None:
    actions = scripted_actions(steps, seed)
    data = (_record_gym_aloha if impl == "gym-aloha" else _record_neura)(task, seed, actions)
    import mujoco

    np.savez_compressed(
        path,
        actions=actions,
        initial_qpos=data["initial_qpos"],
        qpos=np.array(data["qpos"]),
        qvel=np.array(data["qvel"]),
        reward=np.array(data["reward"]),
        contacts=np.array([json.dumps(c) for c in data["contacts"]]),
        frame_steps=np.array(sorted(data["frames"])),
        frames=np.array([data["frames"][k] for k in sorted(data["frames"])]),
        meta=np.array(
            json.dumps({"impl": impl, "task": task, "seed": seed, "mujoco": mujoco.__version__})
        ),
    )


def compare(reference: Path, candidate: Path) -> dict[str, Any]:
    a, b = np.load(reference), np.load(candidate)
    meta_a, meta_b = json.loads(str(a["meta"])), json.loads(str(b["meta"]))
    contacts_a = [set(json.loads(s)) for s in a["contacts"]]
    contacts_b = [set(json.loads(s)) for s in b["contacts"]]
    frames = np.abs(a["frames"].astype(np.int16) - b["frames"].astype(np.int16))
    result = {
        "reference": meta_a,
        "candidate": meta_b,
        "steps": len(a["reward"]),
        "initial_qpos_max_abs_diff": float(np.max(np.abs(a["initial_qpos"] - b["initial_qpos"]))),
        "qpos_max_abs_diff": float(np.max(np.abs(a["qpos"] - b["qpos"]))),
        "qvel_max_abs_diff": float(np.max(np.abs(a["qvel"] - b["qvel"]))),
        "reward_mismatches": int(np.sum(a["reward"] != b["reward"])),
        "max_reward": [int(a["reward"].max()), int(b["reward"].max())],
        "contact_mismatches": sum(x != y for x, y in zip(contacts_a, contacts_b, strict=True)),
        "frame_mean_abs_diff": float(frames.mean()),
        "frame_max_abs_diff": int(frames.max()),
    }
    result["identical_physics"] = (
        result["qpos_max_abs_diff"] == 0.0
        and result["reward_mismatches"] == 0
        and result["contact_mismatches"] == 0
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--impl", choices=["gym-aloha", "neura"], required=True)
    rec.add_argument("--task", choices=["transfer_cube", "insertion"], default="transfer_cube")
    rec.add_argument("--seed", type=int, default=1000)
    rec.add_argument("--steps", type=int, default=400)
    rec.add_argument("--out", type=Path, required=True)
    cmp = sub.add_parser("compare")
    cmp.add_argument("reference", type=Path)
    cmp.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)
    if args.command == "record":
        record(args.impl, args.task, args.seed, args.steps, args.out)
        return 0
    result = compare(args.reference, args.candidate)
    print(json.dumps(result, indent=2))
    return 0 if result["identical_physics"] else 1


if __name__ == "__main__":
    sys.exit(main())
