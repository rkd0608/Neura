"""Mutual TLS between robots and the gateway (design §17.3): only certificates from the site CA
that are still allowed and name the robot and the site are served."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import grpc
import pytest

from neura_contracts import ReasonCode, pki
from neura_runtime.binding import load_binding
from neura_runtime.grpc_client import RobotCredentials, fetch_assignment
from neura_runtime.runtime import RuntimeState
from neura_server.config import (
    DeploymentConfig,
    DeviceConfig,
    GatewayConfig,
    ServingConfig,
    TlsConfig,
)
from neura_server.grpc_server import GatewayServer
from neura_sim.grpc_robots import SimulatedRobotProcess

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "contracts"
ARTIFACT = EXAMPLES / "act-so101.artifact.yaml"
ROBOT = EXAMPLES / "so101.robot.yaml"
PROFILE = EXAMPLES / "act-so101-30hz.profile.yaml"
SITE = "site_1"


class Site:
    """A site CA, the gateway's TLS files, and robot credentials."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.ca = pki.CertificateAuthority.create("test site CA")
        key, cert = self.ca.issue(
            identity_uri=pki.gateway_uri(SITE),
            days=30,
            server_names=["localhost", "127.0.0.1"],
            client=False,
        )
        self.write("server.key", key)
        self.write("server.pem", cert)
        self.write("ca.pem", self.ca.certificate_pem)

    def write(self, name: str, data: bytes) -> Path:
        path = self.directory / name
        path.write_bytes(data)
        return path

    def robot(
        self, name: str, *, ca: pki.CertificateAuthority | None = None, site: str = SITE
    ) -> RobotCredentials:
        key = pki.new_key()
        cert = (ca or self.ca).sign_request(
            pki.signing_request(key, name), identity_uri=pki.robot_uri(site, name), days=30
        )
        return RobotCredentials(
            ca_pem=self.ca.certificate_pem, certificate_pem=cert, key_pem=pki.key_pem(key)
        )

    def config(self, allowed: dict[str, RobotCredentials]) -> GatewayConfig:
        assert all(c.certificate_pem for c in allowed.values())
        return GatewayConfig(
            site_id=SITE,
            listen="127.0.0.1:0",
            status_listen=None,
            tls=TlsConfig(
                certificate=self.directory / "server.pem",
                key=self.directory / "server.key",
                client_ca=self.directory / "ca.pem",
            ),
            devices=[
                DeviceConfig(
                    robot_id=name,
                    certificate_sha256=[pki.fingerprint(c.certificate_pem or b"")],
                )
                for name, c in allowed.items()
            ],
            deployments=[
                DeploymentConfig(
                    revision_id="rev_1",
                    artifact=ARTIFACT,
                    robot_profile=ROBOT,
                    execution_profile=PROFILE,
                    robots=sorted(allowed) or ["nobody"],
                )
            ],
            assignments=dict.fromkeys(allowed, "rev_1"),
            serving=ServingConfig(backend="simulated", simulate_latency=False),
        )


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(tmp_path)


async def test_a_robot_with_a_site_certificate_is_served_and_told_what_to_run(
    site: Site,
) -> None:
    credentials = site.robot("robot-0")
    server = GatewayServer(site.config({"robot-0": credentials}))
    await server.start()
    target = f"127.0.0.1:{server.bound_port}"
    try:
        assignment = await fetch_assignment(target, credentials)
        assert assignment.revision_id == "rev_1"
        assert json.loads(assignment.documents["model_artifact"])["kind"] == "ModelArtifact"
        robot = SimulatedRobotProcess(
            load_binding(ARTIFACT, ROBOT, PROFILE), "robot-0", "rev_1", target, credentials
        )
        stop = asyncio.Event()
        task = asyncio.create_task(robot.connection.run(stop))
        for _ in range(100):
            if robot.runtime.state is RuntimeState.RUNNING:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await task
        assert robot.runtime.state is RuntimeState.RUNNING
    finally:
        await server.stop()


async def test_certificates_from_elsewhere_or_for_others_are_refused(site: Site) -> None:
    good = site.robot("robot-0")
    server = GatewayServer(site.config({"robot-0": good}))
    await server.start()
    target = f"127.0.0.1:{server.bound_port}"
    try:
        foreign = site.robot("robot-0", ca=pki.CertificateAuthority.create("someone else"))
        with pytest.raises(grpc.aio.AioRpcError) as handshake:
            await fetch_assignment(target, foreign)
        assert handshake.value.code() is grpc.StatusCode.UNAVAILABLE  # the TLS handshake failed
        # Issued by the site CA, but never approved for this gateway (or revoked).
        unknown = site.robot("robot-0")
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await fetch_assignment(target, unknown)
        assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED
        # A token does not replace a certificate.
        with pytest.raises(grpc.aio.AioRpcError):
            await fetch_assignment(target, RobotCredentials(token="dev-token-robot-0"))
    finally:
        await server.stop()


async def test_a_certificate_must_name_its_robot_and_site(site: Site) -> None:
    other_site = site.robot("robot-0", site="site_2")
    server = GatewayServer(site.config({"robot-0": other_site}))
    await server.start()
    try:
        with pytest.raises(grpc.aio.AioRpcError) as refused:
            await fetch_assignment(f"127.0.0.1:{server.bound_port}", other_site)
        assert refused.value.code() is grpc.StatusCode.UNAUTHENTICATED
    finally:
        await server.stop()


async def test_revoking_a_robot_ends_its_session(site: Site) -> None:
    credentials = site.robot("robot-0")
    config = site.config({"robot-0": credentials})
    server = GatewayServer(config)
    await server.start()
    target = f"127.0.0.1:{server.bound_port}"
    robot = SimulatedRobotProcess(
        load_binding(ARTIFACT, ROBOT, PROFILE), "robot-0", "rev_1", target, credentials
    )
    stop = asyncio.Event()
    task = asyncio.create_task(robot.connection.run(stop))
    try:
        for _ in range(100):
            if server.core.sessions and server.status()["streams"]:
                break
            await asyncio.sleep(0.05)
        assert server.core.sessions
        closed = server.reload(config.model_copy(update={"devices": []}))
        assert closed
        assert not server.core.sessions
        # The robot is told why: its credential was revoked.
        for _ in range(100):
            if robot.runtime.counters[ReasonCode.AUTH_DEVICE_REVOKED.value]:
                break
            await asyncio.sleep(0.05)
        assert robot.runtime.counters[ReasonCode.AUTH_DEVICE_REVOKED.value]
        with pytest.raises(grpc.aio.AioRpcError):
            await fetch_assignment(target, credentials)
    finally:
        stop.set()
        await task
        await server.stop()


async def test_a_renewed_server_certificate_is_used_without_a_restart(site: Site) -> None:
    credentials = site.robot("robot-0")
    server = GatewayServer(site.config({"robot-0": credentials}))
    await server.start()
    target = f"127.0.0.1:{server.bound_port}"
    try:
        await fetch_assignment(target, credentials)
        key, cert = site.ca.issue(
            identity_uri=pki.gateway_uri(SITE),
            days=30,
            server_names=["localhost", "127.0.0.1"],
            client=False,
        )
        site.write("server.key", key)
        site.write("server.pem", cert)
        assert (await fetch_assignment(target, credentials)).revision_id == "rev_1"
    finally:
        await server.stop()
