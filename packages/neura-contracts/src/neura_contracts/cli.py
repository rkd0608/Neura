"""``neura-contracts`` command line: validate documents, print digests, check compatibility.

Exit codes: 0 success, 1 invalid or incompatible, 2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

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


def _load_kind[T: (ModelArtifact, RobotProfile, ExecutionProfile)](path: str, kind: type[T]) -> T:
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
