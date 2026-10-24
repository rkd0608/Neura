"""The `neura` command (design §29.2).

    neura login                          sign in through the browser
    neura site create lab --bench        a site: a bench to try things, or production
    neura site join lab --simulate 1     make this machine a server of it
    neura robots                         the robots sites found, and what they run
    neura policy push ./ckpt --name pick add a version (LeRobot, @neura.policy, a container)
    neura check ./ckpt                   check it fits a robot, without pushing
    neura deploy pick --to arm-1         run it: check, prepare, shadow, approve, switch
    neura try pick --on sim-1            the same on a bench, right away
    neura approve                        at the site: let what waits switch
    neura status | logs | rollback | cancel
    neura doctor                         check this machine's setup

Every command takes --json, which prints the API's objects for scripts instead of text.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from neura_cli import __version__
from neura_cli.commands import account, local, policies, rollouts, sites
from neura_cli.config import Config
from neura_cli.session import CliError, Context
from neura_cli.ui import Output
from neura_sdk import NeuraApiError, OperationFailedError

HINTS = {
    "API_UNAUTHENTICATED": "Sign in again: neura login",
    "API_FORBIDDEN": "Ask a workspace admin for the permission.",
    "API_RATE_LIMITED": "Wait a moment and try again.",
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="neura",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument("--json", action="store_true", help="print JSON for scripts")
    root.add_argument("--version", action="version", version=f"neura {__version__}")
    sub = root.add_subparsers(dest="command", metavar="COMMAND")
    for module in (account, sites, policies, rollouts, local):
        module.register(sub)
    return root


def main(argv: list[str] | None = None) -> int:
    words = list(sys.argv[1:] if argv is None else argv)
    # --json goes anywhere: `neura --json robots` and `neura robots --json`.
    as_json = "--json" in words
    words = [w for w in words if w != "--json"]
    root = parser()
    args = root.parse_args(words)
    out = Output(as_json=as_json)
    if not hasattr(args, "run"):
        root.print_help()
        return 0
    errors = Output(as_json=out.as_json, stream=sys.stderr)
    ctx = Context(Config.load(), out)
    try:
        return int(args.run(args, ctx))
    except CliError as error:
        return _fail(errors, error.what, *error.details)
    except NeuraApiError as error:
        details = [error.detail] if error.detail and error.detail != error.title else []
        for item in error.errors[:5]:
            details.append(f"{item.get('field', '')}: {item.get('message', '')}".strip(": "))
        if error.reason in HINTS:
            details.append(HINTS[error.reason])
        return _fail(errors, error.title or error.reason, *details, reason=error.reason)
    except OperationFailedError as error:
        return _fail(errors, str(error))
    except httpx.ConnectError:
        config = Config.load()
        return _fail(
            errors,
            f"Neura is not reachable at {config.api_url}.",
            "Check the address, or sign in to another one: neura login --api URL",
        )
    except httpx.HTTPError as error:
        return _fail(errors, f"Talking to Neura failed: {error}")
    except KeyboardInterrupt:
        return 130
    finally:
        ctx.close()


def _fail(out: Output, what: str, *details: str, reason: str | None = None) -> int:
    if out.as_json:
        payload = {"error": {"message": what, "details": list(details), "reason": reason}}
        print(json.dumps(payload, indent=2), file=out.stream)
    else:
        out.line()
        out.fail(what, *details)
        out.line()
    return 1


if __name__ == "__main__":
    sys.exit(main())
