"""`neura deploy|try|rollback|status|cancel|logs`: changing what robots run, and watching it."""

from __future__ import annotations

import argparse
import re
import time
from typing import Any, cast

from neura_cli.commands.sites import robot_state
from neura_cli.session import CliError, Context, Session
from neura_cli.ui import when
from neura_sdk.models import Rollout, RolloutCreate

TERMINAL = {"live", "stopped", "cancelled"}
DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smh]?)$")


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    deploy = sub.add_parser(
        "deploy",
        help="run a policy version on robots",
        description="Run a version on robots: Neura checks it fits, the site prepares and "
        "times it, it runs in shadow beside what runs now, someone at the site approves, and "
        "the robots switch between tasks. Production sites always wait for a person.",
    )
    deploy.add_argument("version", help="POLICY (its latest version), POLICY@v3, or pv_...")
    _robots(deploy, "--to")
    deploy.add_argument(
        "--shadow",
        help="how long it runs in shadow first: 10m, 30s, 0 to skip "
        "(default: 10m on production sites when robots already run something)",
    )
    deploy.add_argument("--note", default="", help="why, for the history")
    _wait(deploy)
    deploy.set_defaults(run=run_deploy, kind="deploy")

    attempt = sub.add_parser(
        "try",
        help="run a version on a bench robot right away",
        description="Deploy to robots of a bench site, with no shadow test: the bench "
        "approves its own activations.",
    )
    attempt.add_argument("version")
    _robots(attempt, "--on")
    _wait(attempt)
    attempt.set_defaults(run=run_deploy, kind="try", shadow="0", note="")

    rollback = sub.add_parser(
        "rollback", help="put robots back on what they ran before their last rollout"
    )
    rollback.add_argument("robots", nargs="*", metavar="ROBOT")
    rollback.add_argument("--site", help="every robot of a site")
    rollback.add_argument("--note", default="")
    _wait(rollback)
    rollback.set_defaults(run=run_rollback)

    status = sub.add_parser("status", help="robots and rollouts, or one of them")
    status.add_argument("target", nargs="?", help="a rollout (ro_...) or a robot")
    status.set_defaults(run=run_status)

    cancel = sub.add_parser("cancel", help="cancel a rollout before its robots switch")
    cancel.add_argument("rollout")
    cancel.set_defaults(run=run_cancel)

    logs = sub.add_parser("logs", help="what happened, one line each")
    logs.add_argument("--site")
    logs.add_argument("--robot")
    logs.add_argument("--rollout")
    logs.add_argument("-n", type=int, default=30, help="how many lines (default 30)")
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    logs.set_defaults(run=run_logs)


def _robots(parser: argparse.ArgumentParser, flag: str) -> None:
    parser.add_argument(
        flag,
        dest="to",
        action="append",
        default=[],
        metavar="ROBOT",
        help="robots, by name (repeat, or separate with commas)",
    )
    parser.add_argument("--site", help="every robot of a site")


def _wait(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-wait", action="store_true", help="start it and return")
    parser.add_argument(
        "--timeout", type=float, default=3600, help="seconds to follow it (default 3600)"
    )


def seconds(text: str) -> float:
    match = DURATION.match(text.strip())
    if not match:
        raise CliError(
            f"{text} is not a duration.", "Write it as 30s, 10m, 1h or a number of seconds."
        )
    value, unit = float(match.group(1)), match.group(2)
    return value * {"": 1, "s": 1, "m": 60, "h": 3600}[unit]


def _chosen(session: Session, names: list[str], site: str | None) -> list[dict[str, Any]]:
    wanted = [n.strip() for item in names for n in item.split(",") if n.strip()]
    if wanted:
        return session.robot_ids(wanted)
    if site is not None:
        robots = session.robots(session.site(site)["metadata"]["id"])
        if not robots:
            raise CliError(f"{site} has no robots yet.")
        return robots
    robots = session.robots()
    if len(robots) == 1:
        return robots
    if not robots:
        raise CliError("This workspace has no robots yet.", "Join a site: neura site join NAME")
    raise CliError(
        "Which robots?",
        "This workspace has " + ", ".join(r["spec"]["name"] for r in robots) + ".",
        "Name them with --to, or a whole site with --site.",
    )


def run_deploy(args: argparse.Namespace, ctx: Context) -> int:
    session = ctx.session()
    version = session.version(args.version)
    robots = _chosen(session, args.to, args.site)
    body: RolloutCreate = {
        "spec": {
            "kind": args.kind,
            "robot_ids": [r["metadata"]["id"] for r in robots],
            "policy_version_id": version["metadata"]["id"],
            "note": args.note,
        }
    }
    if args.shadow is not None:
        body["spec"]["shadow_s"] = seconds(args.shadow)
    rollout = session.client.create_rollout(session.project_id, body=body)
    names = ", ".join(r["spec"]["name"] for r in robots)
    verb = "Trying" if args.kind == "try" else "Deploying"
    return _follow(ctx, rollout, f"{verb} {version['status']['name']} on {names}", args)


def run_rollback(args: argparse.Namespace, ctx: Context) -> int:
    session = ctx.session()
    robots = _chosen(session, args.robots, args.site)
    rollout = session.client.create_rollout(
        session.project_id,
        body={
            "spec": {
                "kind": "rollback",
                "robot_ids": [r["metadata"]["id"] for r in robots],
                "note": args.note,
            }
        },
    )
    names = ", ".join(r["spec"]["name"] for r in robots)
    return _follow(ctx, rollout, f"Rolling back {names}", args)


def _follow(ctx: Context, rollout: Rollout, title: str, args: argparse.Namespace) -> int:
    session, out = ctx.session(), ctx.out
    out.header(title)
    rollout_id = rollout["metadata"]["id"]
    shown: set[tuple[str, str, str]] = set()
    deadline = time.monotonic() + args.timeout
    current: dict[str, Any] = cast(dict[str, Any], rollout)
    while True:
        for step in current["status"]["steps"]:
            key = (step["name"], step["state"], step.get("summary", ""))
            if step["state"] != "pending" and key not in shown:
                shown.add(key)
                out.step(step)
        if current["status"]["state"] in TERMINAL or args.no_wait:
            break
        if time.monotonic() > deadline:
            out.line()
            out.hint(f"Still {current['status']['state']}: neura status {rollout_id}")
            out.data(current)
            return 0
        time.sleep(1)
        current = cast(dict[str, Any], session.client.get_rollout(rollout_id))
    out.data(current)
    return _outcome(ctx, current)


def _outcome(ctx: Context, rollout: dict[str, Any]) -> int:
    out, status = ctx.out, rollout["status"]
    out.line()
    state = status["state"]
    if state == "live":
        out.line(f"  {out.s.good('Live')}  {status['name']}")
    elif state in ("stopped", "cancelled"):
        why = status.get("message") or status.get("reason") or ""
        out.line(f"  {out.s.bad(state.capitalize())}  {why}")
        out.hint("The robots kept running what they ran before.")
    else:
        out.line(f"  {out.s.warn(state.replace('_', ' '))}  {status['name']}")
        if state == "awaiting_approval":
            out.hint("Someone at the site approves it: neura approve, on the site's server.")
        out.hint(f"Follow it: neura status {rollout['metadata']['id']}")
    out.line()
    return 1 if state in ("stopped", "cancelled") else 0


def run_cancel(args: argparse.Namespace, ctx: Context) -> int:
    rollout = ctx.session().client.cancel_rollout(args.rollout)
    ctx.out.data(rollout)
    ctx.out.ok(f"Cancelled {rollout['status']['name']}", "the robots keep what they run")
    return 0


# --- status ------------------------------------------------------------------------------------


def run_status(args: argparse.Namespace, ctx: Context) -> int:
    session, out = ctx.session(), ctx.out
    if args.target and args.target.startswith("ro_"):
        rollout = cast(dict[str, Any], session.client.get_rollout(args.target))
        out.data(rollout)
        out.header(rollout["status"]["name"])
        for step in rollout["status"]["steps"]:
            out.step(step)
        return _outcome(ctx, rollout)
    sites = {s["metadata"]["id"]: s["spec"]["name"] for s in session.sites()}
    robots = session.robots()
    robot_id = None
    if args.target:
        robot_id = session.robot_ids([args.target])[0]["metadata"]["id"]
        robots = [r for r in robots if r["metadata"]["id"] == robot_id]
    page = session.client.list_rollouts(session.project_id, robot_id=robot_id, page_size=10)
    rollouts = list(page["items"])
    out.data({"robots": robots, "rollouts": rollouts})
    out.line()
    if robots:
        out.table(
            ("ROBOT", "SITE", "STATE", "RUNNING"),
            (
                (
                    r["spec"]["name"],
                    sites.get(r["spec"]["site_id"], ""),
                    robot_state(r),
                    f"{r['status']['policy_name']} v{r['status']['policy_version']}"
                    if r["status"].get("policy_name")
                    else "—",
                )
                for r in robots
            ),
        )
        out.line()
    if rollouts:
        out.table(
            ("ROLLOUT", "STATE", "STARTED", "ID"),
            (
                (
                    r["status"]["name"],
                    r["status"]["state"].replace("_", " "),
                    when(r["metadata"].get("created_at")),
                    r["metadata"]["id"],
                )
                for r in rollouts
            ),
        )
        out.line()
    if not robots and not rollouts:
        out.line("  Nothing yet. Start here: neura site create lab --bench")
        out.line()
    return 0


# --- logs --------------------------------------------------------------------------------------


def run_logs(args: argparse.Namespace, ctx: Context) -> int:
    session, out = ctx.session(), ctx.out
    filters = {
        "site_id": session.site(args.site)["metadata"]["id"] if args.site else None,
        "robot_id": session.robot_ids([args.robot])[0]["metadata"]["id"] if args.robot else None,
        "rollout_id": args.rollout,
    }
    page = session.client.list_activity(session.project_id, page_size=args.n, **filters)
    items = list(reversed(page["items"]))
    for item in items:
        _activity(ctx, cast(dict[str, Any], item))
    if not args.follow:
        out.data(items)
        return 0
    last = max((int(i["cursor"]) for i in items), default=0)
    try:
        while True:
            time.sleep(2)
            fresh = session.client.list_activity(session.project_id, page_size=50, **filters)
            new = [i for i in fresh["items"] if int(i["cursor"]) > last]
            for item in reversed(new):
                _activity(ctx, cast(dict[str, Any], item))
                last = max(last, int(item["cursor"]))
    except KeyboardInterrupt:
        return 0


def _activity(ctx: Context, item: dict[str, Any]) -> None:
    s = ctx.out.s
    text = item["summary"]
    if item.get("level") == "warning":
        text = s.warn(text)
    elif item.get("level") == "error":
        text = s.bad(text)
    ctx.out.line(f"  {s.quiet(when(item['occurred_at']))}  {text}")
