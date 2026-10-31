"""``neura-robot``: robot-side tools.

neura-robot keygen ROBOT_NAME [--dir DIR]
    make the robot's TLS key and a certificate signing request. The key stays on the robot;
    the site operator signs the request with `neura-agent robot-certificate`, which returns
    the robot's certificate and the site CA. The robot then connects to the gateway with
    `<name>.key`, `<name>.pem` and `ca.pem`.
neura-robot serve POLICY [--weights DIR] [--device DEVICE] [--listen HOST:PORT]
    serve an @neura.policy class (POLICY: path/to/file.py, or file.py:Class) to robots over the
    network, on a GPU machine for example. Beyond loopback it needs a token, read from the
    environment variable NEURA_SERVE_TOKEN; robots send the same token.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path

from neura.v1 import worker_pb2
from neura_contracts import pki
from neura_runtime import remote
from neura_runtime.authored import AuthoredPolicy


def keygen(robot: str, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    key_path, csr_path = directory / f"{robot}.key", directory / f"{robot}.csr"
    if key_path.exists():
        raise SystemExit(f"{key_path} exists: remove it to make a new key")
    key = pki.new_key()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pki.key_pem(key))
    csr_path.write_bytes(pki.signing_request(key, robot))
    return key_path, csr_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-robot",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("keygen")
    generate.add_argument("robot_name")
    generate.add_argument("--dir", type=Path, default=Path("."))
    served = sub.add_parser("serve", help="serve an @neura.policy class to robots")
    served.add_argument("policy")
    served.add_argument("--weights", type=Path)
    served.add_argument("--device", default="cpu")
    served.add_argument("--listen", default="127.0.0.1:50061")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return asyncio.run(serve(args.policy, args.weights, args.device, args.listen))
    key_path, csr_path = keygen(args.robot_name, args.dir)
    print(f"key {key_path} (keep it on this robot), request {csr_path} (give it to the operator)")
    return 0


async def serve(source: str, weights: Path | None, device: str, listen: str) -> int:
    policy = AuthoredPolicy.load(source, weights=weights, device=device)
    declaration = policy.declaration
    description = worker_pb2.WorkerDescription(
        adapter="python",
        device=device,
        deterministic=declaration.deterministic,
        chunk_length=declaration.chunk_length,
        pid=os.getpid(),
    )
    token = os.environ.get("NEURA_SERVE_TOKEN", "")
    try:
        server, port = await remote.serve(policy, listen, description=description, token=token)
    except ValueError as error:
        raise SystemExit(f"cannot serve: {error} (set NEURA_SERVE_TOKEN)") from error
    host = listen.rsplit(":", 1)[0]
    print(f"serving {source} on {host}:{port}{' with a token' if token else ''}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await server.stop(grace=1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
