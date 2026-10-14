"""``neura-agent`` command line.

    neura-agent [run]                       run the agent and its local site API (default)
    neura-agent enroll --control-plane HOST:PORT --token nen_...
    neura-agent preflight                   run preflight checks and print the results
    neura-agent status                      print the local status
    neura-agent approvals                   list activations awaiting local approval
    neura-agent approve COMMAND_ID --operator NAME
    neura-agent refuse COMMAND_ID --operator NAME [--note TEXT]
    neura-agent robot-token ROBOT_NAME      issue a device credential for a robot (shown once)

Local commands talk to the agent's local API and read the operator credential from
NEURA_AGENT_OPERATOR_TOKEN.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx
import uvicorn

from neura_agent.app import create_app
from neura_agent.runner import AgentRuntime, EnrollmentError
from neura_agent.settings import AgentSettings


def _local(settings: AgentSettings, method: str, path: str, body: Any = None) -> int:
    host = "127.0.0.1" if settings.local_host in ("0.0.0.0", "") else settings.local_host  # noqa: S104
    token = os.environ.get("NEURA_AGENT_OPERATOR_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.request(
        method, f"http://{host}:{settings.local_port}{path}", json=body, headers=headers, timeout=10
    )
    print(json.dumps(response.json(), indent=2))
    return 0 if response.status_code < 400 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-agent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run")
    enroll = sub.add_parser("enroll")
    enroll.add_argument("--control-plane", required=True, help="agent endpoint, host:port")
    enroll.add_argument("--token", required=True)
    sub.add_parser("preflight")
    sub.add_parser("status")
    sub.add_parser("approvals")
    for name in ("approve", "refuse"):
        cmd = sub.add_parser(name)
        cmd.add_argument("command_id")
        cmd.add_argument("--operator", required=True)
        cmd.add_argument("--note", default="")
    robot = sub.add_parser("robot-token")
    robot.add_argument("robot_name")
    args = parser.parse_args(argv)
    settings = AgentSettings()

    if args.command == "enroll":
        runtime = AgentRuntime(settings)
        try:
            identity = asyncio.run(runtime.enroll(args.control_plane, args.token))
        except EnrollmentError as exc:
            print(f"enrollment failed: {exc}", file=sys.stderr)
            return 1
        print(f"enrolled server {identity.server_id} at site {identity.site_id}")
        return 0
    if args.command == "preflight":
        facts = AgentRuntime(settings).facts()
        for check in facts.preflight:
            mark = "ok  " if check.passed else "FAIL"
            print(
                f"[{mark}] {check.name}: {check.detail}"
                + (f"\n       fix: {check.fix}" if check.fix else "")
            )
        return 0 if all(c.passed for c in facts.preflight) else 1
    if args.command == "status":
        return _local(settings, "GET", "/v1/local/status")
    if args.command == "approvals":
        return _local(settings, "GET", "/v1/local/approvals")
    if args.command in ("approve", "refuse"):
        return _local(
            settings,
            "POST",
            f"/v1/local/approvals/{args.command_id}/{args.command}",
            {"operator": args.operator, "note": args.note},
        )
    if args.command == "robot-token":
        return _local(settings, "POST", f"/v1/local/robots/{args.robot_name}/credentials")

    runtime = AgentRuntime(settings)
    uvicorn.run(
        create_app(settings, runtime=runtime, start_runtime=True),
        host=settings.local_host,
        port=settings.local_port,
        log_level=settings.log_level,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
