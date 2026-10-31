"""Run the real backend for the console's end-to-end tests.

    uv run python console/e2e/stack.py --state /tmp/neura-e2e.json

Creates a fresh database on the PostgreSQL server in NEURA_E2E_DATABASE_URL (default: the test
server at 127.0.0.1:55432), migrates it, and serves the control plane with a development
bootstrap admin token. Writes the endpoints and token to --state, prints READY, and runs until
terminated (SIGTERM or Ctrl-C); then drops the database. The tests start the site agent and
robots themselves, with the commands the console shows.

Some things only a person may do (approve a CLI's sign-in): the control plane also trusts a test
SSO issuer whose key lives in this process, and --state holds a person's access token from it.
That person signs up on first use and gets a workspace of their own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import jwt
import psycopg
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa

from neura_control.db.migrate import upgrade
from neura_control.security.oidc import OidcVerifier
from neura_control.services.bootstrap import generate_token_value

ISSUER = "https://sso.e2e.invalid"
AUDIENCE = "neura"


def person_token(key: rsa.RSAPrivateKey) -> str:
    """An access token from the test issuer for a person, valid for the run."""
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "sub": "ada",
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=2),
        "email": "ada@e2e.example",
        "email_verified": True,
    }
    return jwt.encode(claims, key, algorithm="RS256")


def with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def exit_on_sigterm(*_: object) -> None:
    """SIGTERM ends this process the way Ctrl-C does, through its cleanup. uvicorn handles the
    signal, stops serving, then raises it again: by default that kills the process first."""
    raise SystemExit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--api-port", type=int, default=18080)
    parser.add_argument("--agent-port", type=int, default=18081)
    args = parser.parse_args()
    admin = os.environ.get(
        "NEURA_E2E_DATABASE_URL", "postgresql://neura:neura@127.0.0.1:55432/neura"
    )
    name = f"neura_e2e_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    database = with_database(admin, name)
    upgrade(database)
    token = generate_token_value()
    sso = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    env = {
        **os.environ,
        "NEURA_CONTROL_ENVIRONMENT": "development",
        "NEURA_CONTROL_DATABASE_URL": database,
        "NEURA_CONTROL_HOST": "127.0.0.1",
        "NEURA_CONTROL_PORT": str(args.api_port),
        "NEURA_CONTROL_DEV_BOOTSTRAP_TOKEN": token,
        "NEURA_CONTROL_AGENT_GRPC_LISTEN": f"127.0.0.1:{args.agent_port}",
        "NEURA_CONTROL_AGENT_ENDPOINT": f"127.0.0.1:{args.agent_port}",
        "NEURA_CONTROL_AGENT_POLL_INTERVAL_S": "0.3",
        "NEURA_CONTROL_LOG_LEVEL": "warning",
        "NEURA_CONTROL_SELF_SIGNUP": "true",
    }
    os.environ.update(env)
    args.state.write_text(
        json.dumps(
            {
                "api_url": f"http://127.0.0.1:{args.api_port}",
                "agent_endpoint": f"127.0.0.1:{args.agent_port}",
                "token": token,
                "person_token": person_token(sso),
                "database": name,
            }
        )
    )
    # Served in this process, so stopping it (SIGTERM, or Ctrl-C) leaves nothing behind.
    from neura_control.app import create_app
    from neura_control.settings import ControlPlaneSettings

    verifier = OidcVerifier(
        issuer=ISSUER, audience=AUDIENCE, key_resolver=lambda _: sso.public_key()
    )
    config = uvicorn.Config(
        create_app(ControlPlaneSettings(), oidc_verifier=verifier),
        host="127.0.0.1",
        port=args.api_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    async def serve() -> None:
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                await task  # raises: the port is taken, say
                raise SystemExit("the control plane stopped while starting")
            await asyncio.sleep(0.05)
        print("READY", flush=True)
        await task

    signal.signal(signal.SIGTERM, exit_on_sigterm)
    try:
        asyncio.run(serve())
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    return 0


if __name__ == "__main__":
    sys.exit(main())
