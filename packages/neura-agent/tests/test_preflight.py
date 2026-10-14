"""Preflight: what the agent learns about its server and how to fix what fails (design §17.2)."""

from __future__ import annotations

from pathlib import Path

from neura_agent.preflight import NVIDIA_SMI_QUERY, Runner, collect_facts, parse_nvidia_smi

SMI = """\
0, GPU-aaaa, NVIDIA L4, 23034, Disabled, 550.54.15
1, GPU-bbbb, NVIDIA A100-SXM4-80GB, 81920, Enabled, 550.54.15
not, a, gpu, line
"""
SMI_SUMMARY = "| NVIDIA-SMI 550.54.15   Driver Version: 550.54.15   CUDA Version: 12.4     |"


def runner(responses: dict[str, str | None]) -> Runner:
    def run(args: list[str]) -> str | None:
        return responses.get("query" if args == NVIDIA_SMI_QUERY else " ".join(args))

    return run


def test_parse_nvidia_smi_skips_malformed_lines() -> None:
    gpus = parse_nvidia_smi(SMI)
    assert [(g.index, g.model, g.memory_mib, g.mig_enabled) for g in gpus] == [
        (0, "NVIDIA L4", 23034, False),
        (1, "NVIDIA A100-SXM4-80GB", 81920, True),
    ]


def test_facts_report_gpus_driver_and_cuda(tmp_path: Path) -> None:
    facts = collect_facts(
        state_dir=tmp_path,
        require_gpu=True,
        min_free_disk_mib=0,
        allowed_gpu_indexes=[1],
        runner=runner({"query": SMI, "nvidia-smi": SMI_SUMMARY}),
    )
    assert (facts.driver_version, facts.cuda_version) == ("550.54.15", "12.4")
    assert [g.uuid for g in facts.gpus] == ["GPU-aaaa", "GPU-bbbb"]
    checks = {c.name: c for c in facts.preflight}
    assert checks["nvidia_driver"].passed
    assert checks["allowed_gpus_present"].passed
    assert checks["state_dir_writable"].passed
    assert checks["free_disk"].passed


def test_failed_checks_say_how_to_fix_them(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    facts = collect_facts(
        state_dir=blocked / "state",
        require_gpu=True,
        min_free_disk_mib=0,
        allowed_gpu_indexes=[3],
        runner=runner({}),
    )
    failed = {c.name: c for c in facts.preflight if not c.passed}
    assert set(failed) == {"nvidia_driver", "allowed_gpus_present", "state_dir_writable"}
    assert all(c.fix and c.reason == "AGENT_PREFLIGHT_FAILED" for c in failed.values())
    assert "[3]" in failed["allowed_gpus_present"].fix


def test_gpu_is_optional_when_not_required(tmp_path: Path) -> None:
    facts = collect_facts(
        state_dir=tmp_path,
        require_gpu=False,
        min_free_disk_mib=0,
        allowed_gpu_indexes=None,
        runner=runner({}),
    )
    driver = next(c for c in facts.preflight if c.name == "nvidia_driver")
    assert driver.passed
    assert "not required" in driver.detail
    assert not facts.gpus


def test_insufficient_disk_fails(tmp_path: Path) -> None:
    facts = collect_facts(
        state_dir=tmp_path,
        require_gpu=False,
        min_free_disk_mib=10**12,
        allowed_gpu_indexes=None,
        runner=runner({}),
    )
    assert not next(c for c in facts.preflight if c.name == "free_disk").passed
