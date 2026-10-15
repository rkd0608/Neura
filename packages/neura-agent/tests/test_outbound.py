"""What leaves the site (design §18.2): the published outbound schema, the telemetry modes, and the
outbound ledger that records exactly what was sent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.descriptor import Descriptor

from neura.v1 import common_pb2, site_pb2
from neura_agent.__main__ import main
from neura_agent.ledger import OutboundLedger
from neura_agent.outbound import Outbound, filter_message, filter_result
from neura_contracts.outbound_schema import MESSAGES, RESULTS


def leaves(descriptor: Descriptor, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for field in descriptor.fields:
        path = f"{prefix}{field.name}"
        nested = field.message_type
        if nested is not None and not nested.GetOptions().map_entry:
            out |= leaves(nested, f"{path}.")
        else:
            out.add(path)
    return out


@pytest.mark.parametrize(
    ("kind", "descriptor"),
    [
        ("agent", site_pb2.AgentMessage.DESCRIPTOR),
        ("enroll", site_pb2.EnrollRequest.DESCRIPTOR),
        ("fetch_sealed_weights", site_pb2.FetchSealedWeightsRequest.DESCRIPTOR),
    ],
)
def test_every_outbound_field_is_published_and_every_published_field_exists(
    kind: str, descriptor: Descriptor
) -> None:
    """A field added to the protocol is never sent until it is classified in the schema."""
    assert leaves(descriptor) == set(MESSAGES[kind])


def status(**overrides: Any) -> site_pb2.AgentMessage:
    reason = common_pb2.Reason(code="WORKER_TIMED_OUT", message="worker 3 on /dev/nvidia0")
    return site_pb2.AgentMessage(
        status=site_pb2.SiteStatus(
            agent_time_us=1,
            sessions=[
                site_pb2.RobotSessionStatus(
                    session_id="ses_1",
                    robot_name="arm-1",
                    revision_id="rev_1",
                    state="Running",
                    reason=reason,
                    metrics={"chunks": 12.0},
                )
            ],
            faults=[site_pb2.FaultReport(fault_key="k", reason=reason, latched=True)],
            contact_nonce=b"n" * 32,
            **overrides,
        )
    )


def test_standard_mode_sends_every_published_field() -> None:
    message = status()
    filtered = filter_message(message, "agent", "standard")
    assert filtered.message == message
    assert filtered.withheld == ()
    assert filtered.sendable


def test_minimal_mode_withholds_free_text_and_metrics() -> None:
    filtered = filter_message(status(), "agent", "minimal")
    session = filtered.message.status.sessions[0]
    assert session.reason.code == "WORKER_TIMED_OUT"
    assert session.reason.message == ""
    assert dict(session.metrics) == {}
    assert filtered.message.status.faults[0].reason.message == ""
    assert filtered.message.status.contact_nonce == b"n" * 32  # offline authorization needs it
    assert set(filtered.withheld) == {
        "status.sessions.reason.message",
        "status.sessions.metrics",
        "status.faults.reason.message",
    }
    telemetry = site_pb2.AgentMessage(
        telemetry=site_pb2.TelemetryBatch(
            schema_version="1",
            metrics=[site_pb2.MetricPoint(name="robots_per_gpu", value=2)],
        )
    )
    assert not filter_message(telemetry, "agent", "minimal").sendable
    facts = site_pb2.EnrollRequest(
        enrollment_token="nen_x",
        facts=site_pb2.ServerFacts(
            hostname="gpu-07.plant.example",
            driver_version="550",
            gpus=[site_pb2.GpuInfo(index=0, uuid="GPU-1234", model="L40S")],
            preflight=[site_pb2.PreflightResult(name="disk", passed=True, detail="/var 80%")],
        ),
    )
    sent = filter_message(facts, "enroll", "minimal").message
    assert sent.facts.hostname == ""
    assert sent.facts.gpus[0].uuid == ""
    assert sent.facts.gpus[0].model == "L40S"
    assert sent.facts.preflight[0].detail == ""
    assert sent.enrollment_token == "nen_x"


def test_operation_results_carry_only_published_keys() -> None:
    result = {
        "approved_by": "alice",
        "revision_id": "rev_1",
        "robot_ids": ["rbt_1"],
        "capacity": {
            "max_robots": 8,
            "assumptions": {"latency_p99_ms_by_batch": {"1": 20.0, "4": 31.5}, "device": "cuda:0"},
        },
        "assignments": {"arm-1": {"current": "rev_1", "next": None}},
        "checkpoint_path": "/var/lib/neura/artifacts/checkouts/abc",
    }
    standard, withheld, unknown = filter_result(result, "standard")
    assert "checkpoint_path" not in standard
    assert unknown == ["checkpoint_path"]
    assert standard["capacity"]["assumptions"]["latency_p99_ms_by_batch"] == {"1": 20.0, "4": 31.5}
    assert withheld == []
    minimal, withheld, _ = filter_result(result, "minimal")
    assert minimal == {
        "revision_id": "rev_1",
        "robot_ids": ["rbt_1"],
        "capacity": {"max_robots": 8},
        "assignments": {"arm-1": {"current": "rev_1", "next": None}},
    }
    assert "approved_by" in withheld
    message = site_pb2.AgentMessage(
        operation=site_pb2.OperationReport(
            operation_id="opr_1", state="Succeeded", result_json=json.dumps(result).encode()
        )
    )
    sent = filter_message(message, "agent", "minimal")
    assert json.loads(sent.message.operation.result_json) == minimal
    assert "operation.result_json:approved_by" in sent.withheld
    assert "operation.result_json:checkpoint_path" in sent.unknown


def test_telemetry_metrics_and_labels_outside_the_catalog_are_dropped() -> None:
    message = site_pb2.AgentMessage(
        telemetry=site_pb2.TelemetryBatch(
            metrics=[
                site_pb2.MetricPoint(name="robots_per_gpu", value=2, labels={"host": "gpu-07"}),
                site_pb2.MetricPoint(name="camera_frames", value=9),
            ]
        )
    )
    filtered = filter_message(message, "agent", "standard")
    points = filtered.message.telemetry.metrics
    assert [p.name for p in points] == ["robots_per_gpu"]
    assert dict(points[0].labels) == {}
    assert set(filtered.unknown) == {
        "telemetry.metrics:robots_per_gpu.host",
        "telemetry.metrics:camera_frames",
    }


def test_every_result_key_the_agent_reports_is_published() -> None:
    """The keys of every result the agent builds today (prepare, activation, diagnostics)."""
    for key in ("revision_digest", "capacity.max_robots", "approved_by", "manifest_generation"):
        assert key in RESULTS


# --- the ledger -----------------------------------------------------------------------------


def test_the_ledger_records_what_was_sent_and_detects_tampering(tmp_path: Path) -> None:
    gate = Outbound(OutboundLedger(tmp_path / "outbound"), "minimal")
    sent = gate.prepare("agent", status())
    assert sent is not None
    gate.prepare("agent", site_pb2.AgentMessage(hello=site_pb2.AgentHello(site_id="sit_1")))
    assert gate.prepare("agent", site_pb2.AgentMessage(telemetry=site_pb2.TelemetryBatch())) is None
    entries = gate.ledger.entries()
    assert [e["kind"] for e in entries] == ["hello", "status"]
    recorded = entries[1]
    assert recorded["bytes"] == len(sent.SerializeToString())
    assert recorded["message"]["status"]["sessions"][0]["robot_name"] == "arm-1"
    assert "status.sessions.metrics" in recorded["withheld"]
    assert gate.ledger.summary()["status"]["messages"] == 1
    assert gate.ledger.verify().ok
    # Edit one entry: its chain no longer matches.
    path = next((tmp_path / "outbound").glob("ledger-*.jsonl"))
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0].replace("arm-1", "arm-9"), lines[1]]) + "\n")
    assert not gate.ledger.verify().ok
    # Remove one entry: the next one's link breaks.
    path.write_text("\n".join([lines[1]]) + "\n")
    assert gate.ledger.verify().ok  # the oldest kept entry anchors the chain
    gate2 = Outbound(OutboundLedger(tmp_path / "other"), "standard")
    for i in range(3):
        gate2.prepare("agent", site_pb2.AgentMessage(hello=site_pb2.AgentHello(site_id=f"s{i}")))
    other = next((tmp_path / "other").glob("ledger-*.jsonl"))
    kept = other.read_text().splitlines()
    other.write_text("\n".join([kept[0], kept[2]]) + "\n")
    assert not gate2.ledger.verify().ok


def test_the_ledger_resumes_its_chain_and_rotates(tmp_path: Path) -> None:
    ledger = OutboundLedger(tmp_path / "outbound", file_bytes=2000, max_bytes=6000)
    message = site_pb2.AgentMessage(hello=site_pb2.AgentHello(site_id="sit_1"))
    for _ in range(5):
        ledger.record("stream", message)
    resumed = OutboundLedger(tmp_path / "outbound", file_bytes=2000, max_bytes=6000)
    for _ in range(40):
        resumed.record("stream", message)
    files = list((tmp_path / "outbound").glob("ledger-*.jsonl"))
    assert len(files) <= 3
    assert resumed.verify().ok
    seqs = [e["seq"] for e in resumed.entries(limit=1000)]
    assert seqs[0] == 45
    assert seqs == sorted(seqs, reverse=True)


def test_secrets_are_kept_as_digests_only(tmp_path: Path) -> None:
    gate = Outbound(OutboundLedger(tmp_path / "outbound"), "standard")
    request = site_pb2.EnrollRequest(enrollment_token="nen_secret", agent_version="1")
    sent = gate.prepare("enroll", request)
    assert sent is not None
    assert sent.enrollment_token == "nen_secret"  # sent as is
    entry = gate.ledger.entries()[0]
    assert entry["kind"] == "enroll"
    assert entry["message"]["enrollment_token"].startswith("redacted sha256:")
    assert "nen_secret" not in json.dumps(entry)


def test_the_cli_shows_and_verifies_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEURA_AGENT_STATE_DIR", str(tmp_path))
    gate = Outbound(OutboundLedger(tmp_path / "outbound"), "standard")
    gate.prepare("agent", status())
    gate.prepare("agent", site_pb2.AgentMessage(hello=site_pb2.AgentHello(site_id="sit_1")))
    assert main(["outbound", "--kind", "status"]) == 0
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["kind"] for e in printed] == ["status"]
    assert main(["outbound", "--summary"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"hello", "status"}
    assert main(["outbound", "--verify"]) == 0
    assert "hash chain intact" in capsys.readouterr().out


def test_processes_sharing_the_ledger_extend_one_chain(tmp_path: Path) -> None:
    """The running agent opens its ledger before `neura-agent enroll` (another process) writes
    to it: both must chain to what is on disk, not to what they saw when they opened it."""
    import subprocess
    import sys

    agent = OutboundLedger(tmp_path / "outbound")
    enroll = OutboundLedger(tmp_path / "outbound")
    hello = site_pb2.AgentMessage(hello=site_pb2.AgentHello(site_id="sit_1"))
    enroll.record("enroll", site_pb2.EnrollRequest(agent_version="1"))
    agent.record("stream", hello)
    enroll.record("stream", hello)
    agent.record("stream", hello)
    script = (
        "import sys; from pathlib import Path\n"
        "from neura.v1 import site_pb2\n"
        "from neura_agent.ledger import OutboundLedger\n"
        "ledger = OutboundLedger(Path(sys.argv[1]))\n"
        "m = site_pb2.AgentMessage(hello=site_pb2.AgentHello(site_id='sit_1'))\n"
        "for _ in range(25): ledger.record('stream', m)\n"
    )
    workers = [
        subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "outbound")])
        for _ in range(4)
    ]
    assert all(w.wait(timeout=60) == 0 for w in workers)
    assert agent.verify().ok
    seqs = sorted(e["seq"] for e in agent.entries(limit=1000))
    assert seqs == list(range(1, 105))
