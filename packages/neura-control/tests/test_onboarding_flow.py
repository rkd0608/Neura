"""The full customer journey through the API (design §6 of the blueprint, §19-§20.9).

Every response is checked against api/openapi.yaml by the conformance client.
"""

from __future__ import annotations

import asyncio
from typing import Any

from conftest import ConformanceClient, contract
from neura_contracts import ReasonCode
from neura_control.db.engine import create_engine, session_factory
from neura_control.services import site_reports


def site_ack(database_url: str, operation_id: str, state: str, **kwargs: Any) -> None:
    """Simulate the site agent acknowledging an operation (the M4 stream will do this)."""

    async def run() -> None:
        engine = create_engine(database_url, pool_size=1)
        try:
            async with session_factory(engine).begin() as session:
                await site_reports.update_operation(session, operation_id, state, **kwargs)
        finally:
            await engine.dispose()

    asyncio.run(run())


def register(api: ConformanceClient, project: str, collection: str, name: str) -> dict[str, Any]:
    response = api.post(f"/v1/projects/{project}/{collection}", {"document": contract(name)})
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


def onboard(api: ConformanceClient, org: str) -> dict[str, Any]:
    project = api.post(
        f"/v1/organizations/{org}/projects",
        {"spec": {"name": "packing", "display_name": "Packing cells", "labels": {"region": "eu"}}},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["metadata"]["id"]

    site = api.post(
        f"/v1/projects/{project_id}/sites",
        {
            "spec": {
                "name": "warehouse-1",
                "display_name": "Warehouse 1",
                "offline_authorization_hours": 72,
            }
        },
    )
    assert site.status_code == 201, site.text
    site_id = site.json()["metadata"]["id"]
    assert site.json()["status"]["phase"] == "Unknown"
    assert site.json()["status"]["connected"] is False

    robot_profile = register(api, project_id, "robot-profiles", "so101.robot.yaml")
    artifact = register(api, project_id, "model-artifacts", "act-so101.artifact.yaml")
    profile = register(api, project_id, "execution-profiles", "act-so101-30hz.profile.yaml")

    robots = []
    for i in range(4):
        response = api.post(
            f"/v1/projects/{project_id}/robots",
            {
                "spec": {
                    "name": f"cell-{i}",
                    "display_name": f"Cell {i}",
                    "site_id": site_id,
                    "robot_profile_id": robot_profile["metadata"]["id"],
                }
            },
        )
        assert response.status_code == 201, response.text
        robots.append(response.json()["metadata"]["id"])

    return {
        "project": project_id,
        "site": site_id,
        "robot_profile": robot_profile["metadata"]["id"],
        "artifact": artifact["metadata"]["id"],
        "profile": profile["metadata"]["id"],
        "robots": robots,
    }


def test_onboarding_to_activation(api: tuple[ConformanceClient, str], settings: Any) -> None:
    client, org = api
    ids = onboard(client, org)
    project = ids["project"]

    # Connecting a server starts with a one-time enrollment token.
    token = client.post(f"/v1/sites/{ids['site']}/enrollment-tokens", {"ttl_minutes": 30})
    assert token.status_code == 201
    assert token.json()["secret"].startswith("nen_enr_")
    assert token.json()["secret"] in token.json()["install_command"]

    # Registering an identical document again returns the existing resource.
    again = client.post(
        f"/v1/projects/{project}/robot-profiles", {"document": contract("so101.robot.yaml")}
    )
    assert again.status_code == 200
    assert again.json()["metadata"]["id"] == ids["robot_profile"]

    check = client.post(
        f"/v1/projects/{project}/compatibility-checks",
        {
            "model_artifact_id": ids["artifact"],
            "robot_profile_id": ids["robot_profile"],
            "execution_profile_id": ids["profile"],
        },
        idempotency_key=None,
    )
    assert check.status_code == 200
    assert check.json()["compatible"] is True

    deployment = client.post(
        f"/v1/projects/{project}/deployments",
        {
            "spec": {
                "name": "packing-pilot",
                "display_name": "Packing pilot",
                "site_id": ids["site"],
                "robot_ids": ids["robots"],
                "rollout": {"strategy": "single_robot_canary", "canary_robot_id": ids["robots"][0]},
            }
        },
    )
    assert deployment.status_code == 201, deployment.text
    deployment_id = deployment.json()["metadata"]["id"]
    assert deployment.json()["status"]["phase"] == "Pending"
    assert set(deployment.json()["status"]["active_revision_ids"].values()) == {None}

    revision = client.post(
        f"/v1/deployments/{deployment_id}/revisions",
        {
            "spec": {
                "model_artifact_id": ids["artifact"],
                "execution_profile_id": ids["profile"],
                "robot_profile_id": ids["robot_profile"],
                "notes": "first qualified revision",
            }
        },
    )
    assert revision.status_code == 201, revision.text
    revision_id = revision.json()["metadata"]["id"]
    assert revision.json()["status"]["phase"] == "Created"
    assert revision.json()["status"]["compatibility"]["compatible"] is True

    # Activation before preparation is refused.
    early = client.post(
        f"/v1/deployments/{deployment_id}/activation-requests", {"revision_id": revision_id}
    )
    assert early.status_code == 409

    prepare = client.post(f"/v1/revisions/{revision_id}/prepare")
    assert prepare.status_code == 202
    operation_id = prepare.json()["metadata"]["id"]
    assert prepare.headers["Location"] == f"/v1/operations/{operation_id}"
    assert prepare.json()["state"] == "Pending"
    # A successful request is intent, not fact: nothing is prepared until the site says so.
    assert client.get(f"/v1/revisions/{revision_id}").json()["status"]["phase"] == "Preparing"

    site_ack(str(settings.database_url), operation_id, "Succeeded")
    assert client.get(f"/v1/operations/{operation_id}").json()["state"] == "Succeeded"
    assert client.get(f"/v1/revisions/{revision_id}").json()["status"]["phase"] == "Prepared"
    assert (
        client.get(f"/v1/deployments/{deployment_id}").json()["status"]["prepared_revision_id"]
        == revision_id
    )

    activation = client.post(
        f"/v1/deployments/{deployment_id}/activation-requests",
        {"revision_id": revision_id, "note": "canary on cell 0"},
    )
    assert activation.status_code == 202, activation.text
    activation_op = activation.json()["metadata"]["id"]
    requests = client.get(f"/v1/deployments/{deployment_id}/activation-requests").json()["items"]
    assert len(requests) == 1
    assert requests[0]["state"] == "AwaitingLocalApproval"
    assert requests[0]["robot_ids"] == [ids["robots"][0]]  # the canary only

    # Still nothing active until the site operator approves locally and the robot acknowledges.
    status = client.get(f"/v1/deployments/{deployment_id}").json()["status"]
    assert set(status["active_revision_ids"].values()) == {None}

    site_ack(
        str(settings.database_url),
        activation_op,
        "Succeeded",
        result={
            "approved_by": "operator@warehouse-1",
            "revision_id": revision_id,
            "robot_ids": [ids["robots"][0]],
        },
    )
    request = client.get(f"/v1/deployments/{deployment_id}/activation-requests").json()["items"][0]
    assert request["state"] == "Activated"
    assert request["approved_by"] == "operator@warehouse-1"
    status = client.get(f"/v1/deployments/{deployment_id}").json()["status"]
    assert status["phase"] == "Active"
    assert status["active_revision_ids"][ids["robots"][0]] == revision_id
    assert status["active_revision_ids"][ids["robots"][1]] is None
    robot = client.get(f"/v1/robots/{ids['robots'][0]}").json()
    assert robot["status"]["active_revision_id"] == revision_id
    assert robot["status"]["data_age_seconds"] is not None

    actions = {
        r["action"]
        for r in client.get(
            f"/v1/projects/{project}/audit-records", params={"page_size": 200}
        ).json()["items"]
    }
    assert {
        "project.create",
        "site.create",
        "robot.create",
        "deployment.create",
        "revision.create",
        "operation.prepare_revision",
        "operation.activate_revision",
    } <= actions


def test_refused_activation_is_recorded(api: tuple[ConformanceClient, str], settings: Any) -> None:
    client, org = api
    ids = onboard(client, org)
    deployment_id, revision_id = _prepared_deployment(client, ids, settings)
    activation = client.post(
        f"/v1/deployments/{deployment_id}/activation-requests", {"revision_id": revision_id}
    )
    site_ack(
        str(settings.database_url),
        activation.json()["metadata"]["id"],
        "Failed",
        reason=ReasonCode.AGENT_BOUNDARY_DENIED,
        message="operator refused",
        result={"refused": True},
    )
    request = client.get(f"/v1/deployments/{deployment_id}/activation-requests").json()["items"][0]
    assert request["state"] == "Refused"
    operation = client.get(f"/v1/operations/{activation.json()['metadata']['id']}").json()
    assert operation["reason"] == "AGENT_BOUNDARY_DENIED"
    status = client.get(f"/v1/deployments/{deployment_id}").json()["status"]
    assert set(status["active_revision_ids"].values()) == {None}


def _prepared_deployment(
    client: ConformanceClient, ids: dict[str, Any], settings: Any
) -> tuple[str, str]:
    deployment = client.post(
        f"/v1/projects/{ids['project']}/deployments",
        {
            "spec": {
                "name": "pilot",
                "display_name": "Pilot",
                "site_id": ids["site"],
                "robot_ids": ids["robots"][:2],
                "rollout": {"strategy": "all_at_task_boundary"},
            }
        },
    )
    deployment_id = deployment.json()["metadata"]["id"]
    revision = client.post(
        f"/v1/deployments/{deployment_id}/revisions",
        {
            "spec": {
                "model_artifact_id": ids["artifact"],
                "execution_profile_id": ids["profile"],
                "robot_profile_id": ids["robot_profile"],
            }
        },
    )
    revision_id = revision.json()["metadata"]["id"]
    prepare = client.post(f"/v1/revisions/{revision_id}/prepare")
    site_ack(str(settings.database_url), prepare.json()["metadata"]["id"], "Succeeded")
    return deployment_id, revision_id


def test_rollback_and_cancel(api: tuple[ConformanceClient, str], settings: Any) -> None:
    client, org = api
    ids = onboard(client, org)
    deployment_id, revision_id = _prepared_deployment(client, ids, settings)
    rollback = client.post(
        f"/v1/deployments/{deployment_id}/rollbacks", {"to_revision_id": revision_id}
    )
    assert rollback.status_code == 202
    operation_id = rollback.json()["metadata"]["id"]
    cancelled = client.post(f"/v1/operations/{operation_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "Cancelled"
    again = client.post(f"/v1/operations/{operation_id}/cancel")
    assert again.status_code == 409
    # A late success after cancellation is ignored.
    site_ack(
        str(settings.database_url),
        operation_id,
        "Succeeded",
        result={"revision_id": revision_id, "robot_ids": ids["robots"][:2]},
    )
    assert client.get(f"/v1/operations/{operation_id}").json()["state"] == "Cancelled"
