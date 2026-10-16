"""The staging site (deploy/gcp/site.sh) against the deployed Neura, over the internet: the
customer's whole flow, then real network faults on the site's uplink. Run by `site.sh test`.

The customer's steps are those of deploy/site-demo/README.md: the console's calls go through the
API from here, and the site operator's commands run on the site's server over SSH through IAP.
1. A project; ./site.sh publish: the published ALOHA checkpoint from the Hub, signed and
   registered.
2. A site and its robots, an enrollment token, then ./site.sh connect with its install command.
3. A deployment and a revision; prepare; activation, approved at the site (./site.sh approve).
4. The robots at work, then, on the site server's network interface (its internet connection):
   - impaired: 150 ms (+/- 30 ms) of added latency and 2% packet loss, for --impaired seconds;
   - partitioned: every packet to Neura's address dropped, for --partition seconds.
   For each phase: the tasks the robots completed and any faults, and what Neura saw of the site
   (connected or not). The robots must keep working through both, Neura must notice the
   partition, and the site must reconnect after it.

Results go to --out: results.json and each robot's cameras.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neura_sdk import Client, NeuraApiError

ROOT = Path(__file__).resolve().parent.parent
KIT = "/opt/neura-site"


def step(message: str) -> None:
    print(f"--> {message}", flush=True)


@dataclass
class Site:
    """The site's server, over SSH through IAP."""

    vm: str
    zone: str
    project: str

    def run(self, command: str, *, stdin: str | None = None, timeout: float = 900) -> str:
        args = [
            "gcloud",
            "--project",
            self.project,
            "--quiet",
            "compute",
            "ssh",
            self.vm,
            "--zone",
            self.zone,
            "--tunnel-through-iap",
            "--command",
            command,
        ]
        result = subprocess.run(
            args, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"on the site: {command.split(' --token ')[0]!r} failed ({result.returncode}):\n"
                + (result.stdout + result.stderr)[-3000:]
            )
        return result.stdout

    def kit(self, *args: str, stdin: str | None = None) -> str:
        quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
        out = self.run(f"sudo {KIT}/site.sh {quoted}", stdin=stdin)
        print("    " + out.strip().replace("\n", "\n    "), flush=True)
        return out

    def cell(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.run("curl -fsS http://127.0.0.1:8095/api/cell"))
        return body

    def uplink(self) -> str:
        return self.run("ip route show default | awk '{print $5; exit}'").strip()


@dataclass
class Phase:
    name: str
    description: str
    seconds: float
    samples: list[dict[str, Any]] = field(default_factory=list)
    tasks: dict[str, int] = field(default_factory=dict)
    successes: dict[str, int] = field(default_factory=dict)
    faults: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        seen = [s["neura_sees_connected"] for s in self.samples]
        return {
            "phase": self.name,
            "description": self.description,
            "seconds": self.seconds,
            "tasks_completed": self.tasks,
            "successes": self.successes,
            "faults": self.faults,
            "neura_saw_connected": f"{sum(seen)}/{len(seen)} samples",
            "first_seen_offline_s": next(
                (s["t"] for s in self.samples if not s["neura_sees_connected"]), None
            ),
            "samples": self.samples,
        }


def wait_for[T](what: str, check: Callable[[], T | None], timeout: float) -> T:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            result = check()
        except (NeuraApiError, RuntimeError, KeyError, ValueError) as exc:
            last = exc
        else:
            if result:
                return result
            last = result
        time.sleep(3)
    raise SystemExit(f"timed out waiting for {what}; last: {last!r}")


def operation_done(neura: Client, operation_id: str) -> dict[str, Any] | None:
    operation = neura.get_operation(operation_id)
    if operation["state"] in ("Failed", "Cancelled"):
        raise SystemExit(f"operation {operation_id} {operation['state']}: {operation}")
    return dict(operation) if operation["state"] == "Succeeded" else None


def observe(
    neura: Client, site: Site, site_id: str, phase: Phase, *, every_s: float = 10.0
) -> None:
    """Sample the robots and Neura's view of the site for the phase's duration."""
    start = time.monotonic()
    before = {r["name"]: r for r in site.cell()["robots"]}
    while (elapsed := time.monotonic() - start) < phase.seconds:
        robots = site.cell()["robots"]
        try:
            connected = bool(neura.get_site(site_id)["status"].get("connected"))
        except NeuraApiError:
            connected = False
        phase.samples.append(
            {
                "t": round(elapsed),
                "neura_sees_connected": connected,
                "robots": {r["name"]: r["state"] for r in robots},
                "tasks": {r["name"]: r["tasks"] for r in robots},
            }
        )
        for robot in robots:
            if robot.get("fault") and robot["fault"] not in phase.faults:
                phase.faults.append(f"{robot['name']}: {robot['fault']}")
        time.sleep(max(0.0, every_s - (time.monotonic() - start - elapsed)))
    after = {r["name"]: r for r in site.cell()["robots"]}
    for name, robot in after.items():
        phase.tasks[name] = robot["tasks"] - before[name]["tasks"]
        phase.successes[name] = robot["successes"] - before[name]["successes"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="the console's URL")
    parser.add_argument("--neura-address", required=True, help="Neura's public address")
    parser.add_argument("--project-id", required=True, help="the Google Cloud project")
    parser.add_argument("--vm", required=True)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--robots", type=int, default=2)
    parser.add_argument("--refill-at", type=int, default=None)
    parser.add_argument("--baseline", type=float, default=90, help="seconds of plain work")
    parser.add_argument("--impaired", type=float, default=90)
    parser.add_argument("--partition", type=float, default=120)
    parser.add_argument("--out", type=Path, default=ROOT / "out" / "staging-site")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("NEURA_TOKEN", "")
    if not token:
        raise SystemExit("set NEURA_TOKEN: an API token of the deployed Neura")
    neura = Client(args.url, token=token, max_retries=5)
    site = Site(args.vm, args.zone, args.project_id)
    names = [f"aloha-{i}" for i in range(1, args.robots + 1)]
    results: dict[str, Any] = {"url": args.url, "vm": args.vm, "zone": args.zone}

    step("the site's server: a clean start (its enrollment and state are deleted)")
    site.run(
        f"cd {KIT} && sudo docker compose --profile tools down --volumes --remove-orphans "
        f">/dev/null 2>&1; sudo sed -i '/^NEURA_URL=/d;/^NEURA_OPERATOR_TOKEN/d' {KIT}/.env"
    )

    step("a project (the console's New project)")
    org = neura.get_current_principal()["organizations"][0]["id"]
    suffix = time.strftime("%m%d-%H%M") + "-" + secrets.token_hex(2)
    project = neura.create_project(
        org, body={"spec": {"name": f"staging-{suffix}", "display_name": f"Staging {suffix}"}}
    )["metadata"]["id"]
    results["project"] = project

    step("./site.sh publish on the site's server: the ML team's release")
    extra = ["--refill-at", str(args.refill_at)] if args.refill_at is not None else []
    site.kit("publish", f"{args.url}/o/{org}/p/{project}", *extra, stdin=token + "\n")
    contracts = {
        "robot_profile_id": neura.list_robot_profiles(project)["items"][0]["metadata"]["id"],
        "model_artifact_id": neura.list_model_artifacts(project)["items"][0]["metadata"]["id"],
        "execution_profile_id": neura.list_execution_profiles(project)["items"][0]["metadata"][
            "id"
        ],
    }

    step("a site, its robots and a server's enrollment (the console's Add a server)")
    site_id = neura.create_site(
        project,
        body={
            "spec": {
                "name": "staging",
                "display_name": "Staging",
                "offline_authorization_hours": 24,
            }
        },
    )["metadata"]["id"]
    robots = [
        neura.create_robot(
            project,
            body={
                "spec": {
                    "name": name,
                    "display_name": name.upper(),
                    "site_id": site_id,
                    "robot_profile_id": contracts["robot_profile_id"],
                }
            },
        )["metadata"]["id"]
        for name in names
    ]
    install = neura.create_enrollment_token(site_id, body={"ttl_minutes": 30})["install_command"]

    step("./site.sh connect: enrolled over the internet; the robot cell starts")
    site.kit("connect", install)
    wait_for(
        "Neura to see the site", lambda: neura.get_site(site_id)["status"].get("connected"), 180
    )
    wait_for(
        "the robots in the cell", lambda: len(site.cell()["robots"]) == len(names) or None, 180
    )

    step("a deployment and revision; prepare")
    deployment = neura.create_deployment(
        project,
        body={
            "spec": {
                "name": "hand-over",
                "display_name": "Hand over",
                "site_id": site_id,
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
    wait_for("prepare", lambda: operation_done(neura, prepare), 1200)
    capacity = neura.get_revision(revision)["status"].get("capacity")
    if capacity is None:
        raise SystemExit("the site reported no capacity for the prepared revision")
    results["capacity"] = capacity
    latency = capacity["assumptions"]["latency_p99_ms_by_batch"]
    print(f"    capacity: {capacity['max_robots']} robots; p99 ms by batch {latency}", flush=True)
    if capacity["max_robots"] < len(names):
        raise SystemExit(
            f"the site serves {capacity['max_robots']} robots at this profile, not {len(names)} "
            "(use --robots, --refill-at or a larger NEURA_SITE_MACHINE)"
        )

    step("activation, approved by the site operator")
    activation = neura.request_activation(deployment, body={"revision_id": revision})
    wait_for(
        "the approval to reach the site",
        lambda: "approving" in site.kit("approve") or None,
        180,
    )
    wait_for("the activation", lambda: operation_done(neura, activation["metadata"]["id"]), 300)
    wait_for(
        "every robot at work",
        lambda: all(r["state"] in ("Running", "Armed") for r in site.cell()["robots"]) or None,
        180,
    )

    uplink = site.uplink()
    neura_ip = args.neura_address
    phases = [
        Phase("baseline", "the site's internet connection as it is", args.baseline),
        Phase(
            "impaired",
            f"150 ms (+/- 30 ms) added latency and 2% loss on the uplink ({uplink})",
            args.impaired,
        ),
        Phase("partitioned", f"every packet to Neura ({neura_ip}) dropped", args.partition),
        Phase("recovered", "the connection restored", 90),
    ]
    impair = f"sudo tc qdisc replace dev {uplink} root netem delay 150ms 30ms loss 2%"
    unimpair = f"sudo tc qdisc del dev {uplink} root 2>/dev/null || true"
    # Docker's containers reach the internet through the FORWARD chain (DOCKER-USER first).
    drop = f"-d {neura_ip} -j DROP"
    cut = f"sudo iptables -I DOCKER-USER {drop} && sudo iptables -I OUTPUT {drop}"
    uncut = (
        f"sudo iptables -D DOCKER-USER {drop} 2>/dev/null; sudo iptables -D OUTPUT {drop} "
        "2>/dev/null; true"
    )
    try:
        for phase in phases:
            step(f"{phase.name}: {phase.description}, {phase.seconds:g} s")
            if phase.name == "impaired":
                site.run(impair)
            elif phase.name == "partitioned":
                site.run(unimpair)
                site.run(cut)
            elif phase.name == "recovered":
                site.run(uncut)
            observe(neura, site, site_id, phase)
            report = phase.report()
            print(
                f"    tasks {report['tasks_completed']}, successes {report['successes']}, "
                f"faults {report['faults'] or 'none'}, Neura saw the site connected in "
                f"{report['neura_saw_connected']}",
                flush=True,
            )
    finally:
        site.run(unimpair)
        site.run(uncut)
    results["phases"] = [p.report() for p in phases]

    step("what the site sent Neura, and its own view")
    results["outbound"] = site.run(f"sudo {KIT}/site.sh status | head -c 4000")
    for name in names:
        for camera in ("angle", "top"):
            frame = site.run(
                f"curl -fsS 'http://127.0.0.1:8095/robots/{name}/frame.jpg?camera={camera}' "
                "| base64 -w0"
            )
            (args.out / f"{name}-{camera}.jpg").write_bytes(base64.b64decode(frame))
    results["cell"] = site.cell()
    (args.out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")

    problems = []
    for phase in phases:
        if phase.faults:
            problems.append(f"{phase.name}: robots faulted: {phase.faults}")
        if phase.name != "partitioned" and not all(phase.tasks.values()):
            problems.append(f"{phase.name}: a robot completed no task: {phase.tasks}")
    partitioned = phases[2]
    if partitioned.report()["first_seen_offline_s"] is None:
        problems.append("Neura never saw the site offline while it was partitioned")
    if not all(partitioned.tasks.values()):
        problems.append(f"the robots stopped working while partitioned: {partitioned.tasks}")
    if not phases[3].samples or not phases[3].samples[-1]["neura_sees_connected"]:
        problems.append("the site did not reconnect after the partition")
    print(json.dumps({p.name: p.report() | {"samples": len(p.samples)} for p in phases}, indent=1))
    if problems:
        print("staging site test FAILED:\n- " + "\n- ".join(problems), flush=True)
        return 1
    print("staging site test passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
