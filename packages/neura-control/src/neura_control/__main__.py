"""``neura-control`` command line.

neura-control [serve]                 run the API server (default)
neura-control migrate                 apply database migrations
neura-control bootstrap ...           create an organization, an admin and optionally a token
neura-control dev-token               print a new random API token value (for dev bootstrap)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from neura_control.app import create_app
from neura_control.db.engine import create_engine, session_factory
from neura_control.db.migrate import upgrade
from neura_control.security.principal import Role
from neura_control.services import bootstrap
from neura_control.settings import ControlPlaneSettings


def serve(settings: ControlPlaneSettings) -> None:
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=True,
        server_header=False,
    )


async def run_bootstrap(settings: ControlPlaneSettings, args: argparse.Namespace) -> None:
    engine = create_engine(str(settings.database_url), pool_size=1)
    try:
        async with session_factory(engine).begin() as session:
            org = await bootstrap.ensure_organization(session, args.organization)
            print(f"organization: {org.id} ({org.display_name})")
            if args.admin_subject:
                await bootstrap.add_membership(session, org.id, args.admin_subject, Role.ADMIN)
                print(f"admin membership: {args.admin_subject}")
            if args.create_token:
                token = await bootstrap.create_api_token(
                    session,
                    org.id,
                    display_name="bootstrap admin token",
                    scopes=["admin"],
                    expires_in_days=args.token_days,
                )
                print(f"admin API token (shown once): {token}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="neura-control",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the API server")
    sub.add_parser("migrate", help="apply database migrations")
    boot = sub.add_parser("bootstrap", help="create an organization and its first admin")
    boot.add_argument("--organization", required=True, help="organization display name")
    boot.add_argument("--admin-subject", help='OIDC subject as "<issuer>|<sub>"')
    boot.add_argument("--create-token", action="store_true", help="also create an admin API token")
    boot.add_argument("--token-days", type=int, default=30)
    sub.add_parser("dev-token", help="print a new random API token value")
    args = parser.parse_args(argv)

    if args.command == "dev-token":
        print(bootstrap.generate_token_value())
        return
    settings = ControlPlaneSettings()
    if args.command == "migrate":
        upgrade(str(settings.database_url))
        print("database is at the latest schema")
    elif args.command == "bootstrap":
        asyncio.run(run_bootstrap(settings, args))
    else:
        serve(settings)


if __name__ == "__main__":
    main(sys.argv[1:])
