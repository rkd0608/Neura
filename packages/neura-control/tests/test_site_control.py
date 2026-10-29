"""The control plane's site-agent endpoint: enrollment, stream authentication, signed desired
state, contact proofs and what a site's connectivity looks like through the API (design §17)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import os
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import grpc
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from cryptography.x509.oid import NameOID
from sqlalchemy import update

from conftest import AppFactory, contract, until
from neura.v1 import common_pb2, site_pb2, site_pb2_grpc
from neura_agent.agent_tls import AgentTls
from neura_agent.artifacts import ArtifactError, ArtifactStore
from neura_agent.gateway import InProcessGateway
from neura_agent.identity import Identity, IdentityStore, connection_token
from neura_agent.registry import NeuraRegistry
from neura_agent.runner import AgentRuntime
from neura_agent.settings import AgentSettings
from neura_contracts import ModelArtifact, ReasonCode, parse_document, pki, sealing
from neura_contracts.site_protocol import contact_proof_payload
from neura_control.clock import iso, utcnow
from neura_control.db.engine import create_engine, session_factory
from neura_control.db.models import EnrollmentToken, Site


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class Harness:
    def __init__(self, make_api: AppFactory, settings: Any, **overrides: Any) -> None:
        self.endpoint = f"127.0.0.1:{free_port()}"
        self.database_url = str(settings.database_url)
        self.client, org = make_api(
            settings_overrides={
                "agent_grpc_listen": self.endpoint,
                "agent_endpoint": self.endpoint,
                "agent_poll_interval_s": 0.1,
                **overrides,
            }
        )
        c = self.client
        self.project = c.post(
            f"/v1/organizations/{org}/projects", {"spec": {"name": "p", "display_name": "P"}}
        ).json()["metadata"]["id"]
        self.site = c.post(
            f"/v1/projects/{self.project}/sites",
            {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 2}},
        ).json()["metadata"]["id"]
        self.robot_profile = c.post(
            f"/v1/projects/{self.project}/robot-profiles",
            {"document": contract("so101.robot.yaml")},
        ).json()["metadata"]["id"]

    def token(self) -> str:
        secret: str = self.client.post(f"/v1/sites/{self.site}/enrollment-tokens", {}).json()[
            "secret"
        ]
        return secret

    def add_robot(self, name: str) -> None:
        response = self.client.post(
            f"/v1/projects/{self.project}/robots",
            {
                "spec": {
                    "name": name,
                    "display_name": name,
                    "site_id": self.site,
                    "robot_profile_id": self.robot_profile,
                }
            },
        )
        assert response.status_code == 201, response.text

    def enroll(self, token: str, key: Ed25519PrivateKey, **overrides: Any) -> Any:
        request = site_pb2.EnrollRequest(
            protocol_version=common_pb2.PROTOCOL_VERSION_1,
            enrollment_token=token,
            public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
            agent_version="test",
            facts=site_pb2.ServerFacts(hostname="gpu-01"),
        )
        for field, value in overrides.items():
            setattr(request, field, value)

        async def call() -> site_pb2.EnrollResponse:
            async with grpc.aio.insecure_channel(self.endpoint) as channel:
                response: site_pb2.EnrollResponse = await site_pb2_grpc.SiteControlStub(
                    channel
                ).Enroll(request, timeout=10)
                return response

        return asyncio.run(call())

    def enrolled(self) -> tuple[site_pb2.Enrolled, Ed25519PrivateKey]:
        key = Ed25519PrivateKey.generate()
        response = self.enroll(self.token(), key)
        assert response.WhichOneof("result") == "enrolled"
        return response.enrolled, key

    def serving(self, server_id: str) -> bool:
        return server_id in self.client.app.state.site_control.connected

    def db(self, statement: Any) -> None:
        async def run() -> None:
            engine = create_engine(self.database_url, pool_size=1)
            try:
                async with session_factory(engine).begin() as session:
                    await session.execute(statement)
            finally:
                await engine.dispose()

        asyncio.run(run())


@pytest.fixture
def harness(make_api: AppFactory, settings: Any) -> Harness:
    return Harness(make_api, settings)


async def connect(
    endpoint: str,
    token: str,
    outgoing: list[site_pb2.AgentMessage],
    *,
    until: Any,
    timeout: float = 10,
) -> list[site_pb2.ControlPlaneMessage]:
    """Open a stream, send ``outgoing``, and collect messages until ``until(received)``."""
    received: list[site_pb2.ControlPlaneMessage] = []
    done = asyncio.Event()

    async def requests() -> AsyncIterator[site_pb2.AgentMessage]:
        for message in outgoing:
            yield message
        await done.wait()

    async with grpc.aio.insecure_channel(endpoint) as channel:
        call = site_pb2_grpc.SiteControlStub(channel).Connect(
            requests(), metadata=(("authorization", f"Bearer {token}"),)
        )

        async def read() -> None:
            async for message in call:
                received.append(message)
                if until(received):
                    return

        try:
            await asyncio.wait_for(read(), timeout)
        finally:
            done.set()
            call.cancel()
    return received


def hello(site_id: str) -> site_pb2.AgentMessage:
    return site_pb2.AgentMessage(
        hello=site_pb2.AgentHello(
            protocol_version=common_pb2.PROTOCOL_VERSION_1, site_id=site_id, agent_version="9.9"
        )
    )


def kinds(messages: list[site_pb2.ControlPlaneMessage]) -> list[str]:
    return [m.WhichOneof("body") or "" for m in messages]


# --- enrollment ---------------------------------------------------------------------------------


def test_enrollment_records_the_server_and_pins_the_manifest_key(harness: Harness) -> None:
    enrolled, _ = harness.enrolled()
    assert enrolled.site_id == harness.site
    assert enrolled.manifest_key_id.startswith("ed25519:")
    server = harness.client.get(f"/v1/servers/{enrolled.server_id}").json()
    assert server["spec"]["display_name"] == "gpu-01"
    audit = harness.client.get(f"/v1/projects/{harness.project}/audit-records").json()["items"]
    assert any(r["action"] == "server.enroll" and r["target"] == enrolled.server_id for r in audit)


@pytest.mark.parametrize("case", ["reused", "expired", "malformed", "wrong_secret"])
def test_invalid_enrollment_tokens_are_refused(harness: Harness, case: str) -> None:
    token = harness.token()
    if case == "reused":
        harness.enroll(token, Ed25519PrivateKey.generate())
    elif case == "expired":
        harness.db(update(EnrollmentToken).values(expires_at=utcnow() - timedelta(seconds=1)))
    elif case == "malformed":
        token = "not-a-token"
    else:
        token = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    response = harness.enroll(token, Ed25519PrivateKey.generate())
    assert response.WhichOneof("result") == "rejected"
    assert response.rejected.code == ReasonCode.AGENT_ENROLLMENT_INVALID.value


def test_enrollment_checks_protocol_version_and_key(harness: Harness) -> None:
    key = Ed25519PrivateKey.generate()
    wrong_version = harness.enroll(harness.token(), key, protocol_version=99)
    assert wrong_version.rejected.code == ReasonCode.PROTOCOL_VERSION_MISMATCH.value
    short_key = harness.enroll(harness.token(), key, public_key=b"short")
    assert short_key.rejected.code == ReasonCode.PROTOCOL_MESSAGE_INVALID.value


# --- stream authentication ----------------------------------------------------------------------


@pytest.mark.parametrize("case", ["garbage", "other_key", "too_long_lived", "unknown_server"])
def test_streams_with_bad_credentials_are_refused(harness: Harness, case: str) -> None:
    enrolled, key = harness.enrolled()
    if case == "garbage":
        token = "not.a.jwt"
    elif case == "other_key":
        token = connection_token(Ed25519PrivateKey.generate(), enrolled.server_id)
    elif case == "too_long_lived":
        token = jwt.encode(
            {"sub": enrolled.server_id, "aud": "neura-control", "iat": 0, "exp": 2**40},
            key,
            algorithm="EdDSA",
        )
    else:
        token = connection_token(key, "srv_01J0000000000000000000000X")
    with pytest.raises(grpc.aio.AioRpcError) as raised:
        asyncio.run(connect(harness.endpoint, token, [], until=lambda r: True))
    assert raised.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert raised.value.details() == ReasonCode.AGENT_UNAUTHENTICATED.value


def test_deleting_a_server_ends_its_stream_and_its_credential(harness: Harness) -> None:
    enrolled, key = harness.enrolled()
    path = f"/v1/servers/{enrolled.server_id}"

    async def scenario() -> None:
        async def delete_soon() -> None:
            await until(lambda: harness.serving(enrolled.server_id), "the stream to open")
            version = (await asyncio.to_thread(harness.client.get, path)).json()["metadata"]
            response = await asyncio.to_thread(
                harness.client.delete, path, version=version["version"]
            )
            assert response.status_code == 204

        deleting = asyncio.create_task(delete_soon())
        # Never satisfied: this returns only because the control plane ends the stream (a
        # stream it kept open would time out instead).
        await connect(
            harness.endpoint,
            connection_token(key, enrolled.server_id),
            [hello(harness.site)],
            until=lambda r: False,
        )
        await deleting

    asyncio.run(scenario())
    token = connection_token(key, enrolled.server_id)
    with pytest.raises(grpc.aio.AioRpcError) as again:
        asyncio.run(connect(harness.endpoint, token, [], until=lambda r: True))
    assert again.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_a_new_stream_from_a_server_replaces_its_previous_one(harness: Harness) -> None:
    enrolled, key = harness.enrolled()

    async def scenario() -> list[site_pb2.ControlPlaneMessage]:
        first = asyncio.create_task(
            connect(
                harness.endpoint,
                connection_token(key, enrolled.server_id),
                [hello(harness.site)],
                until=lambda r: False,  # ends only if the control plane closes it
            )
        )
        await until(lambda: harness.serving(enrolled.server_id), "the first stream to open")
        assert not first.done()
        second = await connect(
            harness.endpoint,
            connection_token(key, enrolled.server_id),
            [hello(harness.site)],
            until=lambda r: "desired_state" in kinds(r),
        )
        await asyncio.wait_for(first, 5)
        return second

    assert "desired_state" in kinds(asyncio.run(scenario()))


def test_agents_fetch_sealed_weights_from_the_registry_and_open_them_at_the_site(
    make_api: AppFactory, settings: Any, tmp_path: Path
) -> None:
    harness = Harness(make_api, settings, sealed_weights_dir=tmp_path / "registry")
    enrolled, key = harness.enrolled()
    files = {"model.safetensors": os.urandom(4000), "config.json": b'{"type": "act"}'}
    for name, data in files.items():
        (tmp_path / "ckpt" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "ckpt" / name).write_bytes(data)
    sealing.generate_key_file(tmp_path / "kek")
    customer_key = sealing.FileKey(tmp_path / "kek")
    sealing.seal_directory(tmp_path / "ckpt", tmp_path / "sealed", customer_key)
    for blob in (tmp_path / "sealed" / "sha256").iterdir():
        path = f"/v1/projects/{harness.project}/sealed-weights/sha256:{blob.name}"
        assert harness.client.request("PUT", path, content=blob.read_bytes()).status_code == 201
    document = contract("act-so101.artifact.yaml")
    document["spec"]["weights"] = [
        {"path": n, "digest": f"sha256:{hashlib.sha256(d).hexdigest()}", "size_bytes": len(d)}
        for n, d in files.items()
    ]
    artifact = parse_document(document)
    assert isinstance(artifact, ModelArtifact)
    identity = Identity(
        server_id=enrolled.server_id,
        site_id=harness.site,
        project_id=harness.project,
        endpoint=harness.endpoint,
        manifest_public_key_hex=enrolled.manifest_public_key.hex(),
        manifest_key_id=enrolled.manifest_key_id,
    )
    agent_settings = AgentSettings(
        state_dir=tmp_path / "agent", control_plane_insecure=True, development=True
    )

    def store(name: str, agent_key: Ed25519PrivateKey, **keys: Any) -> ArtifactStore:
        return ArtifactStore(
            tmp_path / name,
            ["neura"],
            keys=keys or {customer_key.key_id: customer_key},
            registry=NeuraRegistry(agent_settings, identity, agent_key),
        )

    out = store("site", key).checkout(artifact)
    for name, data in files.items():
        assert (out / name).read_bytes() == data
    # Without the customer's key the site cannot open them; another key cannot fetch them.
    with pytest.raises(ArtifactError) as no_key:
        store("no-key", key, other=customer_key).checkout(artifact)
    assert no_key.value.code is ReasonCode.AGENT_ARTIFACT_KEY_UNAVAILABLE
    with pytest.raises(ArtifactError) as stranger:
        store("stranger", Ed25519PrivateKey.generate()).checkout(artifact)
    assert stranger.value.code is ReasonCode.AGENT_ARTIFACT_UNAVAILABLE


# --- desired state and contact ------------------------------------------------------------------


def test_desired_state_is_signed_and_its_generation_moves_only_with_content(
    harness: Harness,
) -> None:
    enrolled, key = harness.enrolled()
    public = Ed25519PublicKey.from_public_bytes(enrolled.manifest_public_key)

    def first_manifest() -> site_pb2.DesiredState:
        received = asyncio.run(
            connect(
                harness.endpoint,
                connection_token(key, enrolled.server_id),
                [hello(harness.site)],
                until=lambda r: "desired_state" in kinds(r),
            )
        )
        desired = next(m.desired_state for m in received if m.WhichOneof("body") == "desired_state")
        public.verify(desired.signature.value, desired.manifest)  # raises if it does not verify
        assert desired.signature.key_id == enrolled.manifest_key_id
        return desired

    first = first_manifest()
    manifest = json.loads(first.manifest)
    assert (manifest["site_id"], manifest["generation"]) == (harness.site, first.generation)
    assert manifest["offline_authorization_hours"] == 2
    assert first_manifest().generation == first.generation  # nothing changed

    harness.add_robot("robot-0")
    changed = first_manifest()
    assert changed.generation == first.generation + 1
    assert [r["name"] for r in json.loads(changed.manifest)["robots"]] == ["robot-0"]


def test_status_challenges_are_answered_with_signed_contact_proofs(harness: Harness) -> None:
    enrolled, key = harness.enrolled()
    public = Ed25519PublicKey.from_public_bytes(enrolled.manifest_public_key)
    nonce = bytes(range(16))
    status = site_pb2.AgentMessage(
        status=site_pb2.SiteStatus(offline_authorization_remaining_s=7200, contact_nonce=nonce)
    )
    received = asyncio.run(
        connect(
            harness.endpoint,
            connection_token(key, enrolled.server_id),
            [hello(harness.site), status],
            until=lambda r: "contact" in kinds(r),
        )
    )
    proof = next(m.contact for m in received if m.WhichOneof("body") == "contact")
    assert proof.nonce == nonce
    public.verify(proof.signature.value, contact_proof_payload(harness.site, nonce))
    site = harness.client.get(f"/v1/sites/{harness.site}").json()["status"]
    assert site["agent_version"] == "9.9"


def test_site_is_shown_disconnected_when_its_reports_go_stale(harness: Harness) -> None:
    enrolled, key = harness.enrolled()
    path = f"/v1/sites/{harness.site}"

    async def while_connected() -> dict[str, Any]:
        stop = asyncio.Event()

        async def requests() -> AsyncIterator[site_pb2.AgentMessage]:
            yield hello(harness.site)
            await stop.wait()

        async with grpc.aio.insecure_channel(harness.endpoint) as channel:
            call = site_pb2_grpc.SiteControlStub(channel).Connect(
                requests(),
                metadata=(
                    ("authorization", f"Bearer {connection_token(key, enrolled.server_id)}"),
                ),
            )
            for _ in range(50):
                status: dict[str, Any] = (await asyncio.to_thread(harness.client.get, path)).json()[
                    "status"
                ]
                if status["connected"]:
                    break
                await asyncio.sleep(0.1)
            # The process serving this stream "crashes": no stream end is ever recorded, and the
            # site's last report becomes old.
            stale = iso(utcnow() - timedelta(minutes=10))
            await asyncio.to_thread(
                harness.db,
                update(Site)
                .where(Site.id == harness.site)
                .values(observed=Site.observed.op("||")({"observed_at": stale})),
            )
            later: dict[str, Any] = (await asyncio.to_thread(harness.client.get, path)).json()[
                "status"
            ]
            stop.set()
            call.cancel()
        return {"before": status, "after": later}

    result = asyncio.run(while_connected())
    assert result["before"]["connected"] is True
    assert (result["after"]["connected"], result["after"]["phase"]) == (False, "Offline")


def test_capture_settings_reach_the_site_and_only_counts_come_back(harness: Harness) -> None:
    """A deployment's capture settings go to the site in its signed manifest; the site reports
    how many episodes it holds, by outcome and revision, and never the episodes (design §28.2)."""
    enrolled, key = harness.enrolled()
    c = harness.client
    harness.add_robot("robot-0")
    robot = c.get(f"/v1/projects/{harness.project}/robots").json()["items"][0]["metadata"]["id"]
    created = c.post(
        f"/v1/projects/{harness.project}/deployments",
        {
            "spec": {
                "name": "cell",
                "display_name": "Cell",
                "site_id": harness.site,
                "robot_ids": [robot],
                "rollout": {"strategy": "all_at_task_boundary"},
                "capture": {"mode": "failures", "image_stride": 5},
            }
        },
    )
    assert created.status_code == 201, created.text
    deployment = created.json()
    assert deployment["spec"]["capture"] == {
        "mode": "failures",
        "image_stride": 5,
        "cameras": [],
        "max_episode_seconds": 600,
    }
    assert deployment["status"]["capture"] == {"episodes": 0, "outcomes": {}}
    path = f"/v1/deployments/{deployment['metadata']['id']}"
    patched = c.patch(
        path,
        {"spec": {"capture": {"mode": "all", "upload_mbps": 50}}},
        version=deployment["metadata"]["version"],
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["spec"]["capture"]["mode"] == "all"
    assert patched.json()["spec"]["capture"]["upload_mbps"] == 50
    refused = c.patch(path, {"spec": {"capture": {"mode": "always"}}}, version=None)
    assert refused.status_code in (412, 422, 428)
    artifact = c.post(
        f"/v1/projects/{harness.project}/model-artifacts",
        {"document": contract("act-so101.artifact.yaml")},
    ).json()["metadata"]["id"]
    profile = c.post(
        f"/v1/projects/{harness.project}/execution-profiles",
        {"document": contract("act-so101-30hz.profile.yaml")},
    ).json()["metadata"]["id"]
    revision = c.post(
        f"{path}/revisions",
        {
            "spec": {
                "model_artifact_id": artifact,
                "execution_profile_id": profile,
                "robot_profile_id": harness.robot_profile,
            }
        },
    )
    assert revision.status_code == 201, revision.text
    revision_id = revision.json()["metadata"]["id"]

    status = site_pb2.AgentMessage(
        status=site_pb2.SiteStatus(
            offline_authorization_remaining_s=7200,
            contact_nonce=bytes(16),  # answered once the status is applied
            capture=site_pb2.CaptureSummary(
                enabled=True,
                episodes=7,
                bytes=1 << 30,
                steps=21_000,
                outcomes={"success": 4, "failure": 2, "unknown": 1},
                revisions=[
                    site_pb2.RevisionCapture(
                        revision_id=revision_id, outcomes={"success": 4, "failure": 1}
                    ),
                    site_pb2.RevisionCapture(revision_id="rev_elsewhere", outcomes={"unknown": 1}),
                ],
                datasets=1,
            ),
        )
    )
    received = asyncio.run(
        connect(
            harness.endpoint,
            connection_token(key, enrolled.server_id),
            [hello(harness.site), status],
            until=lambda r: {"desired_state", "contact"} <= set(kinds(r)),
        )
    )
    desired = next(m.desired_state for m in received if m.WhichOneof("body") == "desired_state")
    (sent,) = json.loads(desired.manifest)["deployments"]
    assert sent["capture"] == {
        "mode": "all",
        "image_stride": 5,
        "max_episode_seconds": 600,
        "cameras": [],
        "upload_mbps": 50.0,
    }
    site = c.get(f"/v1/sites/{harness.site}").json()["status"]
    assert site["capture"] == {
        "enabled": True,
        "episodes": 7,
        "bytes": 1 << 30,
        "steps": 21_000,
        "outcomes": {"success": 4, "failure": 2, "unknown": 1},
        "datasets": 1,
    }
    counts = c.get(path).json()["status"]["capture"]
    assert counts == {"episodes": 5, "outcomes": {"success": 4, "failure": 1}}


# --- transport security -------------------------------------------------------------------------


def _certificates(directory: Path) -> tuple[Path, Path, Path]:
    """A throwaway CA and a server certificate for 127.0.0.1, as PEM files."""
    now = datetime.now(UTC)

    def name(common: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(name("test ca"))
        .issuer_name(name("test ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name("127.0.0.1"))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    paths = directory / "ca.pem", directory / "server.pem", directory / "server.key"
    paths[0].write_bytes(ca.public_bytes(Encoding.PEM))
    paths[1].write_bytes(cert.public_bytes(Encoding.PEM))
    paths[2].write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return paths


def test_agents_enroll_over_tls_and_refuse_an_unknown_server(
    make_api: AppFactory, tmp_path: Path
) -> None:
    ca, cert, key_file = _certificates(tmp_path)
    stream = f"127.0.0.1:{free_port()}"
    endpoint = f"127.0.0.1:{free_port()}"  # enrollment
    client, org = make_api(
        settings_overrides={
            "agent_grpc_listen": stream,
            "agent_endpoint": stream,
            "agent_enroll_listen": endpoint,
            "agent_enroll_endpoint": endpoint,
            "agent_tls_cert_file": cert,
            "agent_tls_key_file": key_file,
        }
    )
    project = client.post(
        f"/v1/organizations/{org}/projects", {"spec": {"name": "p", "display_name": "P"}}
    ).json()["metadata"]["id"]
    site = client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    ).json()["metadata"]["id"]

    def runtime(name: str, **settings: Any) -> AgentRuntime:
        return AgentRuntime(
            AgentSettings(
                state_dir=tmp_path / name,
                gateway_backend="simulated",
                development=True,
                **settings,
            ),
            command_runner=lambda args: None,
        )

    def enroll(agent: AgentRuntime) -> Any:
        token = client.post(f"/v1/sites/{site}/enrollment-tokens", {}).json()["secret"]
        return asyncio.run(agent.enroll(endpoint, token))

    # Without the CA the server's certificate is not trusted, so nothing is sent.
    with pytest.raises(grpc.aio.AioRpcError) as untrusted:
        enroll(runtime("untrusted"))
    assert untrusted.value.code() == grpc.StatusCode.UNAVAILABLE
    identity = enroll(runtime("trusted", control_plane_ca_file=ca))
    assert identity.site_id == site
    assert identity.endpoint == stream  # the agent streams to the stream listener
    # The install command names the enrollment endpoint.
    issued = client.post(f"/v1/sites/{site}/enrollment-tokens", {}).json()
    assert f"--control-plane {endpoint} " in issued["enroll_command"]


def test_staging_requires_tls_for_the_agent_listener(settings: Any) -> None:
    with pytest.raises(ValueError, match="needs TLS"):
        settings.model_validate(
            {
                **settings.model_dump(),
                "environment": "staging",
                "manifest_signing_key": "x" * 43,
                "agent_grpc_listen": "0.0.0.0:8081",
            }
        )
    offloaded = settings.model_validate(
        {
            **settings.model_dump(),
            "environment": "staging",
            "manifest_signing_key": "x" * 43,
            "agent_grpc_listen": "0.0.0.0:8081",
            "agent_tls_offloaded": True,
            "agent_client_ca_cert_file": "/etc/neura/agent-ca.pem",
            "agent_client_ca_key_file": "/etc/neura/agent-ca.key",
            "database_url": "postgresql://neura:x@db.internal:5432/neura?sslmode=verify-full",
        }
    )
    assert offloaded.agent_tls_offloaded
    # Postgres over TLS that verifies the server, unless the connection stays on the host.
    plaintext = {**offloaded.model_dump(), "database_url": "postgresql://neura:x@db:5432/neura"}
    with pytest.raises(ValueError, match="verify-full"):
        settings.model_validate(plaintext)
    assert settings.model_validate({**plaintext, "database_tls_local": True}).database_tls_local
    # Agents' client certificates come from a CA that survives restarts.
    with pytest.raises(ValueError, match="client certificates"):
        settings.model_validate(
            {
                **settings.model_dump(),
                "environment": "staging",
                "manifest_signing_key": "x" * 43,
                "agent_grpc_listen": "0.0.0.0:8081",
                "agent_tls_offloaded": True,
            }
        )


def test_agent_streams_need_their_client_certificate_and_renew_it(
    make_api: AppFactory, tmp_path: Path
) -> None:
    ca, cert, key_file = _certificates(tmp_path)
    endpoint = f"127.0.0.1:{free_port()}"
    enroll_endpoint = f"127.0.0.1:{free_port()}"
    client, org = make_api(
        settings_overrides={
            "agent_grpc_listen": endpoint,
            "agent_endpoint": endpoint,
            "agent_enroll_listen": enroll_endpoint,
            "agent_tls_cert_file": cert,
            "agent_tls_key_file": key_file,
            "agent_poll_interval_s": 0.2,
            "manifest_signing_key": "x" * 43,
            "sealed_weights_dir": tmp_path / "registry",
        }
    )
    project = client.post(
        f"/v1/organizations/{org}/projects", {"spec": {"name": "p", "display_name": "P"}}
    ).json()["metadata"]["id"]
    site = client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    ).json()["metadata"]["id"]
    agent_settings = AgentSettings(
        state_dir=tmp_path / "agent",
        gateway_backend="simulated",
        development=True,
        control_plane_ca_file=ca,
        reconnect_max_backoff_s=0.5,
    )
    agent = AgentRuntime(
        agent_settings, gateway=InProcessGateway(), command_runner=lambda args: None
    )
    token = client.post(f"/v1/sites/{site}/enrollment-tokens", {}).json()["secret"]
    loop = asyncio.new_event_loop()
    identity = loop.run_until_complete(agent.enroll(enroll_endpoint, token))
    tls = AgentTls(tmp_path / "agent")
    issued = tls.credentials()
    assert issued is not None
    assert pki.identity(issued[1]) == pki.server_uri(identity.server_id)

    # Sealed weights come over the same mutual TLS, with the agent's client certificate.
    (tmp_path / "weights").write_bytes(b"policy weights")
    sealing.generate_key_file(tmp_path / "kek")
    blob = io.BytesIO()
    header = sealing.seal_file(tmp_path / "weights", blob, sealing.FileKey(tmp_path / "kek"))
    uploaded = client.request(
        "PUT",
        f"/v1/projects/{project}/sealed-weights/{header.plaintext_digest}",
        content=blob.getvalue(),
    )
    assert uploaded.status_code == 201
    fetched = io.BytesIO()
    registry = NeuraRegistry(
        agent_settings, identity, IdentityStore(tmp_path / "agent").load_or_create_key()
    )
    assert registry(header.plaintext_digest, fetched)
    assert fetched.getvalue() == blob.getvalue()

    async def connect_without_certificate() -> grpc.StatusCode:
        roots = ca.read_bytes()
        key = IdentityStore(tmp_path / "agent").load_or_create_key()
        async with grpc.aio.secure_channel(
            endpoint, grpc.ssl_channel_credentials(root_certificates=roots)
        ) as channel:
            call = site_pb2_grpc.SiteControlStub(channel).Connect(
                iter([hello(site)]),
                metadata=(
                    ("authorization", f"Bearer {connection_token(key, identity.server_id)}"),
                ),
            )
            try:
                async for _ in call:
                    pass
            except grpc.aio.AioRpcError as exc:
                return exc.code()
        return grpc.StatusCode.OK

    # The token alone is not enough: the stream needs the server's client certificate too (the
    # TLS handshake fails without one).
    assert loop.run_until_complete(connect_without_certificate()) is grpc.StatusCode.UNAVAILABLE

    async def run_agent() -> str:
        await agent.start()
        try:
            await until(lambda: agent.connection and agent.connection.connected, "a connection")
            assert agent.connection is not None
            # Renewal: the agent asks for a new certificate over the stream and installs it.
            agent.connection.tls.needs_renewal = lambda: True  # type: ignore[method-assign]
            agent.connection._renew_if_due()
            for _ in range(200):
                renewed = tls.credentials()
                if renewed is not None and renewed[1] != issued[1]:
                    return renewed[1].decode()
                await asyncio.sleep(0.05)
            raise AssertionError("no renewed certificate")
        finally:
            await agent.stop()

    renewed = loop.run_until_complete(run_agent()).encode()
    loop.close()
    assert pki.identity(renewed) == pki.server_uri(identity.server_id)
    server = client.get(f"/v1/sites/{site}/servers").json()["items"][0]
    assert server["metadata"]["id"] == identity.server_id


def test_agent_upgrades_are_commands_to_one_server(harness: Harness) -> None:
    enrolled, key = harness.enrolled()
    path = f"/v1/servers/{enrolled.server_id}/agent-upgrades"
    body = {
        "version": "1.2.0",
        "bundle_url": "https://releases.example/neura-agent-1.2.0.tar",
        "bundle_sha256": "ab" * 32,
    }
    assert harness.client.post(path, {**body, "bundle_url": "http://x/b.tar"}).status_code == 422
    accepted = harness.client.post(path, body)
    assert accepted.status_code == 202, accepted.text
    operation = accepted.json()
    assert (operation["kind"], operation["target"]) == ("upgrade_agent", enrolled.server_id)
    messages = asyncio.run(
        connect(
            harness.endpoint,
            connection_token(key, enrolled.server_id),
            [hello(harness.site)],
            until=lambda r: "command" in kinds(r),
        )
    )
    command = next(m.command for m in messages if m.WhichOneof("body") == "command")
    assert command.WhichOneof("kind") == "upgrade_agent"
    assert command.operation_id == operation["metadata"]["id"]
    upgrade = command.upgrade_agent
    assert (upgrade.server_id, upgrade.version, upgrade.bundle_sha256) == (
        enrolled.server_id,
        "1.2.0",
        "ab" * 32,
    )
