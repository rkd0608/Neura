"""Sites with several servers (design §29.5): what one server reports changes only what is its own,
the robots it drives and its own connection, never the other servers'."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_site_control import Harness, hello

from conftest import AppFactory, contract
from neura.v1 import site_pb2, site_pb2_grpc
from neura_agent.identity import connection_token
from neura_contracts.site_protocol import CONTACT_NONCE_BYTES

Server = tuple[site_pb2.Enrolled, Ed25519PrivateKey]


@pytest.fixture
def harness(make_api: AppFactory, settings: Any) -> Harness:
    return Harness(make_api, settings)


def inventory(*names: str) -> site_pb2.AgentMessage:
    profile = json.dumps(contract("so101.robot.yaml"))
    return site_pb2.AgentMessage(
        inventory=site_pb2.RobotInventory(
            robots=[
                site_pb2.DiscoveredRobot(
                    local_name=name,
                    driver="simulated",
                    simulated=True,
                    state="ready",
                    robot_profile_json=profile,
                )
                for name in names
            ]
        )
    )


async def stream(
    harness: Harness,
    server: Server,
    outgoing: list[site_pb2.AgentMessage],
    *,
    opened: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
) -> None:
    """One stream of ``server``: its hello, ``outgoing`` and a contact challenge. ``opened`` is set
    once the control plane answers the challenge, which it does only after applying every report
    before it. The agent ends the stream once ``release`` is set (at once without one), and this
    returns when the control plane has closed it, after recording that the server disconnected."""
    enrolled, key = server
    challenge = site_pb2.AgentMessage(
        status=site_pb2.SiteStatus(contact_nonce=os.urandom(CONTACT_NONCE_BYTES))
    )

    async def requests() -> AsyncIterator[site_pb2.AgentMessage]:
        for message in [hello(harness.site), *outgoing, challenge]:
            yield message
        if release is not None:
            await release.wait()

    token = connection_token(key, enrolled.server_id)
    async with grpc.aio.insecure_channel(harness.endpoint) as channel:
        call = site_pb2_grpc.SiteControlStub(channel).Connect(
            requests(), metadata=(("authorization", f"Bearer {token}"),)
        )

        async def read() -> None:
            async for message in call:
                if message.WhichOneof("body") == "contact" and opened is not None:
                    opened.set()

        await asyncio.wait_for(read(), 20)


def test_one_servers_inventory_changes_only_the_robots_it_drives(harness: Harness) -> None:
    first, second = harness.enrolled(), harness.enrolled()
    asyncio.run(stream(harness, first, [inventory("sim-1")]))
    asyncio.run(stream(harness, second, [inventory("sim-2")]))
    asyncio.run(stream(harness, second, [inventory()]))  # the second server's robot is gone
    robots = {
        r["spec"]["name"]: r["status"]
        for r in harness.client.get(f"/v1/projects/{harness.project}/robots").json()["items"]
    }
    assert (robots["sim-1"]["present"], robots["sim-1"]["phase"]) == (True, "Ready")
    assert (robots["sim-2"]["present"], robots["sim-2"]["phase"]) == (False, "Offline")
    assert robots["sim-1"]["hardware"]["server_id"] == first[0].server_id


def test_a_site_stays_connected_while_any_of_its_servers_is(harness: Harness) -> None:
    first, second = harness.enrolled(), harness.enrolled()
    path = f"/v1/sites/{harness.site}"

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        opened, release = asyncio.Event(), asyncio.Event()
        staying = asyncio.create_task(stream(harness, second, [], opened=opened, release=release))
        await asyncio.wait_for(opened.wait(), 20)
        await stream(harness, first, [])  # the first server connects, and its stream ends
        during = (await asyncio.to_thread(harness.client.get, path)).json()["status"]
        release.set()
        await staying
        after = (await asyncio.to_thread(harness.client.get, path)).json()["status"]
        return during, after

    during, after = asyncio.run(scenario())
    assert (during["connected"], during["phase"]) == (True, "Ready")
    assert (after["connected"], after["phase"]) == (False, "Offline")
