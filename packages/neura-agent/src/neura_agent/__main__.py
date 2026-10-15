"""``neura-agent`` command line.

    neura-agent [run]                       run the agent and its local site API (default)
    neura-agent enroll --control-plane HOST:PORT --token nen_...
    neura-agent preflight                   run preflight checks and print the results
    neura-agent status                      print the local status
    neura-agent approvals                   list activations awaiting local approval
    neura-agent approve COMMAND_ID --operator NAME
    neura-agent refuse COMMAND_ID --operator NAME [--note TEXT]
    neura-agent robot-certificate ROBOT_NAME --csr FILE [--out FILE]
                                            sign a robot's certificate request with the site CA
    neura-agent robot-revoke ROBOT_NAME     revoke a robot's certificates (its sessions end)
    neura-agent robot-token ROBOT_NAME      a device token, for a development gateway without TLS
    neura-agent import-weights DIR [--artifact FILE]
                                            add a checkpoint's files to the site's artifact store
    neura-agent trust-publisher KEY [--note TEXT]
                                            trust a publisher key (hex, or a PEM file) to sign
                                            the artifacts this server runs
    neura-agent distrust-publisher KEY_ID   stop trusting a publisher key
    neura-agent publishers                  list the trusted publisher keys

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

from neura_agent import trust
from neura_agent.app import create_app
from neura_agent.artifacts import ArtifactError, ArtifactStore
from neura_agent.runner import AgentRuntime, EnrollmentError
from neura_agent.settings import AgentSettings
from neura_contracts import ModelArtifact, load_document, signing


def _local(settings: AgentSettings, method: str, path: str, body: Any = None) -> int:
    host = "127.0.0.1" if settings.local_host in ("0.0.0.0", "") else settings.local_host  # noqa: S104
    token = os.environ.get("NEURA_AGENT_OPERATOR_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.request(
        method, f"http://{host}:{settings.local_port}{path}", json=body, headers=headers, timeout=10
    )
    print(json.dumps(response.json(), indent=2))
    return 0 if response.status_code < 400 else 1


def _robot_certificate(settings: AgentSettings, robot: str, csr: Path, out: Path | None) -> int:
    """Send the robot's CSR to the local API and write its certificate and the site CA."""
    host = "127.0.0.1" if settings.local_host in ("0.0.0.0", "") else settings.local_host  # noqa: S104
    token = os.environ.get("NEURA_AGENT_OPERATOR_TOKEN", "")
    response = httpx.post(
        f"http://{host}:{settings.local_port}/v1/local/robots/{robot}/certificate",
        json={"csr": csr.read_text(encoding="utf-8")},
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=10,
    )
    if response.status_code >= 400:
        print(json.dumps(response.json(), indent=2), file=sys.stderr)
        return 1
    body = response.json()
    target = out or csr.with_suffix(".pem")
    target.write_text(body["certificate"], encoding="utf-8")
    (target.parent / "ca.pem").write_text(body["ca"], encoding="utf-8")
    print(f"wrote {target} and {target.parent / 'ca.pem'}")
    return 0


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


def _publishers(settings: AgentSettings, args: argparse.Namespace) -> int:
    try:
        if args.command == "trust-publisher":
            text = Path(args.key).read_text() if Path(args.key).is_file() else args.key
            key_id, added = trust.trust(settings, text, args.note)
            print(f"{'trusting' if added else 'already trusted:'} {key_id}")
        elif args.command == "distrust-publisher":
            if not trust.distrust(settings, args.key_id):
                print(f"{args.key_id} is not in {trust.trust_file(settings)}", file=sys.stderr)
                return 1
            print(f"no longer trusting {args.key_id}")
        else:
            for key in trust.trusted_publishers(settings):
                print(signing.key_id(key), key.hex())
    except (OSError, signing.SignatureError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
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
    certificate = sub.add_parser("robot-certificate")
    certificate.add_argument("robot_name")
    certificate.add_argument("--csr", type=Path, required=True)
    certificate.add_argument("--out", type=Path, default=None, help="default: next to the CSR")
    revoke = sub.add_parser("robot-revoke")
    revoke.add_argument("robot_name")
    weights = sub.add_parser("import-weights")
    weights.add_argument("directory", type=Path)
    weights.add_argument(
        "--artifact", type=Path, default=None, help="check the files against this manifest"
    )
    publisher = sub.add_parser("trust-publisher")
    publisher.add_argument("key", help="public key: 64 hex characters, or a PEM file")
    publisher.add_argument("--note", default="", help="who or what the key is, for the file")
    distrust = sub.add_parser("distrust-publisher")
    distrust.add_argument("key_id")
    sub.add_parser("publishers")
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
    if args.command == "robot-certificate":
        return _robot_certificate(settings, args.robot_name, args.csr, args.out)
    if args.command == "robot-revoke":
        return _local(settings, "DELETE", f"/v1/local/robots/{args.robot_name}/certificate")
    if args.command == "import-weights":
        return _import_weights(settings, args.directory, args.artifact)
    if args.command in ("trust-publisher", "distrust-publisher", "publishers"):
        return _publishers(settings, args)

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
