"""``neura-agent`` command line.

    neura-agent [run]                       run the agent and its local site API (default)
    neura-agent enroll --control-plane HOST:PORT --token nen_...
    neura-agent preflight                   run preflight checks and print the results
    neura-agent status                      print the local status
    neura-agent approvals                   list activations awaiting local approval
    neura-agent approve COMMAND_ID --operator NAME
    neura-agent refuse COMMAND_ID --operator NAME [--note TEXT]
    neura-agent robot-token ROBOT_NAME      issue a device credential for a robot (shown once)
    neura-agent import-weights DIR [--artifact FILE]
                                            add a checkpoint's files to the site's artifact store

Local commands talk to the agent's local API and read the operator credential from
NEURA_AGENT_OPERATOR_TOKEN.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from neura_agent.app import create_app
from neura_agent.artifacts import ArtifactError, ArtifactStore
from neura_agent.runner import AgentRuntime, EnrollmentError
from neura_agent.settings import AgentSettings
from neura_contracts import ModelArtifact, load_document


def _local(settings: AgentSettings, method: str, path: str, body: Any = None) -> int:
    host = "127.0.0.1" if settings.local_host in ("0.0.0.0", "") else settings.local_host  # noqa: S104
    token = os.environ.get("NEURA_AGENT_OPERATOR_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.request(
        method, f"http://{host}:{settings.local_port}{path}", json=body, headers=headers, timeout=10
    )
    print(json.dumps(response.json(), indent=2))
    return 0 if response.status_code < 400 else 1


def _import_weights(settings: AgentSettings, directory: Path, manifest: Path | None) -> int:
    """Hash and store every file of a checkpoint directory. With ``--artifact``, first check that
    the directory holds exactly the files the manifest declares, with their digests."""
    if not directory.is_dir():
        print(f"{directory} is not a directory", file=sys.stderr)
        return 1
    store = ArtifactStore(settings.state_dir / "artifacts")
    if manifest is not None:
        artifact = load_document(manifest.read_text(encoding="utf-8"))
        if not isinstance(artifact, ModelArtifact):
            print(f"{manifest} is not a ModelArtifact", file=sys.stderr)
            return 1
        try:
            for weight in artifact.spec.weights:
                store.add_file(directory / weight.path, expected=weight.digest)
        except (ArtifactError, OSError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"imported the {len(artifact.spec.weights)} files of {artifact.metadata.name}")
        return 0
    for item in store.import_directory(directory):
        print(json.dumps({"path": item.path, "digest": item.digest, "size_bytes": item.size_bytes}))
    return 0


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
    weights = sub.add_parser("import-weights")
    weights.add_argument("directory", type=Path)
    weights.add_argument(
        "--artifact", type=Path, default=None, help="check the files against this manifest"
    )
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
    if args.command == "import-weights":
        return _import_weights(settings, args.directory, args.artifact)

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
