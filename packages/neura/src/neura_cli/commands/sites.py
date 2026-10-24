"""`neura site ...` and `neura robots`: where robots are, and the robots there."""

from __future__ import annotations

import argparse
import shlex
import time
from typing import Any, Literal

from neura_cli import keys
from neura_cli.local_site import LocalSite, agent_installed
from neura_cli.session import CliError, Context
from neura_cli.ui import when

LIVE = {"Ready", "Running", "Serving", "Healthy"}


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    site = sub.add_parser("site", help="sites: create one, join this machine to one")
    actions = site.add_subparsers(dest="action", metavar="ACTION", required=True)

    create = actions.add_parser("create", help="create a site")
    create.add_argument("name")
    create.add_argument(
        "--bench",
        action="store_true",
        help="a test bench: it approves its own activations (never near people)",
    )
    create.add_argument("--display-name")
    create.add_argument(
        "--offline-hours",
        type=int,
        default=72,
        help="how long robots keep running what they run without Neura (default 72)",
    )
    create.set_defaults(run=run_create)

    actions.add_parser("list", help="the workspace's sites").set_defaults(run=run_list)
    show = actions.add_parser("show", help="a site: its servers and robots")
    show.add_argument("name")
    show.set_defaults(run=run_show)

    install = actions.add_parser(
        "install-command", help="the one-line command that adds a server to a site"
    )
    install.add_argument("name")
    install.set_defaults(run=run_install_command)

    join = actions.add_parser(
        "join",
        help="make this machine a server of a site",
        description="Enroll this machine in a site and run its agent for you: it finds the "
        "robots plugged in (or simulates some) and runs what is deployed to them. Signed in, "
        "name the site; on a machine that is not, use the command the console shows.",
    )
    join.add_argument("name")
    join.add_argument(
        "--control-plane",
        help="the agent endpoint (host:port): from the console's join command, or to reach "
        "Neura by another address than the one it advertises",
    )
    join.add_argument("--token", help="from the console's join command (single use)")
    join.add_argument("--trust", action="append", default=[], help="a publisher key to trust")
    join.add_argument("--bench", action="store_true", help="from the console's join command")
    join.add_argument("--simulate", type=int, default=0, metavar="N", help="simulate N arms")
    join.add_argument(
        "--mock-policies",
        action="store_true",
        help="answer with a mock policy instead of running models (no PyTorch needed)",
    )
    join.add_argument("--foreground", action="store_true", help="run the agent in this terminal")
    join.add_argument("--api", help="Neura's address, when not signed in")
    join.set_defaults(run=run_join)

    for name, run, text in (
        ("start", run_start, "start this machine's agent for a site"),
        ("stop", run_stop, "stop this machine's agent for a site"),
    ):
        command = actions.add_parser(name, help=text)
        command.add_argument("name")
        command.set_defaults(run=run)

    robots = sub.add_parser("robots", help="the workspace's robots and what they run")
    robots.add_argument("--site")
    robots.set_defaults(run=run_robots)


# --- create, list, show ------------------------------------------------------------------------


def run_create(args: argparse.Namespace, ctx: Context) -> int:
    session, out = ctx.session(), ctx.out
    kind: Literal["bench", "production"] = "bench" if args.bench else "production"
    site = session.client.create_site(
        session.project_id,
        body={
            "spec": {
                "name": args.name,
                "display_name": args.display_name or args.name,
                "offline_authorization_hours": args.offline_hours,
                "kind": kind,
            }
        },
    )
    out.data(site)
    out.line()
    out.ok(f"Created {kind} site {args.name}")
    out.line()
    out.line("  Add a computer to it:")
    out.line(
        f"    this one        neura site join {args.name}" + (" --simulate 1" if args.bench else "")
    )
    out.line(f"    another one     neura site install-command {args.name}")
    out.line()
    return 0


def _state(site: dict[str, Any]) -> str:
    status = site["status"]
    if status.get("connected"):
        return "connected"
    return "offline" if status.get("agent_version") else "no server yet"


def run_list(args: argparse.Namespace, ctx: Context) -> int:
    session, out = ctx.session(), ctx.out
    sites = session.sites()
    robots = session.robots()
    out.data(sites)
    if not sites:
        out.line()
        out.line("  No sites yet. Create one: neura site create lab --bench")
        out.line()
        return 0
    out.line()
    out.table(
        ("SITE", "KIND", "STATE", "ROBOTS"),
        (
            (
                s["spec"]["name"],
                s["spec"].get("kind", "production"),
                _colored(ctx, _state(s)),
                str(sum(1 for r in robots if r["spec"]["site_id"] == s["metadata"]["id"])),
            )
            for s in sites
        ),
    )
    out.line()
    return 0


def _colored(ctx: Context, state: str) -> str:
    s = ctx.out.s
    if state in ("connected", "ready", "running"):
        return s.good(state)
    if state in ("offline", "missing", "needs setup", "attention"):
        return s.warn(state)
    return s.quiet(state)


def run_show(args: argparse.Namespace, ctx: Context) -> int:
    session, out = ctx.session(), ctx.out
    site = session.site(args.name)
    site_id = site["metadata"]["id"]
    servers = list(session.client.paginate(session.client.list_servers, site_id))
    robots = session.robots(site_id)
    out.data({"site": site, "servers": servers, "robots": robots})
    out.header(f"{site['spec']['name']}  {out.s.quiet(site['spec'].get('kind', 'production'))}")
    out.line(
        f"  {_colored(ctx, _state(site))}"
        + (
            f"  ·  agent {site['status']['agent_version']}"
            if site["status"].get("agent_version")
            else ""
        )
    )
    out.line()
    if servers:
        out.table(
            ("SERVER", "STATE", "SINCE"),
            (
                (
                    s["status"].get("hostname") or s["spec"]["display_name"],
                    s["status"].get("phase", ""),
                    when(s["metadata"].get("created_at")),
                )
                for s in servers
            ),
        )
        out.line()
    _robot_table(ctx, robots, {site_id: site["spec"]["name"]})
    return 0


def run_install_command(args: argparse.Namespace, ctx: Context) -> int:
    session, out = ctx.session(), ctx.out
    site = session.site(args.name)
    token = session.client.create_enrollment_token(site["metadata"]["id"], body={})
    out.data(token)
    out.line()
    out.line(
        f"  On the server, once the agent is installed ({out.s.quiet('works once, for an hour')}):"
    )
    out.line()
    out.line(f"    {token['install_command']}")
    out.line()
    if "join_command" in token:
        out.hint("A workstation or bench without a service manager:")
        out.hint(f"  {token['join_command']}")
        out.line()
    return 0


# --- join, start, stop -------------------------------------------------------------------------


def _parse_join(command: str) -> dict[str, Any]:
    words = shlex.split(command)
    found: dict[str, Any] = {"trust": [], "bench": False}
    for index, word in enumerate(words):
        if word == "--control-plane":
            found["control_plane"] = words[index + 1]
        elif word == "--token":
            found["token"] = words[index + 1]
        elif word == "--trust":
            found["trust"].append(words[index + 1])
        elif word == "--bench":
            found["bench"] = True
    return found


def run_join(args: argparse.Namespace, ctx: Context) -> int:
    config, out = ctx.config, ctx.out
    if args.api:
        config.api_url = args.api.rstrip("/")
    local = LocalSite.at(args.name, config.local_sites.get(args.name))
    if local.exists() and (local.state_dir / "identity.json").exists():
        out.line()
        out.ok(f"This machine is already a server of {args.name}")
        return _start(ctx, local, foreground=args.foreground)
    if not agent_installed():
        raise CliError(
            "The site agent is not installed in this Python environment.",
            'Install it: pip install "neura[site]"',
            f"Or install it as a service: neura site install-command {args.name}",
        )
    trust = list(args.trust)
    bench = args.bench
    if args.token:
        if not args.control_plane:
            raise CliError("--token needs --control-plane, as the console's join command gives.")
        endpoint, token = args.control_plane, args.token
    else:
        session = ctx.session()
        site = session.site(args.name)
        issued = session.client.create_enrollment_token(site["metadata"]["id"], body={})
        parsed = _parse_join(issued.get("join_command", ""))
        endpoint, token = args.control_plane or parsed["control_plane"], issued["secret"]
        trust += parsed["trust"]
        bench = site["spec"].get("kind") == "bench"
        # Versions this machine pushes run here without anything else to set up.
        key = keys.load(config, create=True)
        assert key is not None
        keys.ensure_registered(session, key)
        trust.append(keys.public_hex(key))
    if not bench:
        raise CliError(
            f"{args.name} is a production site: its servers run the agent as a system service.",
            f"On the server: neura site install-command {args.name}",
            "To try things on this machine, use a bench: "
            f"neura site create {args.name}-bench --bench",
        )
    out.header(f"Joining {args.name}")
    local.configure(
        api_url=config.api_url,
        bench=True,
        simulated_robots=args.simulate,
        trusted_keys=sorted(set(trust)),
        mock_policies=args.mock_policies,
    )
    local.enroll(endpoint, token)
    out.ok("Enrolled this machine", str(local.directory))
    config.local_sites[args.name] = str(local.directory)
    config.save()
    return _start(ctx, local, foreground=args.foreground)


def _start(ctx: Context, local: LocalSite, *, foreground: bool) -> int:
    out = ctx.out
    if foreground:
        out.hint("The agent runs in this terminal; Ctrl-C stops it.")
        return local.run_in_foreground()
    pid = local.start()
    status = local.wait_until_connected()
    out.ok("Agent running", f"pid {pid}, log {local.log_file}")
    out.data({"local_site": str(local.directory), "pid": pid, "status": status})
    if not ctx.config.token:
        out.line()
        return 0
    session = ctx.session()
    site_id = status.get("identity", {}).get("site_id")
    robots: list[dict[str, Any]] = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        robots = session.robots(site_id)
        if robots:
            break
        time.sleep(1)
    out.line()
    if robots:
        _robot_table(ctx, robots, {site_id: local.name} if site_id else {})
    else:
        out.line("  No robots found yet. Plug an arm in, or simulate one:")
        out.line(f"    neura site stop {local.name} && neura site join {local.name} --simulate 1")
        out.line()
    return 0


def run_start(args: argparse.Namespace, ctx: Context) -> int:
    local = _local(ctx, args.name)
    return _start(ctx, local, foreground=False)


def run_stop(args: argparse.Namespace, ctx: Context) -> int:
    local = _local(ctx, args.name)
    if local.stop():
        ctx.out.ok(f"Stopped {args.name}'s agent on this machine", "robots it drives stop too")
    else:
        ctx.out.hint(f"{args.name}'s agent was not running here.")
    return 0


def _local(ctx: Context, name: str) -> LocalSite:
    local = LocalSite.at(name, ctx.config.local_sites.get(name))
    if not local.exists():
        raise CliError(
            f"This machine is not a server of {name}.", f"Join it: neura site join {name}"
        )
    return local


# --- robots ------------------------------------------------------------------------------------


def robot_state(robot: dict[str, Any]) -> str:
    status = robot["status"]
    phase = status.get("phase", "Unknown")
    if phase == "Offline":
        return "missing" if status.get("discovered") else "offline"
    if phase == "NeedsAttention":
        return "needs setup" if status.get("discovered") else "attention"
    if status.get("policy_version_id"):
        return "running"
    if phase in LIVE:
        return "ready"
    return str(phase).lower()


def _robot_table(ctx: Context, robots: list[dict[str, Any]], site_names: dict[str, str]) -> None:
    out = ctx.out
    if not robots:
        out.line("  No robots yet.")
        out.line()
        return
    rows = []
    notes = []
    for robot in robots:
        status = robot["status"]
        running = (
            f"{status['policy_name']} v{status['policy_version']}"
            if status.get("policy_name")
            else out.s.quiet("—")
        )
        state = robot_state(robot)
        rows.append(
            (
                robot["spec"]["name"],
                site_names.get(robot["spec"]["site_id"], robot["spec"]["site_id"]),
                _colored(ctx, state),
                running,
            )
        )
        if state in ("needs setup", "attention", "missing") and status.get("message"):
            notes.append(f"{robot['spec']['name']}: {status['message']}")
    out.table(("ROBOT", "SITE", "STATE", "RUNNING"), rows)
    for note in notes:
        out.hint(note)
    out.line()


def run_robots(args: argparse.Namespace, ctx: Context) -> int:
    session = ctx.session()
    sites = session.sites()
    site_id = session.site(args.site)["metadata"]["id"] if args.site else None
    robots = session.robots(site_id)
    ctx.out.data(robots)
    ctx.out.line()
    _robot_table(ctx, robots, {s["metadata"]["id"]: s["spec"]["name"] for s in sites})
    return 0
