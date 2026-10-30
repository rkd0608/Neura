"""The generated Python SDK against the real control plane over HTTP, and its transport rules
(idempotency keys, If-Match, retries, errors) against a mock transport."""

from __future__ import annotations

import concurrent.futures
import json
import time
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from conftest import LiveApi, contract
from neura_contracts import sealing
from neura_control.settings import ControlPlaneSettings
from neura_sdk import Client, NeuraApiError
from neura_sdk.models import ContractDocument


def test_onboarding_through_the_sdk(live_api: LiveApi) -> None:
    with Client(live_api.url, token=live_api.token) as neura:
        me = neura.get_current_principal()
        assert me["organizations"][0]["id"] == live_api.organization_id
        project = neura.create_project(
            live_api.organization_id, body={"spec": {"name": "sdk", "display_name": "SDK"}}
        )
        project_id = project["metadata"]["id"]
        for i in range(3):
            neura.create_site(
                project_id,
                body={
                    "spec": {
                        "name": f"site-{i}",
                        "display_name": f"Site {i}",
                        "offline_authorization_hours": 4,
                    }
                },
            )
        names = [
            s["spec"]["name"] for s in neura.paginate(neura.list_sites, project_id, page_size=2)
        ]
        assert names == ["site-0", "site-1", "site-2"]

        profile = neura.register_robot_profile(
            project_id, body={"document": cast(ContractDocument, contract("so101.robot.yaml"))}
        )
        site = neura.list_sites(project_id)["items"][0]
        robot = neura.create_robot(
            project_id,
            body={
                "spec": {
                    "name": "arm-1",
                    "display_name": "Arm 1",
                    "site_id": site["metadata"]["id"],
                    "robot_profile_id": profile["metadata"]["id"],
                }
            },
        )
        renamed = neura.update_robot(
            robot["metadata"]["id"],
            body={"spec": {"display_name": "Arm one"}},
            if_match=robot["metadata"]["version"],
        )
        assert renamed["spec"]["display_name"] == "Arm one"

        with pytest.raises(NeuraApiError) as stale:
            neura.update_robot(
                robot["metadata"]["id"],
                body={"spec": {"display_name": "Arm 1 again"}},
                if_match=robot["metadata"]["version"],
            )
        assert (stale.value.status, stale.value.reason) == (412, "API_PRECONDITION_FAILED")

        member = neura.add_member(
            live_api.organization_id, body={"email": "ops@customer.example", "role": "viewer"}
        )
        assert member["state"] == "Invited"
        # Follow the event stream across server-ended streams: nothing between them is lost.
        events = neura.events(project_id)
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            first = pool.submit(next, events)
            time.sleep(1.5)  # longer than one stream's lifetime in tests
            neura.update_robot(
                robot["metadata"]["id"],
                body={"spec": {"labels": {"line": "a"}}},
                if_match=renamed["metadata"]["version"],
            )
            event = first.result(timeout=10)
        assert (event["type"], event["resource"]) == ("resource.updated", robot["metadata"]["id"])


def test_sealed_weights_upload_through_the_sdk(
    settings: ControlPlaneSettings, tmp_path: Path
) -> None:
    live = LiveApi(settings.model_copy(update={"sealed_weights_dir": tmp_path / "registry"}), {})
    try:
        (tmp_path / "ckpt").mkdir()
        (tmp_path / "ckpt" / "model.safetensors").write_bytes(b"\x00weights" * 50_000)
        sealing.generate_key_file(tmp_path / "kek")
        sealed = sealing.seal_directory(
            tmp_path / "ckpt", tmp_path / "sealed", sealing.FileKey(tmp_path / "kek")
        )
        digest = sealed[0][1]
        with Client(live.url, token=live.token) as neura:
            project = neura.create_project(
                live.organization_id, body={"spec": {"name": "w", "display_name": "W"}}
            )["metadata"]["id"]
            blob = tmp_path / "sealed" / "sha256" / digest.removeprefix("sha256:")
            record = neura.upload_sealed_weights(project, blob)
            assert record["digest"] == digest
            assert record["plaintext_bytes"] == 400_000
            listed = neura.list_sealed_weights(project)["items"]
            assert [w["digest"] for w in listed] == [digest]
            with pytest.raises(NeuraApiError) as refused:
                neura.upload_sealed_weights(project, tmp_path / "ckpt" / "model.safetensors")
            assert refused.value.reason == "API_VALIDATION_FAILED"
    finally:
        live.close()


def mock_client(handler: Any) -> Client:
    client = Client("http://neura.test", token="ntk_tok_x", transport=httpx.MockTransport(handler))
    client._sleep = lambda _: None
    return client


def test_a_retried_post_keeps_its_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) < 3:
            return httpx.Response(
                503, json={"reason": "API_UNAVAILABLE"}, headers={"Retry-After": "1"}
            )
        return httpx.Response(201, json={"metadata": {"id": "prj_1"}})

    created = mock_client(handler).create_project("org_1", body={"spec": {}})  # type: ignore[typeddict-item]
    assert created["metadata"]["id"] == "prj_1"
    keys = {r.headers["Idempotency-Key"] for r in seen}
    assert len(seen) == 3
    assert len(keys) == 1
    assert seen[0].headers["Authorization"] == "Bearer ntk_tok_x"
    assert json.loads(seen[0].content) == {"spec": {}}


def test_a_problem_marked_retryable_is_retried_after_the_delay_it_asks_for() -> None:
    seen: list[httpx.Request] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            problem = {"reason": "API_INTERNAL_ERROR", "status": 500, "retryable": True}
            return httpx.Response(500, json=problem, headers={"Retry-After": "2"})
        return httpx.Response(201, json={"metadata": {"id": "prj_1"}})

    client = mock_client(handler)
    client._sleep = slept.append
    created = client.create_project("org_1", body={"spec": {}})  # type: ignore[typeddict-item]
    assert created["metadata"]["id"] == "prj_1"
    assert len(seen) == 2
    assert len({r.headers["Idempotency-Key"] for r in seen}) == 1
    assert slept == [2.0]


def test_a_problem_not_marked_retryable_is_raised_at_once() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503, json={"reason": "API_UNAVAILABLE", "retryable": False})

    with pytest.raises(NeuraApiError) as raised:
        mock_client(handler).get_site("site_1")
    assert raised.value.reason == "API_UNAVAILABLE"
    assert len(seen) == 1  # the problem says repeating it will not help, whatever its status


def test_problems_raise_with_their_reason_code_and_retries_end() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"reason": "API_RATE_LIMITED", "title": "Slow down", "retryable": True}
        )

    with pytest.raises(NeuraApiError) as raised:
        mock_client(handler).get_site("site_1")
    assert (raised.value.status, raised.value.reason, raised.value.retryable) == (
        429,
        "API_RATE_LIMITED",
        True,
    )


def test_if_match_is_quoted_and_merge_patch_is_used() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = mock_client(handler)
    client.delete_robot("rbt_1", if_match="7")
    client.update_member("mbr_1", body={"role": "admin"}, if_match='"8"')
    assert [r.headers["If-Match"] for r in seen] == ['"7"', '"8"']
    assert seen[1].headers["Content-Type"] == "application/merge-patch+json"
    assert "Idempotency-Key" not in seen[0].headers


def test_path_parameters_are_escaped() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    mock_client(handler).get_robot("../admin")
    assert seen[0].url.raw_path == b"/v1/robots/..%2Fadmin"
