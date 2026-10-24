"""`neura approve` (at the site) and `neura doctor` (on any machine)."""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys
from typing import Any

import httpx

from neura_cli import keys
from neura_cli.local_site import LocalSite, agent_installed
from neura_cli.session import CliError, Context, Session, public_client
from neura_sdk import NeuraApiError


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    approve = sub.add_parser(
        "approve",
        help="approve, at the site, what is waiting to switch",
        description="Approve an activation waiting at a site, through its agent on this "
        "machine. Only someone at the site can: no cloud credential approves anything. On a "
        "server running the agent as a service, set NEURA_AGENT_OPERATOR_TOKEN.",
    )
    approve.add_argument("which", nargs="?", help="a rollout (ro_...) or command id")
    approve.add_argument("--site", help="this machine's site, when it is a server of several")
    approve.add_argument("--agent", help="the agent's local API (default: this machine's)")
    approve.add_argument("--as", dest="operator", help="your name, for the site's audit log")
    approve.add_argument("--refuse", action="store_true", help="refuse it instead")
    approve.add_argument("--note", default="", help="why, with --refuse")
    approve.add_argument("-y", "--yes", action="store_true", help="do not ask to confirm")
    approve.set_defaults(run=run_approve)

    sub.add_parser("doctor", help="check this machine's setup").set_defaults(run=run_doctor)


class _Agent:
    """The local API of the agent of a site this machine is a server of."""

    def __init__(self, ctx: Context, args: argparse.Namespace) -> None:
        if args.agent:
            token = os.environ.get("NEURA_AGENT_OPERATOR_TOKEN")
            if not token:
                raise CliError("Set NEURA_AGENT_OPERATOR_TOKEN to the site's operator credential.")
            self.name, self.url, self.token = args.agent, args.agent.rstrip("/"), token
            return
        sites = ctx.config.local_sites
        if args.site:
            if args.site not in sites:
                raise CliError(f"This machine is not a server of {args.site}.")
            name = args.site
        elif len(sites) == 1:
            name = next(iter(sites))
        elif not sites:
            raise CliError(
                "This machine is not a server of any site.",
                "Run this on the site's server, or name its agent: --agent http://HOST:8090",
            )
        else:
            raise CliError("Which site?", "This machine serves " + ", ".join(sorted(sites)) + ".")
        local = LocalSite.at(name, sites[name])
        self.name, self.url, self.token = name, local.local_url(), local.operator_token()

    def call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            response = httpx.request(
                method,
                self.url + path,
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
        except httpx.HTTPError as error:
            raise CliError(f"The agent of {self.name} is not answering at {self.url}.") from error
        if response.status_code >= 400:
            detail = response.json().get("detail") if response.content else ""
            raise CliError(f"The agent refused: {detail or response.status_code}.")
        return response.json()


def _operator(args: argparse.Namespace) -> str:
    if args.operator:
        return str(args.operator)
    try:
        user = getpass.getuser()
    except OSError:
        user = "operator"
    return f"{user}@{socket.gethostname()}"


def _rollouts_waiting(ctx: Context) -> dict[str, dict[str, Any]]:
    """Rollouts awaiting approval, by the revision they activate (when signed in)."""
    if not ctx.config.token:
        return {}
    try:
        session = ctx.session()
        page = session.client.list_rollouts(session.project_id, state="awaiting_approval")
    except (CliError, NeuraApiError, httpx.HTTPError):
        return {}
    return {
        revision: dict(r) for r in page["items"] if (revision := r["status"].get("revision_id"))
    }


def run_approve(args: argparse.Namespace, ctx: Context) -> int:
    out = ctx.out
    agent = _Agent(ctx, args)
    waiting = agent.call("GET", "/v1/local/approvals")["items"]
    pending = [a for a in waiting if not a.get("approved_by")]
    rollouts = _rollouts_waiting(ctx)
    if args.which:
        wanted = args.which
        if wanted.startswith("ro_"):
            revision = next(
                (rev for rev, r in rollouts.items() if r["metadata"]["id"] == wanted), None
            )
            pending = [a for a in pending if a["revision_id"] == revision]
        else:
            pending = [a for a in pending if a["command_id"].startswith(wanted)]
    out.data(pending)
    if not pending:
        out.line()
        out.line(f"  Nothing is waiting for approval at {agent.name}.")
        out.line()
        return 0
    if len(pending) > 1:
        out.line()
        out.line("  Several are waiting; name one:")
        for item in pending:
            name = (
                rollouts.get(item["revision_id"], {})
                .get("status", {})
                .get("name", item["revision_id"])
            )
            robots = ", ".join(item["robot_names"])
            out.line(f"    neura approve {item['command_id']}   {name} on {robots}")
        out.line()
        return 1
    item = pending[0]
    rollout = rollouts.get(item["revision_id"])
    what = rollout["status"]["name"] if rollout else f"revision {item['revision_id']}"
    robots = ", ".join(item["robot_names"])
    verb = "Refuse" if args.refuse else "Approve"
    out.line()
    out.line(f"  {verb} {out.s.bold(what)} on {robots}?")
    if rollout and rollout["spec"].get("note"):
        out.hint(rollout["spec"]["note"])
    if not args.yes:
        if not sys.stdin.isatty():
            raise CliError("Confirm with --yes when not at a terminal.")
        if input("  [y/N] ").strip().lower() not in ("y", "yes"):
            out.line("  Nothing changed.")
            return 1
    operator = _operator(args)
    action = "refuse" if args.refuse else "approve"
    agent.call(
        "POST",
        f"/v1/local/approvals/{item['command_id']}/{action}",
        {"operator": operator, "note": args.note},
    )
    out.line()
    if args.refuse:
        out.ok("Refused", f"by {operator}; the robots keep what they run")
    else:
        out.ok("Approved", f"by {operator}; the robots switch between tasks")
    out.line()
    return 0


# --- doctor ------------------------------------------------------------------------------------


def run_doctor(args: argparse.Namespace, ctx: Context) -> int:
    out, config = ctx.out, ctx.config
    results: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str, fix: str = "") -> None:
        results.append({"check": name, "passed": passed, "detail": detail, "fix": fix})
        if passed:
            out.ok(name, detail)
        else:
            out.fail(f"{name}: {detail}", *([fix] if fix else []))

    out.header("Checking this machine")
    session: Session | None = None
    try:
        with public_client(config) as client:
            meta = client.get_meta()
        check("Neura reachable", True, f"{config.api_url} (API {meta.get('api_version', '?')})")
    except httpx.HTTPError as error:
        check("Neura reachable", False, f"{config.api_url}: {error}", "neura login --api URL")
    except NeuraApiError as error:
        check("Neura reachable", error.status < 500, f"{config.api_url} answered {error.status}")
    if config.token:
        try:
            session = ctx.session()
            workspace = session.client.get_workspace(organization_id=session.organization_id)
            check(
                "Signed in",
                True,
                f"{workspace['display_name']} ({', '.join(workspace['permissions'])})",
            )
        except (NeuraApiError, httpx.HTTPError, CliError) as error:
            check("Signed in", False, str(error), "neura login")
    else:
        check("Signed in", False, "no token on this machine", "neura login")
    key = keys.load(config, create=False)
    if key is None:
        check("Publisher key", True, "none yet; made on the first push")
    elif session is not None:
        public = keys.public_hex(key)
        registered = any(
            k["spec"]["public_key"] == public
            for k in session.client.paginate(
                session.client.list_publisher_keys, session.organization_id
            )
        )
        check(
            "Publisher key",
            True,
            f"{keys.path(config)}" + ("" if registered else " (registered on the next push)"),
        )
    for name, directory in sorted(config.local_sites.items()):
        local = LocalSite.at(name, directory)
        if not local.exists():
            check(f"Site {name}", False, f"{directory} is gone", f"neura site join {name}")
            continue
        status = local.status()
        if status is None:
            check(f"Site {name}", False, "its agent is not running", f"neura site start {name}")
        elif not status.get("connection", {}).get("connected"):
            problem = status.get("connection", {}).get("last_error") or "not connected"
            check(f"Site {name}", False, f"agent running, {problem}", f"see {local.log_file}")
        else:
            robots = status.get("site", {}).get("robots", {})
            up = sum(1 for r in robots.values() if r.get("running"))
            check(f"Site {name}", True, f"agent connected, running {up} of {len(robots)} robots")
    if not config.local_sites:
        check(
            "Site agent",
            True,
            "installed; this machine can join a site"
            if agent_installed()
            else 'not installed (pip install "neura[site]" to make this machine a site server)',
        )
    out.data(results)
    out.line()
    return 0 if all(r["passed"] for r in results) else 1
