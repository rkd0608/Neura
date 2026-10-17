"""``neura-bench``: the GPU-sharing benchmark (design §9.5, §10).

neura-bench run [--preset gpu|cpu|smoke|smoke-vla] [--device cuda:0] [--out DIR]
    build the models (ACT, Diffusion Policy, and on a GPU SmolVLA and pi0), measure on the
    device, simulate every sharing case against the dedicated baselines, validate in real time,
    and write results.json and README.md (default: bench/reports/<device>/). On a rented L4:
    deploy/gcp/bench.sh run or deploy/modal/bench.py, or Actions, then GPU benchmark (Google
    Cloud or Modal).
neura-bench report DIR
    regenerate DIR/README.md from DIR/results.json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from neura_bench import report, suite

ROOT = Path(__file__).resolve().parents[4]
log = logging.getLogger("neura.bench")


async def run(config: suite.SuiteConfig, out: Path | None) -> Path:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="neura-bench-") as tmp:
        log.info("building synthetic models (%s)", config.size)
        models = suite.build_models(config, Path(tmp))
        measured = await suite.take_measurements(config, models)
        builder = suite.Builder(config, models, measured)
        calibration = await suite.calibrate(builder)
        cells = await asyncio.to_thread(suite.sweep, builder)
        validation = await suite.validate(builder)
        estimate = suite.admission_estimate(config, models, measured)
        estimates_vla = suite.admission_estimates_vla(config, models, measured)
    # The builder's config: its SLO includes the device's memory.
    config = builder.config
    data: dict[str, Any] = {
        "schema": 1,
        "environment": report.environment(config.device),
        "config": dataclasses.asdict(config),
        "measurements": {**measured.to_json(), "calibration": calibration.to_json()},
        "admission_estimate": estimate,
        "admission_estimates_vla": estimates_vla,
        "cells": [
            {
                "case": c.case,
                "workload": c.workload,
                "configuration": c.configuration,
                "robots": c.robots,
                "result": c.result.to_json(),
            }
            for c in cells
        ],
        "capacity": suite.capacity_at_slo(cells, config.slo),
        "validation": [
            {
                "case": v.case,
                "workload": v.workload,
                "configuration": v.configuration,
                "robots": v.robots,
                "real": {
                    "result": v.real.result.to_json(),
                    "late_tick_fraction": v.real.late_tick_fraction,
                    "worker_restarts": v.real.worker_restarts,
                },
                "simulated": v.simulated.to_json(),
            }
            for v in validation
        ],
        "elapsed_s": round(time.monotonic() - started, 1),
    }
    directory = out or ROOT / "bench" / "reports" / report.label(config.device)
    results, markdown = report.write(directory, data)
    log.info("wrote %s and %s in %.0f s", results, markdown, data["elapsed_s"])
    return directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-bench",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--preset", choices=sorted(suite.PRESETS), default="gpu")
    run_parser.add_argument("--device", default=None, help="override the preset's device")
    run_parser.add_argument("--size", choices=["tiny", "small", "base"], default=None)
    run_parser.add_argument("--precision", choices=["fp32", "fp16", "bf16"], default=None)
    run_parser.add_argument("--out", type=Path, default=None)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    for noisy in ("neura.gateway", "neura.gateway.workers", "neura.worker"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.command == "report":
        data = json.loads((args.directory / "results.json").read_text())
        (args.directory / "README.md").write_text(report.markdown(data))
        return 0
    config = suite.PRESETS[args.preset]
    overrides = {
        key: value
        for key, value in (
            ("device", args.device),
            ("size", args.size),
            ("precision", args.precision),
        )
        if value is not None
    }
    config = dataclasses.replace(config, **overrides)
    asyncio.run(run(config, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
