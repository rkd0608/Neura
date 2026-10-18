"""Release gates (design §28.3): the evidence a revision needs before robots switch to it from
the revision they run.

A deployment lists the kinds of evaluation it requires. A revision passes a kind when an
evaluation of that kind, of this revision against the revision a robot runs now, succeeded with a
``superior`` or ``non_inferior`` verdict. Robots that run nothing yet, or already run the revision,
need no evidence; neither does a rollback, which returns to a revision robots ran before.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_control.db.models import Deployment, Evaluation

PASSING = ("superior", "non_inferior")


async def unmet(
    session: AsyncSession, deployment: Deployment, revision_id: str, robot_ids: list[str]
) -> list[str]:
    """What the revision still needs before these robots may switch to it, as sentences."""
    required = list((deployment.release_gates or {}).get("evaluations", []))
    if not required:
        return []
    active = deployment.observed.get("active_revision_ids", {})
    replaced = sorted({active[r] for r in robot_ids if active.get(r) and active[r] != revision_id})
    if not replaced:
        return []
    passed = {
        (kind, baseline)
        for kind, baseline in (
            await session.execute(
                select(Evaluation.suite["kind"].astext, Evaluation.baseline_revision_id).where(
                    Evaluation.deployment_id == deployment.id,
                    Evaluation.candidate_revision_id == revision_id,
                    Evaluation.phase == "Succeeded",
                    Evaluation.verdict.in_(PASSING),
                )
            )
        ).all()
    }
    return [
        f"a {kind} evaluation of {revision_id} against {baseline} with a superior or "
        "non_inferior verdict"
        for baseline in replaced
        for kind in required
        if (kind, baseline) not in passed
    ]
