"""Onboard a customer through the management API of a running dev stack.

Usage: uv run python tools/smoke_api.py [--url http://127.0.0.1:8080] [--token ...]
The token defaults to the development bootstrap token in deploy/compose/compose.yaml.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "contracts"


def dev_token() -> str:
    compose = (ROOT / "deploy" / "compose" / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"NEURA_CONTROL_DEV_BOOTSTRAP_TOKEN: (\S+)", compose)
    if match is None:
        raise SystemExit("no development token in compose.yaml")
    return match.group(1)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()
    api = Api(args.url, args.token or dev_token())

    me = api.call("GET", "/v1/me")
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
    enrollment = api.call("POST", f"/v1/sites/{site}/enrollment-tokens", {"ttl_minutes": 30}, 201)
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
        for i in range(4)
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
                "rollout": {"strategy": "single_robot_canary", "canary_robot_id": robots[0]},
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
    operation = api.call("POST", f"/v1/revisions/{revision['metadata']['id']}/prepare", {}, 202)
    audit = api.call("GET", f"/v1/projects/{project}/audit-records?page_size=200")
    print(f"organization {org}, project {project}, site {site}")
    print(f"enrollment command: {enrollment['install_command'][:60]}...")
    compatible = revision["status"]["compatibility"]["compatible"]
    print(f"revision {revision['metadata']['id']} compatible={compatible}")
    print(f"prepare operation {operation['metadata']['id']} state={operation['state']}")
    print("  (the operation completes when the site agent acknowledges it)")
    print(f"audit records: {len(audit['items'])}")
    print("API onboarding smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
