"""Robots a site's agent found (design §29.5): registered without anyone creating them.

Each inventory a server sends is the whole list of robots it drives. A robot it reports for the
first time is created, with its robot profile registered by digest; one it reports again is
updated (its hardware, its profile, whether it needs setting up); one it no longer reports is kept
and shown as missing, never deleted: its history, rollouts and episodes stay attached to it.

A robot's name in Neura is the name its server gives it, unless another robot of the workspace
already has that name; then it is prefixed with its site's name. The robot keeps its server-side
name as ``hardware.local_name``, which the manifest carries so the agent knows which robot is
which.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neura.v1 import site_pb2
from neura_contracts import ContractError, RobotProfile, parse_document
from neura_control.clock import iso, utcnow
from neura_control.db.models import Robot, Site
from neura_control.ids import Kind, new_id
from neura_control.services import contracts, records

log = logging.getLogger("neura.control.inventory")
MAX_ROBOTS = 1000


def _hardware(robot: site_pb2.DiscoveredRobot) -> dict[str, Any]:
    hardware: dict[str, Any] = {
        "local_name": robot.local_name,
        "driver": robot.driver,
        "simulated": robot.simulated,
    }
    if robot.port:
        hardware["port"] = robot.port[:512]
    if robot.serial:
        hardware["serial"] = robot.serial[:128]
    return hardware


async def _name(session: AsyncSession, site: Site, local_name: str) -> str:
    taken = set(
        (await session.scalars(select(Robot.name).where(Robot.project_id == site.project_id))).all()
    )
    for candidate in (local_name, f"{site.name}-{local_name}"):
        if candidate not in taken:
            return candidate[:63]
    suffix = 2
    while f"{site.name}-{local_name}-{suffix}" in taken:
        suffix += 1
    return f"{site.name}-{local_name}-{suffix}"[:63]


def _observed(robot: site_pb2.DiscoveredRobot, previous: dict[str, Any]) -> dict[str, Any]:
    needs_setup = robot.state == "needs_setup"
    observed = {
        **previous,
        "present": True,
        "setup": robot.state or "ready",
        "observed_at": iso(utcnow()),
    }
    if needs_setup:
        observed.update(phase="NeedsAttention", message=robot.message[:2000])
    elif previous.get("phase") in (None, "Unknown", "NeedsAttention", "Offline"):
        observed.update(phase="Ready", message="")
    return observed


async def report(session: AsyncSession, site_id: str, inventory: site_pb2.RobotInventory) -> None:
    site = await session.get(Site, site_id)
    if site is None:
        return
    existing = (
        await session.scalars(
            select(Robot).where(Robot.site_id == site.id, Robot.source == "discovered")
        )
    ).all()
    by_local = {r.hardware.get("local_name"): r for r in existing}
    reported: set[str] = set()
    for item in list(inventory.robots)[:MAX_ROBOTS]:
        local_name = item.local_name.strip()
        if not local_name or local_name in reported:
            continue
        reported.add(local_name)
        try:
            profile = parse_document(json.loads(item.robot_profile_json))
        except (ContractError, ValueError) as error:
            log.warning(
                "site %s reported robot %s with an invalid profile: %s", site.id, local_name, error
            )
            continue
        if not isinstance(profile, RobotProfile):
            continue
        profile_row, new_profile = await contracts.register(
            session, site.project_id, profile, created_by=f"site:{site.id}"
        )
        if new_profile:
            await records.event(
                session,
                project_id=site.project_id,
                type="resource.created",
                resource=profile_row.id,
                version=1,
            )
        row = by_local.get(local_name)
        hardware = _hardware(item)
        if row is None:
            row = Robot(
                id=new_id(Kind.ROBOT),
                project_id=site.project_id,
                site_id=site.id,
                robot_profile_id=profile_row.id,
                name=await _name(session, site, local_name),
                display_name=local_name,
                labels={},
                source="discovered",
                hardware=hardware,
                observed=_observed(item, {}),
                created_by=f"site:{site.id}",
            )
            session.add(row)
            await session.flush()
            kind = "a simulated " if item.simulated else "an "
            what = profile.metadata.name
            await records.event(
                session,
                project_id=site.project_id,
                type="resource.created",
                resource=row.id,
                version=1,
                summary=f"{site.name} found {kind}{what}: {row.name}"
                + (f" ({item.message})" if item.state == "needs_setup" and item.message else ""),
                level="warning" if item.state == "needs_setup" else "info",
                site_id=site.id,
                robot_id=row.id,
            )
            continue
        previous, observed = row.observed, _observed(item, row.observed)
        changed = (
            row.hardware != hardware
            or row.robot_profile_id != profile_row.id
            or {k: previous.get(k) for k in ("present", "setup", "phase")}
            != {k: observed.get(k) for k in ("present", "setup", "phase")}
        )
        row.observed = observed
        if not changed:
            continue
        returned = previous.get("present") is False
        row.hardware, row.robot_profile_id = hardware, profile_row.id
        row.version += 1
        row.updated_at = utcnow()
        await records.event(
            session,
            project_id=site.project_id,
            type="resource.updated",
            resource=row.id,
            version=row.version,
            summary=f"{row.name} is connected to {site.name} again" if returned else None,
            site_id=site.id,
            robot_id=row.id,
        )
    for row in existing:
        if row.hardware.get("local_name") in reported or row.observed.get("present") is False:
            continue
        row.observed = {
            **row.observed,
            "present": False,
            "phase": "Offline",
            "message": f"{site.name}'s server no longer reports it",
            "observed_at": iso(utcnow()),
        }
        row.version += 1
        row.updated_at = utcnow()
        await records.event(
            session,
            project_id=site.project_id,
            type="resource.updated",
            resource=row.id,
            version=row.version,
            summary=f"{row.name} is no longer connected to {site.name}'s server",
            level="warning",
            site_id=site.id,
            robot_id=row.id,
        )
