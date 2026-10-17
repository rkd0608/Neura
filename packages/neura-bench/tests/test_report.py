"""Reports render from results.json (a fixture from `neura-bench run --preset smoke`)."""

from __future__ import annotations

import json
from pathlib import Path

from neura_bench import report
from neura_bench.cli import main

FIXTURE = Path(__file__).with_name("fixtures") / "smoke-results.json"


def test_the_report_states_capacity_crossovers_and_its_limits() -> None:
    data = json.loads(FIXTURE.read_text())
    markdown = report.markdown(data)
    assert markdown.startswith("# Neura GPU-sharing benchmark: CPU")
    assert "not a GPU" in markdown  # a CPU run says so up front
    for heading in (
        "## Robots per device at the SLO",
        "## Measurements on the device",
        "## Results",
        "## Real-time validation",
        "## Method and limits",
        "## Reproduce",
    ):
        assert heading in markdown
    for case in data["capacity"]:
        assert f"**{report.CASE_NAMES[case]}:**" in markdown  # a crossover statement per case
    assert "SmolVLA" not in markdown  # a case whose model was not measured has no table
    assert "Parity with each adapter merged" in markdown


def test_report_command_regenerates_the_markdown(tmp_path: Path) -> None:
    (tmp_path / "results.json").write_text(FIXTURE.read_text())
    assert main(["report", str(tmp_path)]) == 0
    assert (tmp_path / "README.md").read_text() == report.markdown(json.loads(FIXTURE.read_text()))


def test_labels_are_directory_names() -> None:
    assert report.label("cpu").startswith("cpu-")
    assert "/" not in report.label("cpu")


def test_a_vla_run_states_its_timing_and_each_models_contention() -> None:
    data = json.loads(FIXTURE.read_text())
    data["measurements"]["latency"]["smolvla"] = data["measurements"]["latency"]["act"]
    data["measurements"]["vla_contention"] = {"smolvla": data["measurements"]["contention"]}
    data["config"]["vla_timing"] = {
        "actions_per_chunk": 50,
        "refill_at": 30,
        "min_useful_actions": 8,
        "dispatch_ms": 600,
        "acceptance_ms": 1500,
    }
    markdown = report.markdown(data)
    assert "chunks of 50 actions, the next requested at 30 left" in markdown
    assert "1500 ms when the robot accepts its chunk" in markdown
    assert "Co-location of SmolVLA workers (simulated with this, not ACT's)" in markdown


def test_a_gpu_run_without_the_workers_memory_says_what_it_counted() -> None:
    data = json.loads(FIXTURE.read_text())
    data["environment"]["device"] = "cuda:0"
    note = "The driver reported no device memory for the worker processes of ACT"
    assert note in report.markdown(data)
    for latency in data["measurements"]["latency"].values():
        latency["process_overhead_source"] = "device"
    assert note not in report.markdown(data)
    assert note not in report.markdown(json.loads(FIXTURE.read_text()))  # a CPU has no driver
