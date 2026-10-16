"""The site kit (deploy/site-demo) end to end, the way a customer uses it, against the development
stack's control plane (deploy/compose) and with the published ALOHA checkpoint from the Hub.

1. A project, through the API (a customer would use the console for this and steps 3 and 5).
2. ./site.sh publish: the checkpoint, its contracts, signed and registered.
3. A site, two robots and an enrollment token.
4. ./site.sh connect with the install command: the site agent enrolls, trusts the publisher and
   imports the weights; the robot cell starts.
5. A deployment and a revision: prepare (the site checks the signature, loads and measures the
   model), then activation, approved by the site operator (./site.sh approve).
6. The robots run tasks: each completes some without a fault. The cell's cameras are saved.

Usage: uv run python tools/smoke_site_kit.py [--tasks 3] [--robots 2] [--refill-at N]
                                            [--out DIR] [--checkpoint REPO|DIR]
Needs the development control plane:
    docker compose -f deploy/compose/compose.yaml up -d control-plane
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from neura_sdk import Client, NeuraApiError

ROOT = Path(__file__).resolve().parent.parent
KIT = ROOT / "deploy" / "site-demo"
API = "http://127.0.0.1:8080"
# The control plane as the kit's containers reach it, on the development stack's network.
API_IN_NETWORK = "http://control-plane:8080"
CELL = "http://127.0.0.1:8095"


def step(message: str) -> None:
    print(f"--> {message}", flush=True)


def wait_for[T](what: str, check: Callable[[], T | None], timeout: float) -> T:
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
        time.sleep(2)
    raise SystemExit(f"timed out waiting for {what}; last: {last!r}")


def dev_token() -> str:
    compose = (ROOT / "deploy" / "compose" / "compose.yaml").read_text(encoding="utf-8")
    for line in compose.splitlines():
        if "NEURA_CONTROL_DEV_BOOTSTRAP_TOKEN:" in line:
            return line.split(":", 1)[1].strip()
    raise SystemExit("no development token in deploy/compose/compose.yaml")


def site_sh(*args: str, token: str = "") -> str:
    env = {
        **os.environ,
        "COMPOSE_FILE": "compose.yaml:compose.dev.yaml",
        "NEURA_TOKEN": token,
    }
    result = subprocess.run(
        ["./site.sh", *args], cwd=KIT, env=env, capture_output=True, text=True, check=False
    )
    print(result.stdout + result.stderr, flush=True)
    if result.returncode != 0:
        raise SystemExit(f"./site.sh {args[0]} failed ({result.returncode})")
    return result.stdout


def operation_done(neura: Client, operation_id: str) -> dict[str, Any] | None:
    operation = neura.get_operation(operation_id)
    if operation["state"] in ("Failed", "Cancelled"):
        raise SystemExit(f"operation {operation_id} {operation['state']}: {operation}")
    return dict(operation) if operation["state"] == "Succeeded" else None


def cell() -> dict[str, Any]:
    body: dict[str, Any] = httpx.get(f"{CELL}/api/cell", timeout=5).json()
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=3, help="tasks each robot must complete")
    parser.add_argument(
        "--robots", type=int, default=2, help="robots at the site (as many as it can serve)"
    )
    parser.add_argument(
        "--refill-at", type=int, default=None, help="the execution profile's (default: the kit's)"
    )
    parser.add_argument("--checkpoint", default=None, help="default: the kit's")
    parser.add_argument("--out", type=Path, default=ROOT / "out" / "site-kit")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    names = [f"aloha-{i}" for i in range(1, args.robots + 1)]
    token = dev_token()
    neura = Client(API, token=token, max_retries=5)

    step("a project (the console's New project)")
    org = neura.get_current_principal()["organizations"][0]["id"]
    project = neura.create_project(
        org, body={"spec": {"name": f"kit-{secrets.token_hex(3)}", "display_name": "My lab"}}
    )["metadata"]["id"]

    step("./site.sh publish: the ML team's release")
    extra = ["--checkpoint", args.checkpoint] if args.checkpoint else []
    if args.refill_at is not None:
        extra += ["--refill-at", str(args.refill_at)]
    site_sh("publish", f"{API_IN_NETWORK}/o/{org}/p/{project}", *extra, token=token)
    contracts = {
        "robot_profile_id": neura.list_robot_profiles(project)["items"][0]["metadata"]["id"],
        "model_artifact_id": neura.list_model_artifacts(project)["items"][0]["metadata"]["id"],
        "execution_profile_id": neura.list_execution_profiles(project)["items"][0]["metadata"][
            "id"
        ],
    }

    step("a site and its robots, and a server's enrollment (the console's Add a server)")
    site = neura.create_site(
        project,
        body={
            "spec": {"name": "my-lab", "display_name": "My lab", "offline_authorization_hours": 24}
        },
    )["metadata"]["id"]
    robots = [
        neura.create_robot(
            project,
            body={
                "spec": {
                    "name": name,
                    "display_name": name.upper(),
                    "site_id": site,
                    "robot_profile_id": contracts["robot_profile_id"],
                }
            },
        )["metadata"]["id"]
        for name in names
    ]
    install = neura.create_enrollment_token(site, body={"ttl_minutes": 30})["install_command"]

    step("./site.sh connect: the site's server, enrolled; the robot cell")
    site_sh("connect", install)
    wait_for("the site to connect", lambda: neura.get_site(site)["status"].get("connected"), 120)
    wait_for(
        f"{len(names)} robots in the cell",
        lambda: len(cell()["robots"]) == len(names) or None,
        120,
    )

    step("a deployment and revision; prepare")
    deployment = neura.create_deployment(
        project,
        body={
            "spec": {
                "name": "hand-over",
                "display_name": "Hand over",
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
                "model_artifact_id": contracts["model_artifact_id"],
                "execution_profile_id": contracts["execution_profile_id"],
                "robot_profile_id": contracts["robot_profile_id"],
            }
        },
    )["metadata"]["id"]
    prepare = neura.prepare_revision(revision)["metadata"]["id"]
    wait_for("prepare", lambda: operation_done(neura, prepare), 900)
    capacity = neura.get_revision(revision)["status"].get("capacity")
    if capacity is None:
        raise SystemExit("the site reported no capacity for the prepared revision")
    latency = capacity["assumptions"]["latency_p99_ms_by_batch"]
    print(
        f"    capacity: {capacity['max_robots']} robots on this server; p99 ms by batch {latency}"
    )
    if capacity["max_robots"] < len(names):
        raise SystemExit(
            f"this server can serve {capacity['max_robots']} robots at this profile, not "
            f"{len(names)} (try --robots or --refill-at): {capacity}"
        )

    step("activation, approved by the site operator")
    activation = neura.request_activation(deployment, body={"revision_id": revision})
    wait_for(
        "the approval to reach the site",
        lambda: httpx.get(f"{CELL}/api/site", timeout=5).json()["pending_approvals"],
        120,
    )
    site_sh("approve")
    wait_for("the activation", lambda: operation_done(neura, activation["metadata"]["id"]), 300)

    step(f"the robots at work: {args.tasks} tasks each")

    def worked() -> dict[str, Any] | None:
        status = cell()
        if any(r.get("fault") for r in status["robots"]):
            raise SystemExit(f"a robot faulted: {json.dumps(status, indent=1)}")
        done = all(r["tasks"] >= args.tasks for r in status["robots"])
        return status if done else None

    status = wait_for("the tasks", worked, 120 + args.tasks * 40)
    for robot in status["robots"]:
        print(
            f"    {robot['name']}: {robot['successes']}/{robot['tasks']} tasks succeeded, "
            f"{robot['chunks']} chunks, round trip p99 {robot['rtt_p99_ms']} ms"
        )
        for camera in ("angle", "top"):
            frame = httpx.get(
                f"{CELL}/robots/{robot['name']}/frame.jpg", params={"camera": camera}, timeout=10
            )
            (args.out / f"{robot['name']}-{camera}.jpg").write_bytes(frame.content)
    (args.out / "cell.json").write_text(json.dumps(status, indent=1), encoding="utf-8")
    sessions = neura.list_sessions(project)["items"]
    print(f"    the control plane sees {len(sessions)} sessions")
    print("site kit smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
