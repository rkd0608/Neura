"""What a revision records about its documents: their compatibility and the revision's digest."""

from __future__ import annotations

from typing import Any

from neura_contracts import CompatibilityReport, content_digest


def report_json(report: CompatibilityReport) -> dict[str, Any]:
    return {
        "compatible": report.compatible,
        "artifact_digest": report.artifact_digest,
        "robot_profile_digest": report.robot_profile_digest,
        "execution_profile_digest": report.execution_profile_digest,
        "issues": [
            {"code": i.code.value, "path": i.path, "message": i.message} for i in report.issues
        ],
    }


def revision_digest(report: CompatibilityReport) -> str:
    """The digest a site checks a revision's documents against."""
    return content_digest(
        {
            "model_artifact": report.artifact_digest,
            "robot_profile": report.robot_profile_digest,
            "execution_profile": report.execution_profile_digest,
        }
    )
