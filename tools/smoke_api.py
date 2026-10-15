"""Drive a running dev stack through a whole pilot, the way a customer would, using the SDK.

1. Onboard through the management API: project, site, contracts, robots, deployment, revision.
2. Enroll the site agent with the install command the API returned (run on the "server").
3. Trust the customer's publisher key at the site, then prepare the revision: the site verifies
   the contracts it received in signed desired state and the artifact's publisher signature.
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
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml

from neura_contracts import parse_document, sealing, signing
from neura_sdk import Client, NeuraApiError
from neura_sdk.models import ArtifactSignature, ContractDocument

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "contracts"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy" / "compose" / "compose.yaml")]
AGENT_URL = "http://127.0.0.1:8090"
DATAPLANE_URL = "http://127.0.0.1:8091/v1/local/dataplane"
# DEVELOPMENT ONLY: the public operator credential of the Compose stack's site agent.
DEV_OPERATOR_TOKEN = "ntop_dev_operator_public_token"  # noqa: S105 - public dev value
ROBOTS = 4
# The customer's release key, made for this run: it signs the artifact, and the site operator
# trusts it on the server (design §17.3).
PUBLISHER = signing.load_private_key(signing.generate_key())


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
        except (httpx.HTTPError, NeuraApiError, KeyError, ValueError) as exc:
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


def operation_done(neura: Client, operation_id: str) -> dict[str, Any] | None:
    operation = neura.get_operation(operation_id)
    if operation["state"] in ("Failed", "Cancelled"):
        raise SystemExit(f"operation {operation_id} {operation['state']}: {operation}")
    return dict(operation) if operation["state"] == "Succeeded" else None


def onboard(neura: Client) -> dict[str, Any]:
    org = neura.get_current_principal()["organizations"][0]["id"]
    suffix = secrets.token_hex(3)
    project = neura.create_project(
        org, body={"spec": {"name": f"smoke-{suffix}", "display_name": "Smoke test"}}
    )["metadata"]["id"]
    site = neura.create_site(
        project,
        body={
            "spec": {
                "name": "warehouse",
                "display_name": "Warehouse",
                "offline_authorization_hours": 24,
            }
        },
    )["metadata"]["id"]

    def document(name: str) -> ContractDocument:
        loaded: ContractDocument = yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))
        return loaded

    robot_profile = neura.register_robot_profile(
        project, body={"document": document("so101.robot.yaml")}
    )["metadata"]["id"]
    # The customer's release process signs the artifact; the site runs only trusted signatures.
    artifact_document = document("act-so101.artifact.yaml")
    signed = signing.sign(parse_document(artifact_document).digest(), PUBLISHER)
    signature = ArtifactSignature(
        key_id=signed["key_id"], public_key=signed["public_key"], signature=signed["signature"]
    )
    artifact = neura.register_model_artifact(
        project, body={"document": artifact_document, "signatures": [signature]}
    )["metadata"]["id"]
    execution_profile = neura.register_execution_profile(
        project, body={"document": document("act-so101-30hz.profile.yaml")}
    )["metadata"]["id"]
    robots = [
        neura.create_robot(
            project,
            body={
                "spec": {
                    "name": f"robot-{i}",
                    "display_name": f"Robot {i}",
                    "site_id": site,
                    "robot_profile_id": robot_profile,
                }
            },
        )["metadata"]["id"]
        for i in range(ROBOTS)
    ]
    deployment = neura.create_deployment(
        project,
        body={
            "spec": {
                "name": "pilot",
                "display_name": "Pilot",
                "site_id": site,
                "robot_ids": robots,
                "rollout": {"strategy": "all_at_task_boundary"},
            }
        },
    )["metadata"]["id"]
    revision = neura.create_revision(
        deployment,
        body={
            "spec": {
                "model_artifact_id": artifact,
                "execution_profile_id": execution_profile,
                "robot_profile_id": robot_profile,
            }
        },
    )
    compatibility = revision["status"].get("compatibility")
    if not compatibility or not compatibility["compatible"]:
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


def sealed_weights(neura: Client, project: str) -> str:
    """Seal a file with a customer key and upload it: the registry holds ciphertext only."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ckpt").mkdir()
        (root / "ckpt" / "model.safetensors").write_bytes(secrets.token_bytes(64 * 1024))
        sealing.generate_key_file(root / "kek")
        [(_, digest, _)] = sealing.seal_directory(
            root / "ckpt", root / "sealed", sealing.FileKey(root / "kek")
        )
        record = neura.upload_sealed_weights(
            project, root / "sealed" / "sha256" / digest.removeprefix("sha256:")
        )
    listed = [w["digest"] for w in neura.list_sealed_weights(project)["items"]]
    if digest not in listed:
        raise SystemExit(f"the registry does not list {digest}")
    return f"{digest[:19]}… sealed with {record['key_id']}, {record['sealed_bytes']} bytes"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--token", default=None)
    parser.add_argument("--api-only", action="store_true", help="stop after onboarding")
    args = parser.parse_args()
    neura = Client(args.url, token=args.token or dev_token(), max_retries=5)

    step("onboarding a customer through the management API")
    ids = onboard(neura)
    step("storing weights sealed with a customer key in Neura's registry")
    print(f"    {sealed_weights(neura, ids['project'])}")
    if args.api_only:
        return 0

    step("enrolling the site agent with the install command from the API")
    enrollment = neura.create_enrollment_token(ids["site"], body={"ttl_minutes": 30})
    print("    " + on_server(*shlex.split(enrollment["install_command"])).strip())
    wait_for(
        "the site to connect",
        lambda: neura.get_site(ids["site"])["status"].get("connected"),
    )
    servers = neura.list_servers(ids["site"])["items"]
    checks = {c["name"]: c["passed"] for c in servers[0]["status"].get("preflight", [])}
    print(f"    server {servers[0]['metadata']['id']} connected; preflight {checks}")

    step("trusting the customer's publisher key on the server, as the site operator")
    public = signing.raw_public_key(PUBLISHER).hex()
    print("    " + on_server("neura-agent", "trust-publisher", public, "--note", "smoke").strip())

    step("preparing the revision at the site")
    prepare = neura.prepare_revision(ids["revision"])
    wait_for("prepare", lambda: operation_done(neura, prepare["metadata"]["id"]))
    revision_status = neura.get_revision(ids["revision"])["status"]
    capacity = revision_status.get("capacity")
    if capacity is None:
        raise SystemExit("the site reported no capacity estimate for the prepared revision")
    print(
        f"    revision phase: {revision_status['phase']}; this server can run "
        f"{capacity['max_robots']} robots at this profile "
        f"(limited by {capacity['limiting_factor']})"
    )

    step("requesting activation; it waits for the site operator")
    activation = neura.request_activation(ids["deployment"], body={"revision_id": ids["revision"]})
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
    wait_for(
        "the activation", lambda: operation_done(neura, activation["metadata"]["id"]), timeout=90
    )
    active = neura.get_deployment(ids["deployment"])["status"].get("active_revision_ids", {})
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
        lambda: s if len(s := neura.list_sessions(ids["project"])["items"]) == ROBOTS else None,
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
        lambda: neura.get_site(ids["site"])["status"].get("connected"),
    )
    audit = list(neura.paginate(neura.list_audit_records, ids["project"], page_size=200))
    print(f"    reconnected; {len(audit)} audit records")
    print("pilot smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
