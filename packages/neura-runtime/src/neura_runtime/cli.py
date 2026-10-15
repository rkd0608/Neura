"""``neura-robot``: robot-side tools.

neura-robot keygen ROBOT_NAME [--dir DIR]
    make the robot's TLS key and a certificate signing request. The key stays on the robot;
    the site operator signs the request with `neura-agent robot-certificate`, which returns
    the robot's certificate and the site CA. The robot then connects to the gateway with
    `<name>.key`, `<name>.pem` and `ca.pem`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from neura_contracts import pki


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
    args = parser.parse_args(argv)
    key_path, csr_path = keygen(args.robot_name, args.dir)
    print(f"key {key_path} (keep it on this robot), request {csr_path} (give it to the operator)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
