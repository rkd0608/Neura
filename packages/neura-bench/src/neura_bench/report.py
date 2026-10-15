"""Benchmark reports: results as JSON, and the Markdown report generated from it."""

from __future__ import annotations

import contextlib
import os
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from neura_bench import suite

CONFIG_NAMES = {
    suite.DEDICATED: "Dedicated: one device per robot",
    suite.DEDICATED_SHARED: "Dedicated: one process per robot, one device",
    suite.NEURA_FIFO: "Neura, FIFO",
    suite.NEURA_RR: "Neura, round robin",
    suite.NEURA_DEADLINE: "Neura, deadline + fairness",
    suite.NEURA_MULTI_ADAPTER: "Neura, multi-adapter batching",
    suite.NEURA_CO_LOCATED: "Neura, co-located groups",
}
CASE_NAMES = {
    suite.SAME_WEIGHTS: "Same weights on every robot",
    suite.MULTI_ADAPTER: "Per-robot LoRA adapters of one base model",
    suite.CO_LOCATED: "Different full fine-tunes of one architecture",
    suite.DIFFERENT: "Different architectures (ACT and Diffusion Policy)",
}
WORKLOAD_NAMES = {
    suite.PHASED: "phased arrivals",
    suite.SYNCHRONIZED: "synchronized (bursty) arrivals",
    suite.BACKGROUND: "phased, with a background GPU job",
}


def device_name(device: str) -> str:
    if device.startswith("cuda"):
        try:
            import torch

            return str(torch.cuda.get_device_name(torch.device(device)))
        except Exception:  # report what we can
            return device
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return f"CPU: {line.split(':', 1)[1].strip()}"
    return f"CPU: {platform.processor() or platform.machine()}"


def label(device: str) -> str:
    """A directory name for a device's reports, e.g. nvidia-a10g or cpu-intel-xeon-8375c."""
    name = device_name(device).lower().removeprefix("cpu: ")
    name = re.sub(r"\(r\)|\(tm\)|cpu @.*|@.*", "", name)
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return ("cpu-" + slug if not device.startswith("cuda") else slug)[:60]


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def environment(device: str) -> dict[str, Any]:
    commit = None
    with contextlib.suppress(OSError):
        git = ["git", "rev-parse", "--short", "HEAD"]
        out = subprocess.run(git, capture_output=True, text=True, check=False)  # noqa: S603
        commit = out.stdout.strip() or None
    return {
        "device": device,
        "device_name": device_name(device),
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "torch": _version("torch"),
        "lerobot": _version("lerobot"),
        "git_commit": commit,
        "command": " ".join(["neura-bench", *sys.argv[1:]]),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


# --- Markdown -------------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def markdown(data: dict[str, Any]) -> str:
    env = data["environment"]
    config = data["config"]
    slo = config["slo"]
    cpu = not env["device"].startswith("cuda")
    out = [
        f"# Neura GPU-sharing benchmark: {env['device_name']}",
        "",
        f"Generated {env['generated_at']} from commit `{env['git_commit']}` with "
        f"`{env['command']}` (torch {env['torch']}, LeRobot {env['lerobot']}). "
        "Regenerate the Markdown from `results.json` with `neura-bench report`.",
        "",
    ]
    if cpu:
        out += [
            "> **This run is on a CPU development host, not a GPU.** It shows the harness "
            "working end to end and how sharing behaves on this host. Its numbers say nothing "
            "about GPU capacity: run `make bench` on the GPU runner for that.",
            "",
        ]
    out += [
        "Models: synthetic checkpoints with the real LeRobot architectures and random weights "
        f"(ACT and Diffusion Policy, size `{config['size']}`, cameras "
        f"{_cameras(config['image'])}, {config['precision']}). Latency, memory and batching "
        "depend on architecture and shapes, not on what weights learned; closed-loop task "
        "success needs trained weights and is not measured here.",
        "",
        "## Robots per device at the SLO",
        "",
        f"SLO: at most {slo['max_misses_per_1000']} deadline misses per 1,000 requests, at most "
        f"{slo['max_starvations_per_robot_hour']} starvation events per robot-hour, and no robot "
        "faults. A configuration's capacity is the largest robot count at which it and every "
        "smaller count measured met the SLO (simulation with measured latency and contention, "
        "below).",
        "",
    ]
    capacity = data["capacity"]
    rows = []
    for case, workloads in capacity.items():
        for workload, configurations in workloads.items():
            best = _best_neura(configurations)
            row = [CASE_NAMES[case], WORKLOAD_NAMES[workload]]
            for configuration in (suite.DEDICATED, suite.DEDICATED_SHARED):
                entry = configurations.get(configuration)
                row.append(_capacity_cell(entry))
            row.append(
                f"{_capacity_cell(configurations[best])} ({CONFIG_NAMES[best]})" if best else "-"
            )
            rows.append(row)
    out += _table(
        ["Case", "Workload", "One device per robot", "One process per robot", "Best Neura"],
        rows,
    )
    out += ["", *_crossovers(capacity), ""]

    out += ["## Measurements on the device", ""]
    measured = data["measurements"]
    for model in ("act", "diffusion"):
        points = measured["latency"][model]["points"]
        out += [f"Forward-pass latency through a worker process, {model.upper()}:", ""]
        out += _table(
            ["Batch", "p50 (ms)", "p99 (ms)", "Peak memory (MiB)", "Samples"],
            [
                [
                    str(p["batch_size"]),
                    _fmt(p["p50_ms"]),
                    _fmt(p["p99_ms"]),
                    str(p["memory_mib"]),
                    str(p["samples"]),
                ]
                for p in points
            ],
        )
        out.append("")
    contention = measured["contention"]
    out += [
        "Co-location: k workers on the device, each running batches of one back to back. "
        f"Alone: p50 {contention['alone_p50_ms']} ms. A background job (matrix multiplications) "
        f"slows a lone worker by {contention['background_slowdown']}x.",
        "",
    ]
    out += _table(
        ["Workers", "Slowdown", "p50 (ms)", "p99 (ms)"],
        [
            [
                k,
                _fmt(v),
                _fmt(contention["by_workers"][k]["p50_ms"]),
                _fmt(contention["by_workers"][k]["p99_ms"]),
            ]
            for k, v in contention["slowdown"].items()
        ],
    )
    calibration = measured.get("calibration")
    if calibration:
        out += [
            "",
            "Simulated link, calibrated from one robot alone in real time: observation age at "
            f"dispatch p50 {calibration['real_age_at_dispatch_p50_ms']} ms, p99 "
            f"{calibration['real_age_at_dispatch_p99_ms']} ms (robot encoding, gRPC and the "
            f"gateway's checks), simulated as {calibration['link_base_ms']} ms plus up to "
            f"{calibration['link_jitter_ms']} ms.",
        ]
    multi = measured["multi_adapter"]
    parity = multi["parity"]
    out += [
        "",
        f"Multi-adapter batching: {multi['adapters']} rank-{multi['rank']} LoRA adapters on "
        f"{multi['adapted_layers']} linear layers of the ACT base model; every request in a "
        "batch uses a different adapter. Parity with each adapter merged and run alone: max "
        f"relative error {parity['max_rel_error']:.2e} (tolerance {parity['tolerance']:.0e}), "
        f"{'passed' if parity['passed'] else 'FAILED'}.",
        "",
    ]
    out += _table(
        ["Batch", "Base p50 (ms)", "Mixed adapters p50 (ms)", "Overhead"],
        [
            [
                k,
                _fmt(multi["base_p50_ms"][k]),
                _fmt(multi["mixed_p50_ms"][k]),
                _fmt(multi["overhead"][k]),
            ]
            for k in multi["base_p50_ms"]
        ],
    )
    estimate = data["admission_estimate"]
    measured_capacity = (
        capacity.get(suite.SAME_WEIGHTS, {}).get(suite.SYNCHRONIZED, {}).get(suite.NEURA_DEADLINE)
    )
    out += [
        "",
        f"Admission control's estimate for ACT on this device: {estimate['max_robots']} robots "
        f"(limited by {estimate['limiting_factor']}), against "
        f"{measured_capacity['robots_per_device'] if measured_capacity else '-'} measured with "
        "synchronized arrivals. Admission assumes the worst case (every robot asks at once, "
        "behind a batch already running), so it must not exceed the synchronized capacity: "
        f"{_admission_verdict(estimate, measured_capacity)}",
        "",
        "## Results",
        "",
        "Per 1,000 requests: deadline misses (served after the predicted starvation time or "
        "rejected by the robot as too old, too old to dispatch, or failed). Per robot-hour: "
        "starvation events and faults. Age: from the robot's capture to the start of the "
        "forward pass.",
        "",
    ]
    cells = data["cells"]
    for case in suite.CASES:
        for workload in suite.WORKLOADS[case]:
            rows = []
            for cell in cells:
                if cell["case"] != case or cell["workload"] != workload:
                    continue
                r = cell["result"]
                rows.append(
                    [
                        CONFIG_NAMES[cell["configuration"]],
                        str(cell["robots"]),
                        _fmt(r["misses_per_1000"]),
                        _fmt(r["starvations_per_robot_hour"], 1),
                        _fmt(r["faults_per_robot_hour"], 1),
                        _fmt(r["late_trimmed_per_1000_actions"], 1),
                        f"{_fmt(r['age_at_dispatch_p50_ms'], 1)} / "
                        f"{_fmt(r['age_at_dispatch_p99_ms'], 1)} / "
                        f"{_fmt(r['age_at_dispatch_max_ms'], 1)}",
                        _fmt(r["mean_batch"]),
                        ", ".join(f"{v:.0%}" for v in r["utilization"].values()),
                        _fmt(r["gpu_hours_per_robot_hour"], 3),
                        str(r["peak_memory_mib"]),
                    ]
                )
            out += [f"### {CASE_NAMES[case]}: {WORKLOAD_NAMES[workload]}", ""]
            out += _table(
                [
                    "Configuration",
                    "Robots",
                    "Misses / 1k",
                    "Starvation / robot-h",
                    "Faults / robot-h",
                    "Late-trimmed / 1k actions",
                    "Age p50 / p99 / max (ms)",
                    "Mean batch",
                    "Device busy",
                    "GPU-h / robot-h",
                    "Peak MiB",
                ],
                rows,
            )
            out.append("")
    out += [
        "## Real-time validation",
        "",
        "The production gateway with real worker processes on the device and simulated robots "
        "over gRPC, next to the simulation of the same configuration (same robot count, warmup "
        "and window). Each cell is real / simulated. With a few hundred requests a p99 is close "
        "to the maximum: one collision moves it, so read it with the median and the miss rate. "
        "Late ticks: robot control ticks the harness itself ran late (high values mean the "
        "harness, not Neura, was overloaded).",
        "",
    ]
    rows = []
    for v in data["validation"]:
        real, sim = v["real"]["result"], v["simulated"]
        rows.append(
            [
                f"{CASE_NAMES[v['case']]}: {WORKLOAD_NAMES[v['workload']]}",
                CONFIG_NAMES[v["configuration"]],
                str(v["robots"]),
                f"{real['requests']} / {sim['requests']}",
                f"{_fmt(real['misses_per_1000'])} / {_fmt(sim['misses_per_1000'])}",
                f"{_fmt(real['starvations_per_robot_hour'], 1)} / "
                f"{_fmt(sim['starvations_per_robot_hour'], 1)}",
                f"{_fmt(real['age_at_dispatch_p50_ms'], 1)} / "
                f"{_fmt(sim['age_at_dispatch_p50_ms'], 1)}",
                f"{_fmt(real['age_at_dispatch_p99_ms'], 1)} / "
                f"{_fmt(sim['age_at_dispatch_p99_ms'], 1)}",
                f"{_fmt(real['mean_batch'])} / {_fmt(sim['mean_batch'])}",
                f"{', '.join(f'{x:.0%}' for x in real['utilization'].values())} / "
                f"{', '.join(f'{x:.0%}' for x in sim['utilization'].values())}",
                f"{v['real']['late_tick_fraction']:.1%}",
                str(v["real"]["worker_restarts"]),
            ]
        )
    out += _table(
        [
            "Case",
            "Configuration",
            "Robots",
            "Requests",
            "Misses / 1k",
            "Starvation / robot-h",
            "Age p50 ms",
            "Age p99 ms",
            "Mean batch",
            "Device busy",
            "Late ticks",
            "Worker restarts",
        ],
        rows,
    )
    out += [
        "",
        "## Method and limits",
        "",
        "- Configurations follow design §10: the dedicated baselines run batch size 1 with the "
        "same model, precision, observation encoding and harness; Neura runs one worker per "
        "batch group with the scheduler policy named.",
        "- Latency is measured through `neura-worker` processes over their Unix sockets, so "
        "serialization and IPC are included. Simulation draws forward-pass times from these "
        "measurements (lognormal fitted to p50 and p99) and shares the device between running "
        "passes by the measured co-location slowdown; beyond "
        f"{config['contention_workers']} concurrent workers the slowdown is extrapolated "
        "linearly.",
        "- Multi-adapter batching is measured at the forward pass and simulated at the serving "
        "level; the data plane does not group robots by base model yet (design §9.5: built "
        "only if this benchmark shows it pays). Measured overheads below 1 are timing noise and "
        "are not credited.",
        "- Simulated robots tick with no timing noise, and the simulated gateway decodes an "
        "observation in no time. Real robots drift by milliseconds, so near-simultaneous "
        "requests sometimes miss the batching window: the simulation is somewhat optimistic for "
        "synchronized arrivals near a freshness limit (compare the validation table).",
        "- On a CPU host the background job and the worker share the CPU with the gateway and "
        "the simulated robots, so real ages there include the harness slowing down; the "
        "simulation models the device only. On a GPU the background job competes for the GPU "
        "alone.",
        "- Admission control is set aside in the benchmark so it can measure what N robots "
        "get; production refuses sessions beyond the admission estimate.",
        "- Robots that latch a fault are reset by a simulated operator after one second and "
        "counted.",
        "",
        "## Reproduce",
        "",
        "```",
        env["command"],
        "```",
        "",
    ]
    return "\n".join(out)


def _admission_verdict(estimate: dict[str, Any], measured: dict[str, Any] | None) -> str:
    if measured is None:
        return "not measured."
    promised, served = estimate["max_robots"], measured["robots_per_device"]
    if promised > served:
        return f"it does ({promised} > {served}), so admission is optimistic on this device."
    return f"it does not ({promised} <= {served})."


def _cameras(image: list[int] | None) -> str:
    return "640 x 480" if image is None else f"{image[1]} x {image[0]}"


def _capacity_cell(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "-"
    n = entry["robots_per_device"]
    if not n:
        return "0 (fails at 1)"
    if entry["first_failure"] is None:
        return f">= {n} (no failure measured)" if n > 1 else str(n)
    return f"{n}, fails at {entry['first_failure']}"


def _best_neura(configurations: dict[str, dict[str, Any]]) -> str | None:
    """The Neura configuration with the most robots per device; ties go to the default
    policy (deadline + fairness)."""
    neura = [c for c in configurations if c.startswith("neura")]
    if not neura:
        return None
    return max(
        neura,
        key=lambda c: (configurations[c]["robots_per_device"], c == suite.NEURA_DEADLINE),
    )


def _crossovers(capacity: dict[str, Any]) -> list[str]:
    lines = []
    for case, workloads in capacity.items():
        configurations = workloads.get(suite.PHASED) or next(iter(workloads.values()))
        best = _best_neura(configurations)
        if best is None:
            continue
        n = configurations[best]["robots_per_device"]
        shared = configurations.get(suite.DEDICATED_SHARED, {}).get("robots_per_device", 0)
        failure = configurations[best]["first_failure"]
        if n >= 2:
            beyond = (
                f"; at {failure} robots it misses the SLO, so the crossover where one device "
                f"stops paying is between {n} and {failure}"
                if failure
                else "; it met the SLO at every robot count measured"
            )
            verdict = (
                "sharing pays, and Neura's batching serves the most robots"
                if n > shared
                else "sharing a device pays, but here one process per robot serves as many or "
                f"more ({shared} against {n}): on this device batching does not beat "
                "co-location"
            )
            lines.append(
                f"- **{CASE_NAMES[case]}:** {verdict}. {CONFIG_NAMES[best]} serves {n} robots "
                f"per device ({1 / n:.3f} device-hours per robot-hour, against 1 with a device "
                f"per robot){beyond}."
            )
        else:
            lines.append(
                f"- **{CASE_NAMES[case]}:** sharing does not pay on this device: "
                f"{CONFIG_NAMES[best]} serves {n} robot(s) at the SLO."
            )
    return lines


def write(directory: Path, data: dict[str, Any]) -> tuple[Path, Path]:
    import json

    directory.mkdir(parents=True, exist_ok=True)
    results = directory / "results.json"
    results.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n")
    report = directory / "README.md"
    report.write_text(markdown(data))
    return results, report
