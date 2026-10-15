"""Database rows to API representations (api/openapi.yaml component schemas)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from neura_control.clock import iso, utcnow
from neura_control.db import models
from neura_control.services.site_reports import SITE_LIVENESS_S


def metadata(
    row: Any, *, project_id: str | None = None, organization_id: str | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row.id,
        "version": str(row.version),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }
    # Observed records (sessions, faults) are written by the site, not created by a principal.
    created_by = getattr(row, "created_by", None)
    if created_by is not None:
        data["created_by"] = created_by
    if project_id is not None:
        data["project_id"] = project_id
    if organization_id is not None:
        data["organization_id"] = organization_id
    return data


def _age(observed_at: str | None, now: datetime) -> float | None:
    if observed_at is None:
        return None
    at = datetime.fromisoformat(observed_at)
    return max(0.0, round((now - at).total_seconds(), 3))


def observed_status(observed: dict[str, Any], *, default_phase: str = "Unknown") -> dict[str, Any]:
    """Common status fields. ``data_age_seconds`` is computed at read time (design §19.1)."""
    observed_at = observed.get("observed_at")
    return {
        "phase": observed.get("phase", default_phase),
        "reason": observed.get("reason"),
        "message": observed.get("message", ""),
        "observed_revision_id": observed.get("observed_revision_id"),
        "observed_at": observed_at,
        "data_age_seconds": _age(observed_at, utcnow()),
    }


def organization(row: models.Organization) -> dict[str, Any]:
    return {"metadata": metadata(row), "spec": {"display_name": row.display_name}}


def project(row: models.Project) -> dict[str, Any]:
    return {
        "metadata": metadata(row, organization_id=row.organization_id),
        "spec": {"name": row.name, "display_name": row.display_name, "labels": row.labels},
    }


def site(row: models.Site) -> dict[str, Any]:
    status = observed_status(row.observed)
    age = status.get("data_age_seconds")
    connected = (
        bool(row.observed.get("connected", False)) and age is not None and age <= SITE_LIVENESS_S
    )
    if row.observed.get("connected") and not connected:
        status["phase"] = "Offline"
    status.update(
        agent_version=row.observed.get("agent_version"),
        connected=connected,
        offline_authorization_expires_at=row.observed.get("offline_authorization_expires_at"),
    )
    return {
        "metadata": metadata(row, project_id=row.project_id),
        "spec": {
            "name": row.name,
            "display_name": row.display_name,
            "labels": row.labels,
            "offline_authorization_hours": row.offline_authorization_hours,
        },
        "status": status,
    }


def server(row: models.Server) -> dict[str, Any]:
    status = observed_status(row.observed)
    status.update(
        site_id=row.site_id,
        hostname=row.observed.get("hostname", ""),
        driver_version=row.observed.get("driver_version"),
        cuda_version=row.observed.get("cuda_version"),
        gpus=[
            {**gpu, "allocated_to_neura": gpu.get("index") in row.allocated_gpu_indexes}
            for gpu in row.observed.get("gpus", [])
        ],
        preflight=row.observed.get("preflight", []),
    )
    return {
        "metadata": metadata(row, project_id=row.project_id),
        "spec": {
            "display_name": row.display_name,
            "labels": row.labels,
            "allocated_gpu_indexes": row.allocated_gpu_indexes,
        },
        "status": status,
    }


def robot(row: models.Robot) -> dict[str, Any]:
    status = observed_status(row.observed)
    status.update(
        active_revision_id=row.observed.get("active_revision_id"),
        lease_holder_session_id=row.observed.get("lease_holder_session_id"),
        task_epoch=row.observed.get("task_epoch"),
    )
    return {
        "metadata": metadata(row, project_id=row.project_id),
        "spec": {
            "name": row.name,
            "display_name": row.display_name,
            "site_id": row.site_id,
            "robot_profile_id": row.robot_profile_id,
            "labels": row.labels,
        },
        "status": status,
    }


def contract(row: models.ContractDocument) -> dict[str, Any]:
    body: dict[str, Any] = {
        "metadata": metadata(row, project_id=row.project_id),
        "digest": row.digest,
        "document": row.document,
    }
    if row.kind == "ModelArtifact":
        body["signatures"] = list(row.signatures)
    return body


def deployment(row: models.Deployment) -> dict[str, Any]:
    observed = row.observed
    observed_at = observed.get("observed_at")
    rollout = {"strategy": row.rollout["strategy"]}
    if row.rollout.get("canary_robot_id"):
        rollout["canary_robot_id"] = row.rollout["canary_robot_id"]
    return {
        "metadata": metadata(row, project_id=row.project_id),
        "spec": {
            "name": row.name,
            "display_name": row.display_name,
            "site_id": row.site_id,
            "robot_ids": row.robot_ids,
            "desired_revision_id": row.desired_revision_id,
            "rollout": rollout,
            "labels": row.labels,
        },
        "status": {
            "phase": observed.get("phase", "Pending"),
            "reason": observed.get("reason"),
            "message": observed.get("message", ""),
            "prepared_revision_id": observed.get("prepared_revision_id"),
            "active_revision_ids": observed.get(
                "active_revision_ids", dict.fromkeys(row.robot_ids)
            ),
            "observed_at": observed_at,
            "data_age_seconds": _age(observed_at, utcnow()),
        },
    }


def revision(row: models.Revision) -> dict[str, Any]:
    return {
        "metadata": metadata(row, project_id=row.project_id),
        "spec": {
            "model_artifact_id": row.model_artifact_id,
            "execution_profile_id": row.execution_profile_id,
            "robot_profile_id": row.robot_profile_id,
            "notes": row.notes,
        },
        "status": {
            "phase": row.phase,
            "revision_digest": row.revision_digest,
            "compatibility": row.compatibility,
            "reason": row.reason,
            "message": row.message,
            "capacity": row.capacity,
        },
    }


def operation(row: models.Operation) -> dict[str, Any]:
    return {
        "metadata": metadata(row, project_id=row.project_id),
        "kind": row.kind,
        "target": row.target,
        "state": row.state,
        "reason": row.reason,
        "message": row.message,
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
        "result": row.result,
    }


def activation_request(row: models.ActivationRequest) -> dict[str, Any]:
    return {
        "metadata": metadata(row, project_id=row.project_id),
        "revision_id": row.revision_id,
        "robot_ids": row.robot_ids,
        "operation_id": row.operation_id,
        "state": row.state,
        "approved_by": row.approved_by,
        "note": row.note,
    }


def session(row: models.RobotSession) -> dict[str, Any]:
    return {
        "metadata": metadata(row, project_id=row.project_id),
        "robot_id": row.robot_id,
        "site_id": row.site_id,
        "revision_id": row.revision_id,
        "lease_generation": row.lease_generation,
        "task_epoch": row.task_epoch,
        "state": row.state,
        "reason": row.reason,
        "metrics": row.metrics,
        "data_age_seconds": _age(iso(row.observed_at), utcnow()),
    }


def fault(row: models.Fault) -> dict[str, Any]:
    return {
        "metadata": metadata(row, project_id=row.project_id),
        "robot_id": row.robot_id,
        "session_id": row.session_id,
        "revision_id": row.revision_id,
        "reason": row.reason,
        "message": row.message,
        "latched": row.latched,
        "local_response": row.local_response,
        "occurred_at": iso(row.occurred_at),
        "cleared_at": iso(row.cleared_at),
        "trace_ids": row.trace_ids,
    }


def audit_record(row: models.AuditRecord) -> dict[str, Any]:
    return {
        "metadata": {
            "id": row.id,
            "version": "1",
            "created_at": iso(row.occurred_at),
            "updated_at": iso(row.occurred_at),
            "created_by": row.actor,
            "organization_id": row.organization_id,
            **({"project_id": row.project_id} if row.project_id else {}),
        },
        "actor": row.actor,
        "actor_kind": row.actor_kind,
        "action": row.action,
        "target": row.target,
        "result": row.result,
        "reason": row.reason,
        "request_id": row.request_id,
        "occurred_at": iso(row.occurred_at),
    }


def api_token(row: models.ApiToken) -> dict[str, Any]:
    return {
        "metadata": metadata(row, organization_id=row.organization_id),
        "display_name": row.display_name,
        "scopes": row.scopes,
        "project_ids": row.project_ids,
        "expires_at": iso(row.expires_at),
        "last_used_at": iso(row.last_used_at),
    }


def member(row: models.Membership) -> dict[str, Any]:
    return {
        "metadata": metadata(row, organization_id=row.organization_id),
        "role": row.role,
        "email": row.email,
        "subject": row.subject,
        "state": "Active" if row.subject is not None else "Invited",
    }


def support_grant(row: models.SupportGrant) -> dict[str, Any]:
    now = utcnow()
    state = "Revoked" if row.revoked_at else ("Expired" if row.expires_at <= now else "Active")
    return {
        "metadata": metadata(row, organization_id=row.organization_id),
        "site_id": row.site_id,
        "operations": row.operations,
        "justification": row.justification,
        "expires_at": iso(row.expires_at),
        "state": state,
    }
