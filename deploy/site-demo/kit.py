"""The customer's side of the site kit (deploy/site-demo), run in the kit's image by site.sh.

    kit.py publish --url URL [--checkpoint REPO|DIR] [--task TASK]
        The ML team's release. Take a LeRobot ACT checkpoint (a published one from the Hugging
        Face Hub, or your own; migrated to LeRobot's processor format by LeRobot's own tool if
        it predates it), write its contracts (robot profile, model artifact, execution
        profile), sign the artifact with the publisher key (made on first use, kept in the
        kit) and register the three in the Neura project. URL is the project's console page
        (https://console.../o/.../p/...), or the API's base URL with --project. The API token
        comes from NEURA_TOKEN.
    kit.py publisher-key
        The publisher's public key (hex), for the site operator to trust.
    kit.py artifact
        The artifact's file, for the site to check the checkpoint against when importing it.

Everything is kept in --kit (default /kit): checkpoint/, contracts/, publisher.key and
published.json. Re-running publish registers nothing twice: Neura keys contracts by digest.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from neura_contracts import parse_document, signing
from neura_sdk import Client, NeuraApiError
from neura_sdk.models import ArtifactSignature

DEFAULT_CHECKPOINT = "lerobot/act_aloha_sim_transfer_cube_human"
TASKS = {"transfer_cube": "transfer-cube", "insertion": "insertion"}


def say(line: str) -> None:
    print(line, flush=True)


def api_and_project(url: str, project: str | None) -> tuple[str, str | None]:
    """The API base URL (the console serves the API under /v1) and the project, from a console
    page URL or a base URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise SystemExit(f"{url}: give the console's URL, https://console.<domain>/...")
    match = re.search(r"/p/(prj_[0-9A-Za-z]+)", parsed.path)
    return f"{parsed.scheme}://{parsed.netloc}", project or (match.group(1) if match else None)


def choose_project(neura: Client, project: str | None) -> str:
    if project is not None:
        neura.get_project(project)  # exists, and this token can read it
        return project
    found = [
        p
        for org in neura.get_current_principal()["organizations"]
        for p in neura.list_projects(org["id"])["items"]
    ]
    if len(found) == 1:
        return str(found[0]["metadata"]["id"])
    names = ", ".join(f"{p['spec']['name']} ({p['metadata']['id']})" for p in found) or "none"
    raise SystemExit(
        "which project? Give its console URL (https://console.<domain>/o/<org>/p/<project>) or "
        f"--project. Projects: {names}"
    )


def publisher_key(kit: Path) -> Any:
    path = kit / "publisher.key"
    if not path.exists():
        path.write_bytes(signing.generate_key())
        path.chmod(0o600)
        say(f"    made a publisher key: {path} (keep it: it signs your releases)")
    return signing.load_private_key(path.read_bytes())


def checkpoint(kit: Path, source: str) -> Path:
    """The checkpoint's files in <kit>/checkpoint, where the site imports them from: a Hub
    repository, or a local directory (your own training run)."""
    from neura_bench.evidence.policy import checkpoint_path

    target = kit / "checkpoint"
    marker = kit / "checkpoint.source"
    ready = (target / "policy_preprocessor.json").is_file()
    if ready and marker.is_file() and marker.read_text(encoding="utf-8") == source:
        say(f"    {source}: already in {target}")
        return target
    work = kit / "work"
    work.mkdir(parents=True, exist_ok=True)
    local = Path(source).expanduser()
    if local.is_dir():
        path, migrated = checkpoint_path(local, work)
    else:
        say(f"    downloading {source} from the Hugging Face Hub")
        path, migrated = checkpoint_path(f"hub:{source}", work)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(path, target, ignore=shutil.ignore_patterns(".cache", ".gitattributes"))
    shutil.rmtree(work, ignore_errors=True)
    marker.write_text(source, encoding="utf-8")
    if migrated:
        say("    migrated to LeRobot's processor format by LeRobot's own tool")
    return target


def publish(args: argparse.Namespace) -> int:
    from neura_robots.aloha.contracts import Chunking, write

    token = os.environ.get("NEURA_TOKEN", "")
    if not token:
        raise SystemExit("set NEURA_TOKEN: an API token from the console (Settings, API tokens)")
    base, project = api_and_project(args.url, args.project)
    neura = Client(base, token=token, max_retries=3)
    kit: Path = args.kit
    kit.mkdir(parents=True, exist_ok=True)
    project = choose_project(neura, project)
    say(f"--> publishing {args.checkpoint} to project {project} at {base}")

    path = checkpoint(kit, args.checkpoint)
    name = f"act-aloha-{TASKS[args.task]}"
    # On a CPU a forward pass of ACT takes 100-300 ms (on a GPU, a few ms) and a request can wait
    # behind another robot's: the site's admission refuses a robot a session when it cannot
    # serve it within these budgets (an observation's age when it reaches the model; the
    # actions left when the next chunk is requested, at 20 ms each, before the robot would
    # starve). The age of the actions a robot accepts stays bounded by 1.5 s either way.
    chunking = Chunking(
        refill_at=args.refill_at, max_age_at_dispatch_us=args.dispatch_freshness_ms * 1000
    )
    deployment = write(
        kit / "contracts",
        path,
        name=name,
        source=args.checkpoint,
        task=args.task,
        chunking=chunking,
    )
    documents = {
        kind: json.loads(p.read_text(encoding="utf-8"))
        for kind, p in (
            ("robot_profile", deployment.robot_profile),
            ("artifact", deployment.artifact),
            ("execution_profile", deployment.execution_profile),
        )
    }
    weights = sum(w["size_bytes"] for w in documents["artifact"]["spec"]["weights"])
    say(f"    contracts in {kit / 'contracts'} ({weights / 1e6:.0f} MB of weights, by digest)")

    key = publisher_key(kit)
    signed = signing.sign(parse_document(documents["artifact"]).digest(), key)
    signature = ArtifactSignature(
        key_id=signed["key_id"], public_key=signed["public_key"], signature=signed["signature"]
    )
    try:
        robot = neura.register_robot_profile(project, body={"document": documents["robot_profile"]})
        artifact = neura.register_model_artifact(
            project, body={"document": documents["artifact"], "signatures": [signature]}
        )
        profile = neura.register_execution_profile(
            project, body={"document": documents["execution_profile"]}
        )
    except NeuraApiError as exc:
        raise SystemExit(f"Neura refused the contracts: {exc}") from exc
    published = {
        "api": base,
        "project": project,
        "checkpoint": args.checkpoint,
        "task": args.task,
        "robot_profile_id": robot["metadata"]["id"],
        "model_artifact_id": artifact["metadata"]["id"],
        "execution_profile_id": profile["metadata"]["id"],
        "publisher_key_id": signed["key_id"],
        "publisher_public_key": signing.raw_public_key(key).hex(),
    }
    (kit / "published.json").write_text(json.dumps(published, indent=2), encoding="utf-8")
    for kind, label, note in (
        ("robot_profile", "robot profile", ""),
        ("artifact", "model artifact", f", signed by {signed['key_id']}"),
        ("execution_profile", "execution profile", ""),
    ):
        metadata = documents[kind]["metadata"]
        identifier = published[f"{'model_artifact' if kind == 'artifact' else kind}_id"]
        say(f"    {label:<18} {metadata['name']} {metadata['version']}  ({identifier}{note})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--kit", type=Path, default=Path(os.environ.get("NEURA_KIT", "/kit")))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("publish", help="register a published checkpoint's contracts")
    p.add_argument("--url", required=True, help="the project's console URL, or the API's")
    p.add_argument("--project", default=None)
    p.add_argument(
        "--checkpoint",
        default=DEFAULT_CHECKPOINT,
        help="a LeRobot ACT checkpoint trained on the ALOHA scene: a Hub repository, or a "
        f"local directory (default {DEFAULT_CHECKPOINT})",
    )
    p.add_argument("--task", choices=sorted(TASKS), default="transfer_cube")
    p.add_argument(
        "--dispatch-freshness-ms",
        type=int,
        default=600,
        help="the oldest an observation may be when it reaches the model (default 600, for a "
        "site serving on CPUs; 100 on a GPU)",
    )
    p.add_argument(
        "--refill-at",
        type=int,
        default=50,
        help="actions left of a chunk when the next is requested (default 50, a second's "
        "budget, for CPUs; 30 on a GPU)",
    )
    sub.add_parser("publisher-key", help="the publisher's public key (hex)")
    sub.add_parser("artifact", help="the artifact file's path")
    args = parser.parse_args(argv)
    if args.command == "publish":
        return publish(args)
    published = args.kit / "published.json"
    if not published.is_file():
        raise SystemExit("nothing published yet: run publish first")
    if args.command == "publisher-key":
        print(json.loads(published.read_text(encoding="utf-8"))["publisher_public_key"])
    else:
        print(args.kit / "contracts" / "artifact.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
