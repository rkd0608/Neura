"""`neura login`, `neura logout`, `neura whoami`."""

from __future__ import annotations

import argparse
import getpass
import socket
import sys
import time
import webbrowser
from datetime import datetime

from neura_cli.session import CliError, Context, public_client
from neura_sdk import Client, NeuraApiError


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    login = sub.add_parser(
        "login",
        help="sign in through the browser",
        description="Sign in through the browser: open the link, check the code, approve. "
        "In CI, pipe a token instead: echo $NEURA_TOKEN | neura login --with-token",
    )
    login.add_argument("--api", help="Neura's address (default: the last one used)")
    login.add_argument("--with-token", action="store_true", help="read an API token from stdin")
    login.add_argument("--workspace", help="the workspace (organization id) to use")
    login.add_argument("--no-browser", action="store_true", help="only print the link")
    login.set_defaults(run=run_login)
    sub.add_parser("logout", help="forget the token on this machine").set_defaults(run=run_logout)
    sub.add_parser("whoami", help="who you are signed in as, and where").set_defaults(
        run=run_whoami
    )


def _client_name() -> str:
    try:
        user = getpass.getuser()
    except OSError:
        user = "someone"
    return f"neura CLI on {user}@{socket.gethostname()}"


def run_login(args: argparse.Namespace, ctx: Context) -> int:
    config, out = ctx.config, ctx.out
    if args.api:
        config.api_url = args.api.rstrip("/")
    if args.with_token:
        token = sys.stdin.readline().strip()
        if not token:
            raise CliError("No token on stdin.", "echo $NEURA_TOKEN | neura login --with-token")
    else:
        token = _browser_login(ctx, open_browser=not args.no_browser)
    config.token = token
    config.organization_id = args.workspace
    config.project_id = None
    try:
        session = ctx.session()
    except NeuraApiError as error:
        if error.status == 401:
            raise CliError("Neura did not accept that token.") from error
        raise
    config.save()
    workspace = session.client.get_workspace(organization_id=session.organization_id)
    out.data({"workspace": workspace, "api_url": config.api_url})
    out.line()
    out.ok(f"Signed in to {workspace['display_name']}", ", ".join(workspace["permissions"]))
    out.hint(f"Neura at {config.api_url}")
    out.line()
    return 0


def _browser_login(ctx: Context, *, open_browser: bool) -> str:
    with public_client(ctx.config) as client:
        return _wait_for_approval(ctx, client, open_browser=open_browser)


def _wait_for_approval(ctx: Context, client: Client, *, open_browser: bool) -> str:
    out = ctx.out
    started = client.create_login_session(body={"client_name": _client_name()})
    url = started["verification_url"]
    out.header("Sign in to Neura")
    out.line(f"  Open    {url}")
    out.line(f"  Check   the page shows {out.s.bold(started['user_code'])}, then approve")
    out.line()
    if open_browser and sys.stdout.isatty():
        webbrowser.open(url)
    deadline = datetime.fromisoformat(started["expires_at"].replace("Z", "+00:00")).timestamp()
    while time.time() < deadline:
        time.sleep(max(1, started["interval_s"]))
        try:
            granted = client.exchange_login_session(
                started["id"], body={"secret": started["secret"]}
            )
        except NeuraApiError as error:
            if error.reason == "API_LOGIN_PENDING":
                continue
            raise CliError(
                "The sign-in expired before it was approved.", "Run: neura login"
            ) from error
        ctx.config.organization_id = granted["organization_id"]
        return granted["token"]
    raise CliError("The sign-in expired before it was approved.", "Run: neura login")


def run_logout(args: argparse.Namespace, ctx: Context) -> int:
    ctx.config.token = None
    ctx.config.organization_id = ctx.config.project_id = None
    ctx.config.save()
    ctx.out.ok("Signed out", "the token stays valid until it expires or is revoked")
    return 0


def run_whoami(args: argparse.Namespace, ctx: Context) -> int:
    session = ctx.session()
    me = session.client.get_current_principal()
    workspace = session.client.get_workspace(organization_id=session.organization_id)
    ctx.out.data({"principal": me, "workspace": workspace, "api_url": ctx.config.api_url})
    ctx.out.line()
    ctx.out.line(f"  {ctx.out.s.bold(me['subject'])}")
    ctx.out.line(f"  {workspace['display_name']}  ·  {', '.join(workspace['permissions'])}")
    ctx.out.hint(ctx.config.api_url)
    ctx.out.line()
    return 0
