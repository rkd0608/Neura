"""Build and sign a site-agent release bundle (design §17.3).

    uv run python tools/build_agent_release.py keygen --out release   # .pem and .pub
    uv run python tools/build_agent_release.py build --key release.pem --out dist/

`build` makes the workspace's wheels (uv build), downloads the agent's dependencies as wheels
(pip download; or takes every wheel from --wheels-from, for offline builds), adds the launcher
and the installer, writes release.json with every file's SHA-256, signs its exact bytes with the
Ed25519 release key, and packs neura-agent-<version>.tar. It prints the bundle's SHA-256, which
an upgrade command and the one-line installer carry. Sites verify the signature against the
release keys the installer pinned.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "packages" / "neura-agent" / "src" / "neura_agent" / "launch.py"
INSTALLER = ROOT / "deploy" / "install" / "install.sh"


def keygen(out: Path) -> None:
    key = Ed25519PrivateKey.generate()
    private = out.with_suffix(".pem")
    public = out.with_suffix(".pub")
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private.chmod(0o600)
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    print(f"wrote {private} (keep it offline) and {public} (pin it on sites)")


def agent_version() -> str:
    data = tomllib.loads((ROOT / "packages" / "neura-agent" / "pyproject.toml").read_text())
    return str(data["project"]["version"])


def collect_wheels(directory: Path) -> None:
    subprocess.run(
        ["uv", "build", "--all-packages", "--wheel", "--out-dir", str(directory)],
        cwd=ROOT,
        check=True,
    )
    requirements = directory.parent / "requirements.txt"
    exported = subprocess.run(
        [
            "uv",
            "export",
            "--package",
            "neura-agent",
            "--no-dev",
            "--no-hashes",
            "--no-emit-workspace",
            "--format",
            "requirements-txt",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    requirements.write_text(exported)
    subprocess.run(
        [
            "uv",
            "run",
            "--with",
            "pip",
            "python",
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(directory),
            "--requirement",
            str(requirements),
        ],
        cwd=ROOT,
        check=True,
    )


def build(key_path: Path, out: Path, version: str, wheels_from: Path | None) -> Path:
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("the release key must be Ed25519")
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / f"neura-agent-{version}"
        wheels = bundle / "wheels"
        wheels.mkdir(parents=True)
        if wheels_from is not None:
            for wheel in sorted(wheels_from.glob("*.whl")):
                shutil.copy2(wheel, wheels / wheel.name)
        else:
            collect_wheels(wheels)
        (bundle / "bin").mkdir()
        launcher = bundle / "bin" / "neura-agent-launch"
        launcher.write_text("#!/usr/bin/env python3\n" + LAUNCHER.read_text())
        launcher.chmod(0o755)
        # The release carries its installer, signed with it: the one-line installer runs it.
        shutil.copy2(INSTALLER, bundle / "bin" / "install.sh")
        files = {
            p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(bundle.rglob("*"))
            if p.is_file()
        }
        manifest = {
            "type": "neura-agent-release",
            "name": "neura-agent",
            "version": version,
            "python": ">=3.13",
            "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "files": files,
        }
        data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        (bundle / "release.json").write_bytes(data)
        (bundle / "release.sig").write_bytes(key.sign(data))
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"neura-agent-{version}.tar"
        with tarfile.open(target, "w") as tar:
            tar.add(bundle, arcname=bundle.name)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"{target}\nsha256 {digest}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("keygen", help="create a release key pair")
    generate.add_argument("--out", type=Path, required=True, help="path without extension")
    make = sub.add_parser("build", help="build and sign a release bundle")
    make.add_argument("--key", type=Path, required=True)
    make.add_argument("--out", type=Path, required=True)
    make.add_argument("--version", default=None, help="default: neura-agent's version")
    make.add_argument("--wheels-from", type=Path, default=None, help="use these wheels")
    args = parser.parse_args()
    if args.command == "keygen":
        keygen(args.out)
    else:
        build(args.key, args.out, args.version or agent_version(), args.wheels_from)
    return 0


if __name__ == "__main__":
    sys.exit(main())
