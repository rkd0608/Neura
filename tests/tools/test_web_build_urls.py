"""The web images link where they are deployed (design §20.10): every build-time address the
console, the NucleiLabs site and the docs read reaches them in the images Neura ships.

An address an app reads but no build passes leaves it on its development default (127.0.0.1, or
a retired site), and nothing fails until someone follows the link. So each variable an app reads
is declared in its Dockerfile build stage and passed by both builds for deployment
(deploy/gcp/deploy.sh and the release workflow), with a value that works from any host."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
VITE = re.compile(r"import\.meta\.env\.(VITE_\w+)")
NODE = re.compile(r"process\.env\.(NEURA_\w+)")
# Each web image, from its Dockerfile build stages and the sources each stage builds.
IMAGES = {
    "neura-console": [("console-build", "console/src", VITE)],
    "neura-landing": [
        ("landing-build", "landing/src", VITE),
        ("docs-build", "site/docusaurus.config.ts", NODE),
    ],
}
# Development addresses, and a retired site: never in an image for deployment.
DEVELOPMENT = re.compile(r"127\.0\.0\.1|localhost|github\.io")


def read_by(source: str, pattern: re.Pattern[str]) -> set[str]:
    path = ROOT / source
    files = [path] if path.is_file() else sorted(path.rglob("*.ts*"))
    return {m for f in files if ".test." not in f.name for m in pattern.findall(f.read_text())}


def declared(stage: str) -> set[str]:
    """The ARGs of a Dockerfile build stage."""
    names: set[str] = set()
    current = None
    for line in (ROOT / "deploy/docker/Dockerfile").read_text().splitlines():
        if line.startswith("FROM "):
            current = line.split()[-1]
        elif current == stage and line.startswith("ARG "):
            names |= {arg.split("=")[0] for arg in line.split()[1:]}
    return names


def deploy_script_args() -> dict[str, str]:
    script = (ROOT / "deploy/gcp/deploy.sh").read_text()
    return dict(re.findall(r'--build-arg "(\w+)=([^"]*)"', script))


def release_args() -> tuple[set[str], dict[str, str]]:
    """The images the release workflow builds, and the build args it gives them."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    job = workflow["jobs"]["images"]
    build = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("docker/build-push"))
    lines = str(build["with"].get("build-args", "")).splitlines()
    args = dict(line.strip().split("=", 1) for line in lines if line.strip())
    return set(job["strategy"]["matrix"]["target"]), args


def test_each_build_stage_declares_what_its_app_reads() -> None:
    for stages in IMAGES.values():
        for stage, source, pattern in stages:
            missing = read_by(source, pattern) - declared(stage)
            assert not missing, f"{source} reads {sorted(missing)}: declare ARG in {stage}"


def test_every_build_for_deployment_passes_the_addresses_with_values_that_work() -> None:
    released, release = release_args()
    builds = {"deploy/gcp/deploy.sh": deploy_script_args()}
    for image, stages in IMAGES.items():
        reads = set().union(*(read_by(source, pattern) for _, source, pattern in stages))
        assert reads, f"{image}: found no build-time variables (has the pattern changed?)"
        if image in released:
            builds[f"release.yml ({image})"] = release
        for build, args in builds.items():
            missing = reads - set(args)
            assert not missing, f"{build} builds {image} without {sorted(missing)}"
    for build, args in builds.items():
        for name, value in args.items():
            assert not DEVELOPMENT.search(value), f"{build}: {name}={value} is for development"
            # The console and the site are on different hosts: a path would point at the wrong one.
            assert not value.startswith("/"), f"{build}: {name}={value} is a path, not an address"
