"""``neura-contracts`` command line: validate documents, print digests, check compatibility, sign
artifacts and seal weights.

Exit codes: 0 success, 1 invalid, incompatible or unverified, 2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

from neura_contracts import sealing, signing
from neura_contracts.artifact import ModelArtifact
from neura_contracts.compatibility import check_compatibility
from neura_contracts.documents import ContractError, Document, load_document
from neura_contracts.execution import ExecutionProfile
from neura_contracts.robot import RobotProfile


class _UsageError(Exception):
    pass


def _load(path: str) -> Document:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise _UsageError(f"{path}: {exc.strerror}") from exc
    return load_document(text)


_Kind = TypeVar("_Kind", ModelArtifact, RobotProfile, ExecutionProfile)


def _load_kind(path: str, kind: type[_Kind]) -> _Kind:
    document = _load(path)
    if not isinstance(document, kind):
        raise _UsageError(f"{path}: expected kind {kind.__name__}, got {document.kind}")
    return document


def _print_contract_error(path: str, exc: ContractError) -> None:
    print(f"{path}: {exc.code}: {exc}", file=sys.stderr)
    for error in exc.errors:
        print(f"  - {error}", file=sys.stderr)


def _cmd_validate(args: argparse.Namespace) -> int:
    status = 0
    for path in args.files:
        try:
            document = _load(path)
        except ContractError as exc:
            _print_contract_error(path, exc)
            status = 1
            continue
        print(f"{path}: valid {document.kind} {document.digest()}")
    return status


def _cmd_digest(args: argparse.Namespace) -> int:
    try:
        document = _load(args.file)
    except ContractError as exc:
        _print_contract_error(args.file, exc)
        return 1
    print(document.digest())
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        artifact = _load_kind(args.artifact, ModelArtifact)
        robot = _load_kind(args.robot, RobotProfile)
        profile = _load_kind(args.profile, ExecutionProfile)
    except ContractError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    report = check_compatibility(artifact, robot, profile)
    if args.json:
        print(
            json.dumps(
                {
                    "compatible": report.compatible,
                    "artifact_digest": report.artifact_digest,
                    "robot_profile_digest": report.robot_profile_digest,
                    "execution_profile_digest": report.execution_profile_digest,
                    "issues": [
                        {"code": str(i.code), "path": i.path, "message": i.message}
                        for i in report.issues
                    ],
                },
                indent=2,
            )
        )
    elif report.compatible:
        print("compatible")
    else:
        print(f"incompatible: {len(report.issues)} issue(s)")
        for issue in report.issues:
            print(f"  {issue.code} at {issue.path}: {issue.message}")
    return 0 if report.compatible else 1


def _write_secret(path: Path, data: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise _UsageError(f"{path} exists; not overwriting a key") from exc
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _read(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise _UsageError(f"{path}: {exc.strerror}") from exc


def _cmd_publisher_key(args: argparse.Namespace) -> int:
    pem = signing.generate_key()
    _write_secret(Path(args.out), pem)
    public = signing.raw_public_key(signing.load_private_key(pem))
    print(f"wrote {args.out}; trust it on a site with its public key:")
    print(f"  key_id      {signing.key_id(public)}")
    print(f"  public_key  {public.hex()}")
    return 0


def _cmd_sign(args: argparse.Namespace) -> int:
    try:
        artifact = _load_kind(args.artifact, ModelArtifact)
    except ContractError as exc:
        _print_contract_error(args.artifact, exc)
        return 1
    try:
        key = signing.load_private_key(_read(args.key))
    except signing.SignatureError as exc:
        raise _UsageError(f"{args.key}: {exc}") from exc
    print(json.dumps(signing.sign(artifact.digest(), key), indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        artifact = _load_kind(args.artifact, ModelArtifact)
    except ContractError as exc:
        _print_contract_error(args.artifact, exc)
        return 1
    try:
        loaded = json.loads(_read(args.signature))
        signatures = loaded if isinstance(loaded, list) else [loaded]
        trusted = [signing.parse_public_key(k) for k in args.trusted_key]
        signer = signing.trusted_signer(artifact.digest(), signatures, trusted)
    except (ValueError, signing.SignatureError) as exc:
        print(f"not verified: {exc}", file=sys.stderr)
        return 1
    print(f"verified: {artifact.digest()} signed by {signer}")
    return 0


def _key_provider(args: argparse.Namespace) -> sealing.KeyProvider:
    try:
        if args.key_file:
            return sealing.FileKey(Path(args.key_file))
        if not args.key_id:
            raise _UsageError("--key-command needs --key-id")
        return sealing.CommandKey(args.key_id, shlex.split(args.key_command))
    except (OSError, sealing.KeyUnavailable) as exc:
        raise _UsageError(str(exc)) from exc


def _cmd_key_file(args: argparse.Namespace) -> int:
    try:
        key_id = sealing.generate_key_file(Path(args.out))
    except FileExistsError as exc:
        raise _UsageError(f"{args.out} exists; not overwriting a key") from exc
    print(f"wrote {args.out} ({key_id}); keep it on the server and in your key escrow only")
    return 0


def _cmd_seal(args: argparse.Namespace) -> int:
    key = _key_provider(args)
    directory = Path(args.directory)
    if not directory.is_dir():
        raise _UsageError(f"{directory} is not a directory")
    try:
        sealed = sealing.seal_directory(directory, Path(args.out), key)
    except sealing.KeyUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"# sealed with {key.key_id} into {args.out}/sha256/; the artifact's weights:")
    print("weights:")
    for path, digest, size in sealed:
        print(f"  - path: {path}\n    digest: {digest}\n    size_bytes: {size}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="neura-contracts", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate one or more documents")
    validate.add_argument("files", nargs="+")
    validate.set_defaults(func=_cmd_validate)

    digest = sub.add_parser("digest", help="print a document's content digest")
    digest.add_argument("file")
    digest.set_defaults(func=_cmd_digest)

    check = sub.add_parser("check", help="check artifact, robot and execution profile together")
    check.add_argument("--artifact", required=True)
    check.add_argument("--robot", required=True)
    check.add_argument("--profile", required=True)
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.set_defaults(func=_cmd_check)

    publisher = sub.add_parser("publisher-key", help="create a publisher signing key (PEM)")
    publisher.add_argument("--out", required=True, help="private key file to create (0600)")
    publisher.set_defaults(func=_cmd_publisher_key)

    sign = sub.add_parser("sign", help="sign a model artifact's digest; prints the signature")
    sign.add_argument("artifact")
    sign.add_argument("--key", required=True, help="publisher private key (PEM)")
    sign.set_defaults(func=_cmd_sign)

    verify = sub.add_parser("verify", help="check an artifact's signature against trusted keys")
    verify.add_argument("artifact")
    verify.add_argument("--signature", required=True, help="a signature or a list (JSON)")
    verify.add_argument(
        "--trusted-key", action="append", required=True, help="public key (hex or PEM file text)"
    )
    verify.set_defaults(func=_cmd_verify)

    key_file = sub.add_parser("key-file", help="create a 256-bit key-encryption key file")
    key_file.add_argument("--out", required=True)
    key_file.set_defaults(func=_cmd_key_file)

    seal = sub.add_parser(
        "seal", help="encrypt a checkpoint's files with a customer-managed key (design §18.2)"
    )
    seal.add_argument("directory")
    seal.add_argument("--out", required=True, help="writes <out>/sha256/<hex> per file")
    keys = seal.add_mutually_exclusive_group(required=True)
    keys.add_argument("--key-file", help="key-encryption key file")
    keys.add_argument("--key-command", help="KMS command: '<command> wrap|unwrap'")
    seal.add_argument("--key-id", help="the key's name, with --key-command")
    seal.set_defaults(func=_cmd_seal)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return result


if __name__ == "__main__":
    sys.exit(main())
