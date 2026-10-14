"""Drive a running dev stack through a whole pilot, the way a customer would.

1. Onboard through the management API: project, site, contracts, robots, deployment, revision.
2. Enroll the site agent with the install command the API returned (run on the "server").
3. Prepare the revision: the site verifies the contracts it received in signed desired state.
4. Request activation; approve it locally at the site, as the site operator.
5. Check that every robot runs the revision and that the cloud sees it.
6. Stop the control plane: the robots keep running. Start it again: the site reconnects.

Usage: uv run python tools/smoke_api.py [--url http://127.0.0.1:8080] [--token ...] [--api-only]
The token defaults to the development bootstrap token in deploy/compose/compose.yaml.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "contracts"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy" / "compose" / "compose.yaml")]
AGENT_URL = "http://127.0.0.1:8090"
DATAPLANE_URL = "http://127.0.0.1:8091/v1/local/dataplane"
# DEVELOPMENT ONLY: the public operator credential of the Compose stack's site agent.
DEV_OPERATOR_TOKEN = "ntop_dev_operator_public_token"  # noqa: S105 - public dev value
ROBOTS = 4


def dev_token() -> str:
    compose = (ROOT / "deploy" / "compose" / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"NEURA_CONTROL_DEV_BOOTSTRAP_TOKEN: (\S+)", compose)
    if match is None:
        raise SystemExit("no development token in compose.yaml")
    return match.group(1)


def step(message: str) -> None:
    print(f"--> {message}", flush=True)


def wait_for[T](what: str, check: Callable[[], T | None], timeout: float = 60) -> T:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            result = check()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            last = exc
        else:
            if result:
                return result
            last = result
        time.sleep(1)
    raise SystemExit(f"timed out waiting for {what}; last: {last!r}")


def compose(*args: str) -> str:
    result = subprocess.run([*COMPOSE, *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"docker compose {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
    return result.stdout


def on_server(*args: str, env: dict[str, str] | None = None) -> str:
    """Run a command on the site's GPU server (the site-agent container)."""
    flags = [f for key, value in (env or {}).items() for f in ("-e", f"{key}={value}")]
    return compose("exec", "-T", *flags, "site-agent", *args)


class Api:
    def __init__(self, url: str, token: str) -> None:
        self.http = httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=10
        )

    def call(self, method: str, path: str, body: Any = None, expect: int = 200) -> dict[str, Any]:
        headers = {}
        if method == "POST":
            headers["Idempotency-Key"] = secrets.token_hex(16)
        response = self.http.request(method, path, json=body, headers=headers)
        if response.status_code != expect:
            raise SystemExit(
                f"{method} {path}: expected {expect}, got {response.status_code}: {response.text}"
            )
        data: dict[str, Any] = response.json() if response.content else {}
        return data

    def get(self, path: str) -> dict[str, Any]:
        return self.call("GET", path)

    def operation_done(self, operation_id: str) -> dict[str, Any] | None:
        operation = self.get(f"/v1/operations/{operation_id}")
        if operation["state"] in ("Failed", "Cancelled"):
            raise SystemExit(f"operation {operation_id} {operation['state']}: {operation}")
        return operation if operation["state"] == "Succeeded" else None


def onboard(api: Api) -> dict[str, Any]:
    me = api.get("/v1/me")
    org = me["organizations"][0]["id"]
    suffix = secrets.token_hex(3)
    project = api.call(
        "POST",
        f"/v1/organizations/{org}/projects",
        {"spec": {"name": f"smoke-{suffix}", "display_name": "Smoke test"}},
        201,
    )["metadata"]["id"]
    site = api.call(
        "POST",
        f"/v1/projects/{project}/sites",
        {
            "spec": {
                "name": "warehouse",
                "display_name": "Warehouse",
                "offline_authorization_hours": 24,
            }
        },
        201,
    )["metadata"]["id"]
    ids = {}
    for collection, name in (
        ("robot-profiles", "so101.robot.yaml"),
        ("model-artifacts", "act-so101.artifact.yaml"),
        ("execution-profiles", "act-so101-30hz.profile.yaml"),
    ):
        document = yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))
        ids[collection] = api.call(
            "POST", f"/v1/projects/{project}/{collection}", {"document": document}, 201
        )["metadata"]["id"]
    robots = [
        api.call(
            "POST",
            f"/v1/projects/{project}/robots",
            {
                "spec": {
                    "name": f"robot-{i}",
                    "display_name": f"Robot {i}",
                    "site_id": site,
                    "robot_profile_id": ids["robot-profiles"],
                }
            },
            201,
        )["metadata"]["id"]
        for i in range(ROBOTS)
    ]
    deployment = api.call(
        "POST",
        f"/v1/projects/{project}/deployments",
        {
            "spec": {
                "name": "pilot",
                "display_name": "Pilot",
                "site_id": site,
                "robot_ids": robots,
                "rollout": {"strategy": "all_at_task_boundary"},
            }
        },
        201,
    )["metadata"]["id"]
    revision = api.call(
        "POST",
        f"/v1/deployments/{deployment}/revisions",
        {
            "spec": {
                "model_artifact_id": ids["model-artifacts"],
                "execution_profile_id": ids["execution-profiles"],
                "robot_profile_id": ids["robot-profiles"],
            }
        },
        201,
    )
    if not revision["status"]["compatibility"]["compatible"]:
        raise SystemExit(f"example revision is incompatible: {revision['status']}")
    print(f"    organization {org}, project {project}, site {site}")
    print(f"    {ROBOTS} robots, deployment {deployment}, revision {revision['metadata']['id']}")
    return {
        "project": project,
        "site": site,
        "robots": robots,
        "deployment": deployment,
        "revision": revision["metadata"]["id"],
    }


def dataplane() -> dict[str, Any]:
    data: dict[str, Any] = httpx.get(DATAPLANE_URL, timeout=5).json()
    return data


def chunks() -> dict[str, int]:
    """Action chunks delivered per robot session; empty while the gateway has none (or is not
    started yet, before the first desired state)."""
    try:
        sessions = dataplane()["sessions"]
    except httpx.HTTPError:
        return {}
    return {s["robot_id"]: int(s["chunks"]) for s in sessions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default=None)
    parser.add_argument("--api-only", action="store_true", help="stop after onboarding")
    args = parser.parse_args()
    api = Api(args.url, args.token or dev_token())

    step("onboarding a customer through the management API")
    ids = onboard(api)
    if args.api_only:
        return 0

    step("enrolling the site agent with the install command from the API")
    enrollment = api.call(
        "POST", f"/v1/sites/{ids['site']}/enrollment-tokens", {"ttl_minutes": 30}, 201
    )
    print("    " + on_server(*shlex.split(enrollment["install_command"])).strip())
    wait_for(
        "the site to connect",
        lambda: api.get(f"/v1/sites/{ids['site']}")["status"]["connected"],
    )
    servers = api.get(f"/v1/sites/{ids['site']}/servers")["items"]
    checks = {c["name"]: c["passed"] for c in servers[0]["status"]["preflight"]}
    print(f"    server {servers[0]['metadata']['id']} connected; preflight {checks}")

    step("preparing the revision at the site")
    prepare = api.call("POST", f"/v1/revisions/{ids['revision']}/prepare", {}, 202)
    wait_for("prepare", lambda: api.operation_done(prepare["metadata"]["id"]))
    phase = api.get(f"/v1/revisions/{ids['revision']}")["status"]["phase"]
    print(f"    revision phase: {phase}")

    step("requesting activation; it waits for the site operator")
    activation = api.call(
        "POST",
        f"/v1/deployments/{ids['deployment']}/activation-requests",
        {"revision_id": ids["revision"]},
        202,
    )
    pending = wait_for(
        "the approval to reach the site",
        lambda: json.loads(on_server("neura-agent", "approvals"))["items"],
    )
    command_id = pending[0]["command_id"]
    print(f"    pending at the site: {command_id} for {pending[0]['robot_names']}")
    if chunks():
        raise SystemExit("robots are running before the activation was approved")

    step("approving locally at the site, as the site operator")
    on_server(
        "neura-agent",
        "approve",
        command_id,
        "--operator",
        "ops@warehouse",
        env={"NEURA_AGENT_OPERATOR_TOKEN": DEV_OPERATOR_TOKEN},
    )
    wait_for("the activation", lambda: api.operation_done(activation["metadata"]["id"]), timeout=90)
    active = api.get(f"/v1/deployments/{ids['deployment']}")["status"]["active_revision_ids"]
    if set(active.values()) != {ids["revision"]}:
        raise SystemExit(f"deployment does not show the revision active: {active}")

    step("checking every robot runs the revision")
    wait_for(
        f"{ROBOTS} robots receiving action chunks",
        lambda: len(c := chunks()) == ROBOTS and all(v >= 3 for v in c.values()),
    )
    status = dataplane()
    group = next(iter(status["groups"].values()))
    print(f"    sessions: {sorted(chunks().items())}")
    print(f"    batching: {group['batch_sizes']}, deadline misses: {group['deadline_misses']}")
    sessions = wait_for(
        "sessions in the control plane",
        lambda: (
            len(s := api.get(f"/v1/projects/{ids['project']}/sessions")["items"]) == ROBOTS and s
        ),
    )
    print(f"    the control plane sees {len(sessions)} sessions")

    step("stopping the control plane: the site keeps operating")
    compose("stop", "control-plane")
    before = chunks()
    time.sleep(5)
    after = chunks()
    if not all(after.get(robot, 0) > count for robot, count in before.items()):
        raise SystemExit(f"robots stopped during the outage: {before} -> {after}")
    agent = httpx.get(f"{AGENT_URL}/v1/local/status", timeout=5).json()
    print(
        f"    robots kept running ({sum(after.values()) - sum(before.values())} chunks in 5 s); "
        f"agent connected={agent['connection']['connected']}, offline authorization "
        f"{agent['offline_authorization']['remaining_s']} s"
    )

    step("starting the control plane: the site reconnects by itself")
    compose("start", "control-plane")
    wait_for("the control plane", lambda: httpx.get(f"{args.url}/readyz", timeout=2).is_success)
    wait_for(
        "the site to reconnect",
        lambda: api.get(f"/v1/sites/{ids['site']}")["status"]["connected"],
    )
    audit = api.get(f"/v1/projects/{ids['project']}/audit-records?page_size=200")["items"]
    print(f"    reconnected; {len(audit)} audit records")
    print("pilot smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
