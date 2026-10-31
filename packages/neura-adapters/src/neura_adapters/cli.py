"""``neura-adapters`` command line.

neura-adapters list                       adapters this installation can load
neura-adapters weights DIR                the artifact's spec.weights for a checkpoint directory
neura-adapters synthetic-act ARTIFACT OUT [--size tiny|small|base] [--seed N]
                                          write an ACT checkpoint with random weights
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from neura_adapters.base import available_adapters
from neura_contracts import ModelArtifact, load_document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-adapters",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    weights = sub.add_parser("weights")
    weights.add_argument("directory", type=Path)
    synthetic = sub.add_parser("synthetic-act")
    synthetic.add_argument("artifact", type=Path)
    synthetic.add_argument("out", type=Path)
    synthetic.add_argument("--size", choices=["tiny", "small", "base"], default="small")
    synthetic.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    if args.command == "list":
        print("\n".join(available_adapters()))
        return 0
    from neura_adapters.synthetic import write_act_checkpoint
    from neura_contracts.inference import weights_manifest

    if args.command == "weights":
        print(yaml.safe_dump({"weights": weights_manifest(args.directory)}, sort_keys=False))
        return 0
    artifact = load_document(args.artifact.read_text(encoding="utf-8"))
    if not isinstance(artifact, ModelArtifact):
        print(f"{args.artifact} is not a ModelArtifact", file=sys.stderr)
        return 1
    write_act_checkpoint(artifact, args.out, size=args.size, seed=args.seed)
    print(yaml.safe_dump({"weights": weights_manifest(args.out)}, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
