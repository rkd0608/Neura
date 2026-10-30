"""The console's nginx in front of the API (deploy/console, design §19.6, §20.1): on the cloud
deployment the console's address is the API's public address too (`NEURA_CONTROL_PUBLIC_URL`), so
every route the API serves must reach it through the console, the one-line installer included.

Runs the console's nginx (the image deploy/docker/Dockerfile builds it from) with the templates in
this tree and a stand-in API that names the path it was asked for, then asks for every route the
control plane serves. Needs Docker.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from neura_control.app import create_app, iter_routes
from neura_control.settings import ControlPlaneSettings

TEMPLATES = Path(__file__).resolve().parents[2] / "deploy" / "console"
# Answered by the API only on its own network: the container's probes, and its bare root.
INTERNAL = {"/", "/healthz", "/readyz"}
# The stand-in names the path, and the headers the console forwarded.
STAND_IN = (
    "server { listen 9000; location / { return 200"
    ' "api $request_uri host=$http_host for=$http_x_forwarded_for'
    ' proto=$http_x_forwarded_proto\\n"; } }\n'
)
FORWARDED = "host=127.0.0.1 for=127.0.0.1 proto=http"


def answers(image: str, paths: list[str], tmp_path: Path) -> dict[str, str]:
    """What the console answers for each path: the first line of the body."""
    (tmp_path / "stand-in-api.conf").write_text(STAND_IN, encoding="utf-8")
    fetch = " ".join(
        f"printf '%s\\t' {q}; wget -qO- http://127.0.0.1:8080{q} | head -n 1;"
        for q in map(shlex.quote, paths)
    )
    mounts = [
        f"{TEMPLATES / name}:/etc/nginx/templates/{name}:ro"
        for name in ("default.conf.template", "security-headers.inc.template")
    ] + [f"{tmp_path / 'stand-in-api.conf'}:/etc/nginx/conf.d/stand-in-api.conf:ro"]
    done = subprocess.run(
        ["docker", "run", "--rm", "--network", "none"]
        + [arg for mount in mounts for arg in ("-v", mount)]
        + ["-e", "NEURA_API_UPSTREAM=http://127.0.0.1:9000", "-e", "NEURA_CSP_CONNECT_EXTRA="]
        # nginx listens once it returns (it then runs in the background), so no waiting.
        + [image, "sh", "-c", f"/docker-entrypoint.sh nginx >/dev/null && {fetch}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr
    return dict(line.split("\t", 1) for line in done.stdout.splitlines())


def test_every_route_the_api_serves_publicly_reaches_it_through_the_console(
    tmp_path: Path, base_image: Callable[[str], str]
) -> None:
    image = base_image("neura-console")
    app = create_app(ControlPlaneSettings(environment="test"))
    routes = sorted({re.sub(r"\{[^}]+\}", "x", path) for _, path in iter_routes(app)})
    public = [path for path in routes if path not in INTERNAL]
    assert "/install.sh" in public
    got = answers(image, [*public, "/healthz", "/sites", "/sites/x"], tmp_path)
    assert {p: got[p] for p in public} == {p: f"api {p} {FORWARDED}" for p in public}
    # The console's own pages and probe stay the console's.
    assert got["/healthz"] == "ok"
    assert not got["/sites"].startswith("api ")
    assert not got["/sites/x"].startswith("api ")
