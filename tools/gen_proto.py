"""Generate Python protobuf and gRPC bindings from proto/.

Usage:
    python tools/gen_proto.py           # regenerate packages/neura-proto/src/neura/v1
    python tools/gen_proto.py --check   # exit 1 if the generated files are stale
"""

from __future__ import annotations

import argparse
import filecmp
import sys
import tempfile
from pathlib import Path

from grpc_tools import protoc

ROOT = Path(__file__).resolve().parent.parent
PROTO_ROOT = ROOT / "proto"
OUT_ROOT = ROOT / "packages" / "neura-proto" / "src"
PACKAGE_DIR = Path("neura") / "v1"
GENERATED_SUFFIXES = ("_pb2.py", "_pb2.pyi", "_pb2_grpc.py")
HEADER = "# GENERATED FILE: do not edit. Source: proto/{source}. Regenerate with `make gen`.\n"


def generate(out_root: Path) -> None:
    sources = sorted(
        str(p.relative_to(PROTO_ROOT)) for p in (PROTO_ROOT / PACKAGE_DIR).glob("*.proto")
    )
    if not sources:
        raise SystemExit("no .proto files found")
    include = Path(protoc.__file__).parent / "_proto"
    args = [
        "grpc_tools.protoc",
        f"-I{PROTO_ROOT}",
        f"-I{include}",
        f"--python_out={out_root}",
        f"--pyi_out={out_root}",
        f"--grpc_python_out={out_root}",
        *sources,
    ]
    if protoc.main(args) != 0:
        raise SystemExit("protoc failed")
    for path in (out_root / PACKAGE_DIR).iterdir():
        if path.name.endswith(GENERATED_SUFFIXES):
            stem = path.name.split("_pb2")[0]
            content = path.read_text(encoding="utf-8")
            path.write_text(
                HEADER.format(source=f"{PACKAGE_DIR}/{stem}.proto") + content, encoding="utf-8"
            )


def generated_files(root: Path) -> set[str]:
    directory = root / PACKAGE_DIR
    if not directory.exists():
        return set()
    return {p.name for p in directory.iterdir() if p.name.endswith(GENERATED_SUFFIXES)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if output is stale")
    args = parser.parse_args()
    if not args.check:
        for name in generated_files(OUT_ROOT):
            (OUT_ROOT / PACKAGE_DIR / name).unlink()
        generate(OUT_ROOT)
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        (tmp_root / PACKAGE_DIR).mkdir(parents=True)
        generate(tmp_root)
        expected = generated_files(tmp_root)
        actual = generated_files(OUT_ROOT)
        stale = sorted(expected ^ actual)
        stale += sorted(
            name
            for name in expected & actual
            if not filecmp.cmp(
                tmp_root / PACKAGE_DIR / name, OUT_ROOT / PACKAGE_DIR / name, shallow=False
            )
        )
        if stale:
            print(
                f"generated protobuf files are stale ({', '.join(stale)}); run `make gen`",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
