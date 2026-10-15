"""The site agent's decisions, without a network: signed desired state, commands, local approval,
task-boundary switches, offline authorization and restart recovery (design §13, §16, §17)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from neura.v1 import site_pb2
from neura_agent.core import AgentCore, LocalActionError
from neura_agent.gateway import build_gateway_config
from neura_agent.identity import AUDIENCE, Identity, IdentityStore, atomic_write, connection_token
from neura_agent.settings import AgentSettings
from neura_agent.state import AgentState, StateStore
from neura_contracts import (
    ReasonCode,
    canonical_json,
    content_digest,
    parse_document,
    pki,
    signing,
)
from neura_contracts.site_protocol import MANIFEST_API_VERSION, contact_proof_payload

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"
SITE = "sit_01J0000000000000000000000S"
ROBOTS = {"rbt_01J000000000000000000000R0": "robot-0", "rbt_01J000000000000000000000R1": "robot-1"}
KEY_ID = "mfk_test"
# The customer's release key: every test artifact is signed with it and the site trusts it.
PUBLISHER = Ed25519PrivateKey.generate()
PUBLISHER_HEX = signing.raw_public_key(PUBLISHER).hex()


def _example(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))
    return data


def revision(
    revision_id: str,
    refill: int | None = None,
    *,
    artifact: dict[str, Any] | None = None,
    publishers: tuple[Ed25519PrivateKey, ...] = (PUBLISHER,),
) -> dict[str, Any]:
    profile = _example("act-so101-30hz.profile.yaml")
    if refill is not None:
        profile["metadata"]["version"] = f"refill-{refill}"
        profile["spec"]["chunk"]["refill_threshold_actions"] = refill
    if artifact is not None:
        profile["spec"]["binding"]["model_artifact"] = parse_document(artifact).digest()
    raw = {
        "model_artifact": artifact or _example("act-so101.artifact.yaml"),
        "robot_profile": _example("so101.robot.yaml"),
        "execution_profile": profile,
    }
    documents: dict[str, dict[str, Any]] = {
        role: {"id": f"doc_{role}", "digest": parse_document(doc).digest(), "document": doc}
        for role, doc in raw.items()
    }
    artifact_entry = documents["model_artifact"]
    artifact_entry["signatures"] = [signing.sign(artifact_entry["digest"], k) for k in publishers]
    digest = content_digest({role: entry["digest"] for role, entry in documents.items()})
    return {"id": revision_id, "revision_digest": digest, "documents": documents}


REV1 = revision("rev_01J0000000000000000000001")
REV2 = revision("rev_01J0000000000000000000002", refill=25)


class FakeGateway:
    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.reloads = 0
        self.stops = 0
        self.sessions: list[dict[str, Any]] = []

    async def start(self, config_path: Path) -> None:
        self.running, self.starts = True, self.starts + 1

    async def reload(self) -> None:
        self.reloads += 1

    async def stop(self) -> None:
        self.running, self.stops = False, self.stops + 1

    async def status(self) -> dict[str, Any] | None:
        return {"sessions": self.sessions, "groups": {}} if self.running else None


def serving(site: Site) -> bool:
    return site.gateway.running


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


class Site:
    """The control plane's side, reduced to what the agent sees: signed manifests and commands."""

    def __init__(self, tmp_path: Path, **settings: Any) -> None:
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.identity = Identity(
            server_id="srv_01J0000000000000000000000V",
            site_id=SITE,
            project_id="prj_01J0000000000000000000000P",
            endpoint="127.0.0.1:1",
            manifest_public_key_hex=public.hex(),
            manifest_key_id=KEY_ID,
        )
        self.settings = AgentSettings(
            state_dir=tmp_path / "agent",
            **{
                "gateway_backend": "simulated",
                "trusted_publisher_keys": [PUBLISHER_HEX],
                "worker_sandbox": "process",
                **settings,
            },
        )
        self.gateway = FakeGateway()
        self.clock = Clock()
        self.cuda: list[int] = []  # GPUs the agent may use
        self.generation = 0
        self.commands = 0
        self.core = self.new_core()

    def new_core(self) -> AgentCore:
        return AgentCore(
            self.settings,
            self.identity,
            self.gateway,
            clock=self.clock,
            cuda_indexes=self.cuda.copy,
        )

    def manifest(self, revisions: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
        self.generation += 1
        return {
            "api_version": MANIFEST_API_VERSION,
            "site_id": SITE,
            "project_id": self.identity.project_id,
            "offline_authorization_hours": 1,
            "generation": self.generation,
            "robots": [{"id": rid, "name": name} for rid, name in ROBOTS.items()],
            "deployments": [
                {"id": "dep_1", "robot_ids": list(ROBOTS), "revisions": copy.deepcopy(revisions)}
            ],
            **overrides,
        }

    def desired(
        self,
        manifest: dict[str, Any],
        *,
        key: Ed25519PrivateKey | None = None,
        key_id: str = KEY_ID,
    ) -> site_pb2.DesiredState:
        payload = canonical_json(manifest)
        return site_pb2.DesiredState(
            generation=manifest["generation"],
            manifest=payload,
            signature=site_pb2.Signature(key_id=key_id, value=(key or self.key).sign(payload)),
        )

    async def commit(self, revisions: list[dict[str, Any]]) -> site_pb2.Acknowledgement:
        message = await self.core.on_desired_state(self.desired(self.manifest(revisions)))
        return message.ack

    def command(self, **kind: Any) -> site_pb2.Command:
        self.commands += 1
        return site_pb2.Command(
            command_id=f"cmd_{self.commands}", operation_id=f"opr_{self.commands}", **kind
        )

    def prepare(self, revision_id: str) -> site_pb2.Command:
        return self.command(prepare=site_pb2.PrepareRevision(revision_id=revision_id))

    def activate(self, revision_id: str) -> site_pb2.Command:
        return self.command(
            request_activation=site_pb2.RequestActivation(
                revision_id=revision_id, robot_ids=list(ROBOTS)
            )
        )

    def drain(self) -> list[site_pb2.AgentMessage]:
        messages = []
        while not self.core.outbox.empty():
            messages.append(self.core.outbox.get_nowait())
        return messages

    def reports(self) -> list[site_pb2.OperationReport]:
        return [m.operation for m in self.drain() if m.WhichOneof("body") == "operation"]

    def running(self, revision_id: str) -> None:
        self.gateway.sessions = [
            {"robot_id": name, "revision_id": revision_id, "session_id": f"ses_{name}"}
            for name in ROBOTS.values()
        ]


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


async def prepared(site: Site, *revisions: dict[str, Any]) -> None:
    assert (await site.commit(list(revisions))).accepted
    for rev in revisions:
        await site.core.on_command(site.prepare(rev["id"]))
    assert all(r.state != "Failed" for r in site.reports())


async def activated(site: Site, revision_id: str) -> site_pb2.Command:
    command = site.activate(revision_id)
    await site.core.on_command(command)
    await site.core.approve(command.command_id, "ops@site")
    site.running(revision_id)
    await site.core.watch_activations(await site.gateway.status())
    site.drain()
    return command


# --- signed desired state ----------------------------------------------------------------------


async def test_signed_manifest_is_committed_and_starts_the_gateway(site: Site) -> None:
    ack = await site.commit([REV1])
    assert ack.accepted
    assert ack.desired_state_generation == 1
    assert site.core.state.manifest_generation == 1
    assert site.gateway.starts == 1
    assert site.core.robot_names() == ROBOTS


@pytest.mark.parametrize("tamper", ["foreign_key", "unknown_key_id", "edited_payload"])
async def test_manifest_with_a_bad_signature_is_refused(site: Site, tamper: str) -> None:
    manifest = site.manifest([REV1])
    if tamper == "foreign_key":
        desired = site.desired(manifest, key=Ed25519PrivateKey.generate())
    elif tamper == "unknown_key_id":
        desired = site.desired(manifest, key_id="mfk_other")
    else:
        desired = site.desired(manifest)
        desired.manifest = canonical_json({**manifest, "offline_authorization_hours": 8760})
    ack = (await site.core.on_desired_state(desired)).ack
    assert not ack.accepted
    assert ack.reason.code == ReasonCode.AGENT_MANIFEST_SIGNATURE_INVALID.value
    assert site.core.state.manifest_payload is None
    assert site.gateway.starts == 0


async def test_manifest_for_another_site_is_refused(site: Site) -> None:
    desired = site.desired(site.manifest([REV1], site_id="sit_01J00000000000000000000OTHER"))
    ack = (await site.core.on_desired_state(desired)).ack
    assert (ack.accepted, ack.reason.code) == (False, ReasonCode.PROTOCOL_MESSAGE_INVALID.value)


async def test_older_generation_is_refused_and_a_redelivery_is_a_no_op(site: Site) -> None:
    first = site.manifest([REV1])
    second = site.manifest([REV1, REV2])
    assert (await site.core.on_desired_state(site.desired(second))).ack.accepted
    stale = (await site.core.on_desired_state(site.desired(first))).ack
    assert (stale.accepted, stale.reason.code) == (False, ReasonCode.AGENT_GENERATION_STALE.value)
    assert site.core.state.manifest_generation == 2
    again = (await site.core.on_desired_state(site.desired(second))).ack
    assert again.accepted
    assert site.gateway.reloads == 0


async def test_revisions_leaving_the_manifest_are_pruned(site: Site) -> None:
    await prepared(site, REV1, REV2)
    await activated(site, REV1["id"])
    await site.commit([REV2])
    assert set(site.core.state.prepared) == {REV2["id"]}
    assert all(a.current is None for a in site.core.state.assignments.values())


# --- commands ----------------------------------------------------------------------------------


async def test_prepare_verifies_contracts_and_reports_success(site: Site) -> None:
    await site.commit([REV1])
    await site.core.on_command(site.prepare(REV1["id"]))
    messages = site.drain()
    assert messages[0].ack.command_id == "cmd_1"
    assert messages[0].ack.accepted
    states = [m.operation.state for m in messages if m.WhichOneof("body") == "operation"]
    assert states == ["Running", "Succeeded"]
    assert site.core.state.prepared == {REV1["id"]: REV1["revision_digest"]}
    succeeded = [m.operation for m in messages if m.WhichOneof("body") == "operation"][-1]
    capacity = json.loads(succeeded.result_json)["capacity"]
    assert capacity["max_robots"] > 0
    assert capacity["source"] == "configured_profile"


async def test_a_redelivered_command_is_executed_at_most_once(site: Site) -> None:
    await site.commit([REV1])
    command = site.prepare(REV1["id"])
    await site.core.on_command(command)
    site.drain()
    executed = list(site.core.state.executed)
    await site.core.on_command(command)
    again = site.drain()
    # Acknowledged again with the last report, but not run again.
    assert [m.WhichOneof("body") for m in again] == ["ack", "operation"]
    assert again[1].operation.state == "Succeeded"
    assert site.core.state.executed == executed
    # Also across a restart: the record of executed commands is durable.
    site.core = site.new_core()
    await site.core.on_command(command)
    assert [m.WhichOneof("body") for m in site.drain()] == ["ack", "operation"]


async def test_tampered_document_fails_prepare(site: Site) -> None:
    bad = copy.deepcopy(REV1)
    bad["documents"]["model_artifact"]["document"]["metadata"]["version"] = "swapped"
    await site.commit([bad])
    await site.core.on_command(site.prepare(bad["id"]))
    final = site.reports()[-1]
    assert (final.state, final.reason.code) == (
        "Failed",
        ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH.value,
    )
    assert site.core.state.prepared == {}


async def test_revision_digest_must_match_its_documents(site: Site) -> None:
    bad = {**copy.deepcopy(REV1), "revision_digest": REV2["revision_digest"]}
    await site.commit([bad])
    await site.core.on_command(site.prepare(bad["id"]))
    assert site.reports()[-1].reason.code == ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH.value


async def failed_prepare(site: Site, rev: dict[str, Any]) -> site_pb2.OperationReport:
    assert (await site.commit([rev])).accepted
    await site.core.on_command(site.prepare(rev["id"]))
    final = site.reports()[-1]
    assert final.state == "Failed"
    assert rev["id"] not in site.core.state.prepared
    return final


async def test_an_artifact_without_a_trusted_publishers_signature_is_not_run(
    site: Site,
) -> None:
    unsigned = revision("rev_01J0000000000000000000003", publishers=())
    final = await failed_prepare(site, unsigned)
    assert final.reason.code == ReasonCode.AGENT_ARTIFACT_UNTRUSTED.value
    stranger = revision("rev_01J0000000000000000000004", publishers=(Ed25519PrivateKey.generate(),))
    final = await failed_prepare(site, stranger)
    assert final.reason.code == ReasonCode.AGENT_ARTIFACT_UNTRUSTED.value
    assert "signed by ed25519:" in final.message
    refused = [r for r in site.core.audit.tail() if r["event"] == "artifact.refused"]
    assert [r["revision"] for r in refused] == [unsigned["id"], stranger["id"]]


async def test_a_trusted_signature_for_another_artifact_is_invalid(site: Site) -> None:
    rev = revision("rev_01J0000000000000000000003")
    rev["documents"]["model_artifact"]["signatures"] = [
        signing.sign("sha256:" + "0" * 64, PUBLISHER)
    ]
    final = await failed_prepare(site, rev)
    assert final.reason.code == ReasonCode.AGENT_ARTIFACT_SIGNATURE_INVALID.value


async def test_publisher_trust_is_read_from_a_file_on_every_prepare(tmp_path: Path) -> None:
    trust = tmp_path / "publishers"
    trust.write_text("# nobody yet\n")
    site = Site(tmp_path, trusted_publisher_keys=[], trusted_publisher_keys_file=trust)
    assert (await failed_prepare(site, REV1)).reason.code == (
        ReasonCode.AGENT_ARTIFACT_UNTRUSTED.value
    )
    trust.write_text(f"{PUBLISHER_HEX}  # release pipeline\n")
    await site.core.on_command(site.prepare(REV1["id"]))
    assert site.reports()[-1].state == "Succeeded"
    verified = [r for r in site.core.audit.tail() if r["event"] == "artifact.verified"]
    assert verified[-1]["publisher"] == signing.key_id(signing.raw_public_key(PUBLISHER))


async def test_signatures_can_be_waived_for_development(tmp_path: Path) -> None:
    site = Site(tmp_path, trusted_publisher_keys=[], require_artifact_signatures=False)
    await prepared(site, revision("rev_01J0000000000000000000003", publishers=()))


async def test_prepare_of_a_revision_not_in_the_manifest_fails(site: Site) -> None:
    await site.commit([REV1])
    await site.core.on_command(site.prepare(REV2["id"]))
    assert site.reports()[-1].reason.code == ReasonCode.DEPLOYMENT_NOT_ACTIVE.value


async def test_operations_outside_local_boundaries_are_denied(tmp_path: Path) -> None:
    site = Site(tmp_path, allowed_operations=["prepare"])
    await prepared(site, REV1)
    await site.core.on_command(site.activate(REV1["id"]))
    final = site.reports()[-1]
    assert (final.state, final.reason.code) == ("Failed", ReasonCode.AGENT_BOUNDARY_DENIED.value)
    assert site.core.pending_approvals() == []
    assert site.core.audit.tail()[-1]["reason"] == ReasonCode.AGENT_BOUNDARY_DENIED.value


async def test_activation_of_an_unprepared_revision_fails(site: Site) -> None:
    await site.commit([REV1])
    await site.core.on_command(site.activate(REV1["id"]))
    assert site.reports()[-1].reason.code == ReasonCode.DEPLOYMENT_NOT_ACTIVE.value


async def test_collect_diagnostics_reports_local_state(site: Site) -> None:
    await prepared(site, REV1)
    await site.core.on_command(site.command(collect_diagnostics=site_pb2.CollectDiagnostics()))
    result = json.loads(site.reports()[-1].result_json)
    assert result["prepared"] == [REV1["id"]]
    assert result["manifest_generation"] == 1


# --- activation: local approval and task-boundary switches -------------------------------------


async def test_activation_waits_for_local_approval(site: Site) -> None:
    await prepared(site, REV1)
    command = site.activate(REV1["id"])
    await site.core.on_command(command)
    assert site.reports()[-1].state == "Running"
    assert [a.command_id for a in site.core.pending_approvals()] == [command.command_id]
    assert site.core.assignment_for("robot-0") is None  # nothing runs before approval

    await site.core.approve(command.command_id, "ops@site")
    assignment = site.core.assignment_for("robot-0")
    assert assignment is not None
    assert assignment["revision_id"] == REV1["id"]
    assert site.reports()[-1].message.startswith("approved by ops@site")

    site.running(REV1["id"])
    await site.core.watch_activations(await site.gateway.status())
    final = site.reports()[-1]
    assert final.state == "Succeeded"
    assert json.loads(final.result_json) == {
        "approved_by": "ops@site",
        "revision_id": REV1["id"],
        "robot_ids": sorted(ROBOTS),
    }
    assert site.core.pending_approvals() == []


async def test_both_revisions_are_served_until_every_robot_switched(site: Site) -> None:
    await prepared(site, REV1, REV2)
    await activated(site, REV1["id"])
    command = site.activate(REV2["id"])
    await site.core.on_command(command)
    await site.core.approve(command.command_id, "ops@site")

    config = build_gateway_config(site.core.state, site.settings, site.core.gateway_dir)
    served = {d["revision_id"]: d["robots"] for d in config["deployments"]}
    assert served == {REV1["id"]: ["robot-0", "robot-1"], REV2["id"]: ["robot-0", "robot-1"]}

    # One robot reached a task boundary; the activation is not done until both have.
    site.gateway.sessions = [
        {"robot_id": "robot-0", "revision_id": REV2["id"]},
        {"robot_id": "robot-1", "revision_id": REV1["id"]},
    ]
    await site.core.watch_activations(await site.gateway.status())
    assert site.core.pending_approvals() != []

    site.running(REV2["id"])
    await site.core.watch_activations(await site.gateway.status())
    assert site.reports()[-1].state == "Succeeded"
    config = build_gateway_config(site.core.state, site.settings, site.core.gateway_dir)
    assert [d["revision_id"] for d in config["deployments"]] == [REV2["id"]]


async def test_refusal_reverts_the_assignment(site: Site) -> None:
    await prepared(site, REV1, REV2)
    await activated(site, REV1["id"])
    command = site.activate(REV2["id"])
    await site.core.on_command(command)
    await site.core.refuse(command.command_id, "ops@site", "not during the shift")
    final = site.reports()[-1]
    assert (final.state, final.reason.code) == ("Failed", ReasonCode.AGENT_ACTIVATION_REFUSED.value)
    assert json.loads(final.result_json)["refused_by"] == "ops@site"
    assert all(
        a.next is None and a.current == REV1["id"] for a in site.core.state.assignments.values()
    )
    with pytest.raises(LocalActionError):
        await site.core.approve(command.command_id, "ops@site")


async def test_activation_times_out_when_robots_do_not_switch(tmp_path: Path) -> None:
    site = Site(tmp_path, activation_timeout_s=30)
    await prepared(site, REV1)
    command = site.activate(REV1["id"])
    await site.core.on_command(command)
    await site.core.approve(command.command_id, "ops@site")
    site.clock.now += 31
    await site.core.watch_activations(await site.gateway.status())
    final = site.reports()[-1]
    assert final.reason.code == ReasonCode.AGENT_ACTIVATION_TIMED_OUT.value
    assert site.core.assignment_for("robot-0") is None


async def test_cancel_withdraws_a_pending_activation(site: Site) -> None:
    await prepared(site, REV1)
    command = site.activate(REV1["id"])
    await site.core.on_command(command)
    await site.core.on_command(
        site.command(cancel=site_pb2.CancelOperation(operation_id=command.operation_id))
    )
    assert site.reports()[-1].state == "Cancelled"
    assert site.core.pending_approvals() == []


# --- offline operation --------------------------------------------------------------------------


async def test_offline_authorization_expiry_stops_serving_until_contact(site: Site) -> None:
    await prepared(site, REV1)
    await activated(site, REV1["id"])
    site.core.note_contact()
    site.clock.now += 3599
    await site.core.enforce_authorization()
    assert serving(site)  # the site keeps working through an outage

    site.clock.now += 2
    await site.core.enforce_authorization()
    assert site.core.authorization_expired()
    assert not serving(site)
    await site.core.on_command(site.activate(REV1["id"]))
    assert site.reports()[-1].reason.code == ReasonCode.AGENT_AUTHORIZATION_EXPIRED.value

    site.core.note_contact()
    await site.core.enforce_authorization()
    assert serving(site)
    assert not site.core.authorization_expired()


async def test_restart_resumes_committed_state_and_fails_interrupted_operations(
    site: Site,
) -> None:
    await prepared(site, REV1)
    pending = site.activate(REV1["id"])
    await site.core.on_command(pending)
    # A prepare that was running when the process died: its report says Running.
    site.core._report("cmd_lost", "opr_lost", "Running", message="verifying contracts")
    site.drain()

    site.core = site.new_core()
    site.core.recover()
    reports = {r.operation_id: r for r in site.reports()}
    assert reports["opr_lost"].reason.code == ReasonCode.AGENT_OPERATION_INTERRUPTED.value
    assert pending.operation_id not in reports  # durable activations carry on
    assert [a.command_id for a in site.core.pending_approvals()] == [pending.command_id]
    assert site.core.state.prepared == {REV1["id"]: REV1["revision_digest"]}


async def test_reconnect_resends_reports_in_order(site: Site) -> None:
    await prepared(site, REV1)
    states = [m.operation.state for m in site.core.pending_reports()]
    assert states == ["Succeeded"]  # the last report per command


# --- local state and identity ---------------------------------------------------------------------


def test_state_round_trips_and_is_private(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    state = AgentState(manifest_generation=7, prepared={"rev_1": "sha256:x"})
    store.save(state)
    assert store.load() == state
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temporary files left


def test_atomic_write_keeps_the_old_file_when_writing_fails(tmp_path: Path) -> None:
    path = tmp_path / "file"
    atomic_write(path, b"old")

    with pytest.raises(TypeError):
        atomic_write(path, "not bytes")  # type: ignore[arg-type]
    assert path.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["file"]


def test_key_is_generated_once_and_tokens_verify_with_its_public_key(tmp_path: Path) -> None:
    store = IdentityStore(tmp_path)
    key = store.load_or_create_key()
    assert store.public_key_raw(store.load_or_create_key()) == store.public_key_raw(key)
    assert stat.S_IMODE(os.stat(store.key_path).st_mode) == 0o600
    token = connection_token(key, "srv_1", now=1_700_000_000)
    claims = jwt.decode(
        token,
        key.public_key(),
        algorithms=["EdDSA"],
        audience=AUDIENCE,
        options={"verify_exp": False},
    )
    assert (claims["sub"], claims["exp"] - claims["iat"]) == ("srv_1", 240)


# --- contact proofs (offline authorization renewal) -----------------------------------------------


def proof_for(site: Site, nonce: bytes, *, key: Ed25519PrivateKey | None = None) -> Any:
    return site_pb2.ContactProof(
        nonce=nonce,
        signature=site_pb2.Signature(
            key_id=KEY_ID, value=(key or site.key).sign(contact_proof_payload(SITE, nonce))
        ),
    )


async def test_only_a_signed_answer_to_our_own_challenge_renews_authorization(site: Site) -> None:
    await prepared(site, REV1)
    contact = site.core.state.last_contact_unix
    site.clock.now += 3000
    nonce = site.core.status_message(None).status.contact_nonce
    assert len(nonce) == 16

    assert not site.core.on_contact_proof(proof_for(site, b"x" * 16))  # not our nonce
    assert not site.core.on_contact_proof(proof_for(site, nonce, key=Ed25519PrivateKey.generate()))
    forged = proof_for(site, nonce)
    forged.signature.value = site.key.sign(nonce)  # signed without the proof context
    assert not site.core.on_contact_proof(forged)
    assert site.core.state.last_contact_unix == contact

    assert site.core.on_contact_proof(proof_for(site, nonce))
    assert site.core.state.last_contact_unix == site.clock.now
    assert not site.core.on_contact_proof(proof_for(site, nonce))  # each nonce counts once


async def test_a_replayed_manifest_does_not_renew_authorization(site: Site) -> None:
    manifest = site.manifest([REV1])
    await site.core.on_desired_state(site.desired(manifest))
    committed_at = site.core.state.last_contact_unix
    assert committed_at is not None
    site.clock.now += 600
    assert (await site.core.on_desired_state(site.desired(manifest))).ack.accepted
    assert site.core.state.last_contact_unix == committed_at


# --- policy workers: placement, weights and measured profiles (design §9.5, §13) ---------------


def fine_tune(version: str, adapter: str = "lerobot-act") -> dict[str, Any]:
    """Another artifact of the same architecture: a different digest, so its own batch group."""
    artifact = _example("act-so101.artifact.yaml")
    artifact["metadata"]["version"] = version
    artifact["spec"]["adapter"]["name"] = adapter
    return artifact


def last_report(site: Site) -> site_pb2.OperationReport:
    return site.reports()[-1]


async def test_prepare_needs_a_device_of_the_profiles_kind(tmp_path: Path) -> None:
    site = Site(tmp_path, gateway_backend="workers")  # this server has no usable GPU
    await site.commit([REV1])
    await site.core.on_command(site.prepare(REV1["id"]))
    report = last_report(site)
    assert (report.state, report.reason.code) == ("Failed", "AGENT_DEVICE_UNAVAILABLE")
    assert site.core.state.prepared == {}


async def test_prepare_needs_the_weights_at_the_site(tmp_path: Path) -> None:
    site = Site(tmp_path, gateway_backend="workers")
    site.cuda.append(0)
    await site.commit([REV1])
    await site.core.on_command(site.prepare(REV1["id"]))
    report = last_report(site)
    assert (report.state, report.reason.code) == ("Failed", "AGENT_ARTIFACT_UNAVAILABLE")
    assert "neura-agent import-weights" in report.message


async def test_imported_weights_are_checked_out_and_the_model_loaded(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    (checkpoint / "model.safetensors").write_bytes(b"not really weights")
    site = Site(tmp_path, gateway_backend="workers")
    site.cuda.append(0)
    imported = site.core.artifacts.import_directory(checkpoint)
    artifact = fine_tune("imported")
    artifact["spec"]["weights"] = [
        {"path": i.path, "digest": i.digest, "size_bytes": i.size_bytes} for i in imported
    ]
    rev = revision("rev_01J0000000000000000000009", artifact=artifact)
    await site.commit([rev])
    await site.core.on_command(site.prepare(rev["id"]))
    report = last_report(site)
    # The weights were found and verified; loading them on the device is what failed.
    assert (report.state, report.reason.code) == ("Failed", "WORKER_LOAD_FAILED"), report
    checkout = site.core.artifacts.checkout_path(parse_document(artifact))  # type: ignore[arg-type]
    assert (checkout / "model.safetensors").read_bytes() == b"not really weights"
    assert not os.access(checkout / "model.safetensors", os.W_OK) or os.geteuid() == 0


async def test_prepare_measures_the_model_and_reports_its_sharing_case(tmp_path: Path) -> None:
    site = Site(tmp_path, gateway_backend="workers", dev_adapter_override="mock")
    site.cuda.extend([0, 1])
    other = revision("rev_01J0000000000000000000003", artifact=fine_tune("tune-b"))
    await site.commit([REV1, REV2, other])

    def capacity() -> dict[str, Any]:
        report = last_report(site)
        assert report.state == "Succeeded", report
        estimate: dict[str, Any] = json.loads(report.result_json)["capacity"]
        return estimate

    await site.core.on_command(site.prepare(REV1["id"]))
    first = capacity()
    assert first["source"] == "measured_profile"
    assert first["sharing_case"] == "same_weights"
    assert first["assumptions"]["device"] == "cuda:0"
    assert set(first["assumptions"]["latency_p99_ms_by_batch"]) == {"1", "2", "4", "8"}
    # Same artifact (only the execution profile differs): the same batch group and device.
    await site.core.on_command(site.prepare(REV2["id"]))
    assert capacity()["assumptions"]["device"] == "cuda:0"
    # A different fine-tune gets the least-loaded GPU to itself.
    await site.core.on_command(site.prepare(other["id"]))
    third = capacity()
    assert (third["assumptions"]["device"], third["sharing_case"]) == ("cuda:1", "same_weights")

    await activated(site, REV1["id"])
    config = build_gateway_config(site.core.state, site.settings, site.core.gateway_dir)
    deployment = config["deployments"][0]
    assert (deployment["device"], deployment["adapter"]) == ("cuda:0", "mock")
    assert json.loads(Path(deployment["profile"]).read_text())["points"]
    assert config["serving"]["backend"] == "workers"


async def test_prepare_pulls_the_pinned_runtime_image_or_fails(tmp_path: Path) -> None:
    """In the container sandbox the runtime image is fetched at prepare, by digest."""
    calls = tmp_path / "calls"
    runtime = tmp_path / "runtime"
    runtime.write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexit 1\n')
    runtime.chmod(0o755)
    site = Site(
        tmp_path,
        gateway_backend="workers",
        dev_adapter_override="mock",
        worker_sandbox="container",
        worker_container_runtime=[str(runtime)],
    )
    site.cuda.append(0)
    await site.commit([REV1])
    await site.core.on_command(site.prepare(REV1["id"]))
    report = last_report(site)
    assert (report.state, report.reason.code) == ("Failed", "WORKER_IMAGE_UNAVAILABLE"), report
    image = _example("act-so101.artifact.yaml")["spec"]["runtime_image"]["reference"]
    assert calls.read_text().splitlines() == [f"image inspect {image}", f"pull {image}"]
    config = build_gateway_config(site.core.state, site.settings, site.core.gateway_dir)
    assert config["serving"]["sandbox"]["kind"] == "container"
    assert config["serving"]["sandbox"]["runtime"] == [str(runtime)]


async def test_fine_tunes_sharing_one_gpu_are_co_located(tmp_path: Path) -> None:
    site = Site(tmp_path, gateway_backend="workers", dev_adapter_override="mock")
    site.cuda.append(0)
    tune = revision("rev_01J0000000000000000000003", artifact=fine_tune("tune-b"))
    other_arch = revision(
        "rev_01J0000000000000000000004", artifact=fine_tune("smol", adapter="lerobot-smolvla")
    )
    await site.commit([REV1, tune, other_arch])
    cases = []
    for rev in (REV1, tune, other_arch):
        await site.core.on_command(site.prepare(rev["id"]))
        report = last_report(site)
        assert report.state == "Succeeded", report
        estimate = json.loads(report.result_json)["capacity"]
        cases.append((estimate["sharing_case"], estimate["assumptions"]["co_located_groups"]))
    assert cases == [
        ("same_weights", 0),
        ("co_located_fine_tunes", 1),
        ("different_architectures", 2),
    ]
    assert {p["device"] for p in site.core.state.placements.values()} == {"cuda:0"}


# --- the site CA and robot certificates (design §17.3) -----------------------------------------


def robot_request(name: str) -> bytes:
    return pki.signing_request(pki.new_key(), name)


async def test_the_gateway_runs_mutual_tls_with_certificates_from_the_site_ca(
    site: Site,
) -> None:
    await site.commit([REV1])
    config = site.core.gateway_config()
    assert config["site_id"] == SITE
    tls = config["tls"]
    gateway_cert = Path(tls["certificate"]).read_bytes()
    assert site.core.pki.authority.issued(gateway_cert)
    assert pki.identity(gateway_cert) == pki.gateway_uri(SITE)
    assert Path(tls["client_ca"]).read_bytes() == site.core.pki.ca_pem
    assert stat.S_IMODE(os.stat(tls["key"]).st_mode) == 0o600
    assert config["devices"] == []  # no robot has a certificate yet

    certificate, ca = site.core.issue_robot_certificate("robot-0", robot_request("robot-0"))
    assert ca == site.core.pki.ca_pem
    assert pki.identity(certificate) == pki.robot_uri(SITE, "robot-0")
    devices = site.core.gateway_config()["devices"]
    assert devices == [
        {"robot_id": "robot-0", "certificate_sha256": [pki.fingerprint(certificate)]}
    ]
    # The same gateway certificate is kept while it is valid for the same names.
    assert Path(site.core.gateway_config()["tls"]["certificate"]).read_bytes() == gateway_cert

    with pytest.raises(LocalActionError):
        site.core.issue_robot_certificate("not-a-robot", robot_request("x"))
    with pytest.raises(LocalActionError):
        site.core.issue_robot_certificate("robot-1", b"garbage")

    assert site.core.revoke_robot_certificates("robot-0") == 1
    assert site.core.gateway_config()["devices"] == []
    events = [e["event"] for e in site.core.audit.tail()]
    assert "robot_certificate_issued" in events
    assert "robot_certificates_revoked" in events


async def test_assignments_tell_each_robot_its_preferred_revision(site: Site) -> None:
    await prepared(site, REV1)
    await activated(site, REV1["id"])
    config = site.core.gateway_config()
    assert config["assignments"] == dict.fromkeys(ROBOTS.values(), REV1["id"])


async def test_development_robots_get_bundles_written_for_them(tmp_path: Path) -> None:
    site = Site(tmp_path, dev_robot_certificate_dir=tmp_path / "certs")
    await site.commit([REV1])
    site.core.gateway_config()
    for name in ROBOTS.values():
        cert = (tmp_path / "certs" / f"{name}.pem").read_bytes()
        assert site.core.pki.authority.issued(cert)
        assert stat.S_IMODE(os.stat(tmp_path / "certs" / f"{name}.key").st_mode) == 0o600
    first = (tmp_path / "certs" / "robot-0.pem").read_bytes()
    site.core.gateway_config()
    assert (tmp_path / "certs" / "robot-0.pem").read_bytes() == first  # not reissued


async def test_without_tls_robots_use_development_tokens(tmp_path: Path) -> None:
    site = Site(tmp_path, gateway_tls=False, dev_robot_tokens={"robot-0": "t0"})
    await site.commit([REV1])
    config = site.core.gateway_config()
    assert "tls" not in config
    assert config["devices"] == [
        {"robot_id": "robot-0", "token_sha256": hashlib.sha256(b"t0").hexdigest()}
    ]


def test_robot_keygen_keeps_the_key_private(tmp_path: Path) -> None:
    from neura_runtime.cli import main as robot_main

    assert robot_main(["keygen", "robot-0", "--dir", str(tmp_path)]) == 0
    assert stat.S_IMODE(os.stat(tmp_path / "robot-0.key").st_mode) == 0o600
    request = (tmp_path / "robot-0.csr").read_bytes()
    ca = pki.CertificateAuthority.create("site CA")
    cert = ca.sign_request(request, identity_uri=pki.robot_uri(SITE, "robot-0"), days=1)
    key = pki.load_key((tmp_path / "robot-0.key").read_bytes())
    assert pki.load_certificate(cert).public_key() == key.public_key()
    with pytest.raises(SystemExit):  # never overwrites a key
        robot_main(["keygen", "robot-0", "--dir", str(tmp_path)])


def test_operators_trust_and_distrust_publishers_with_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from neura_agent import trust
    from neura_agent.__main__ import main

    monkeypatch.setenv("NEURA_AGENT_STATE_DIR", str(tmp_path / "agent"))
    assert main(["publishers"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["trust-publisher", PUBLISHER_HEX, "--note", "release pipeline"]) == 0
    assert "trusting ed25519:" in capsys.readouterr().out
    assert main(["trust-publisher", PUBLISHER_HEX]) == 0
    assert "already trusted" in capsys.readouterr().out
    other = Ed25519PrivateKey.generate()
    pem = tmp_path / "ci.pub"
    pem.write_bytes(
        other.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    assert main(["trust-publisher", str(pem)]) == 0
    settings = AgentSettings(state_dir=tmp_path / "agent")
    assert trust.trusted_publishers(settings) == [
        signing.raw_public_key(PUBLISHER),
        signing.raw_public_key(other),
    ]
    assert "release pipeline" in trust.trust_file(settings).read_text()
    assert main(["distrust-publisher", signing.key_id(signing.raw_public_key(PUBLISHER))]) == 0
    assert trust.trusted_publishers(settings) == [signing.raw_public_key(other)]
    assert main(["distrust-publisher", signing.key_id(signing.raw_public_key(PUBLISHER))]) == 1
    assert main(["trust-publisher", "not-a-key"]) == 1


async def test_the_agent_exports_its_state_for_the_customers_monitoring(site: Site) -> None:
    from types import SimpleNamespace

    from prometheus_client.parser import text_string_to_metric_families

    from neura_agent.identity import IdentityStore
    from neura_agent.metrics import EXPORTED, AgentMetrics

    await prepared(site, REV1)
    site.core.outbound.prepare(
        "agent", site_pb2.AgentMessage(hello=site_pb2.AgentHello(site_id=SITE))
    )
    identities = IdentityStore(site.settings.state_dir)
    identities.save(site.identity)
    runtime = SimpleNamespace(
        settings=site.settings,
        identities=identities,
        outbound=site.core.outbound,
        core=site.core,
        connection=None,
    )
    exposition = AgentMetrics(runtime, "9.9").exposition().decode()  # type: ignore[arg-type]
    families = {f.name: f for f in text_string_to_metric_families(exposition)}
    assert set(families) <= EXPORTED

    def value(name: str, **labels: str) -> float:
        return next(
            s.value
            for s in families[name].samples
            if all(s.labels.get(k) == v for k, v in labels.items())
        )

    assert value("neura_agent_info", telemetry_mode="standard", site_id=SITE) == 1
    assert value("neura_agent_enrolled") == 1
    assert value("neura_agent_manifest_generation") == 1
    assert value("neura_agent_prepared_revisions") == 1
    assert value("neura_agent_operations", state="Succeeded") == 1
    assert value("neura_agent_artifact_checks", result="verified") == 1
    assert value("neura_agent_outbound_messages", kind="hello") == 1
    assert value("neura_agent_connected") == 0
    assert value("neura_agent_certificate_expiry_timestamp_seconds", certificate="site_ca") > 0
