"""Generated from api/openapi.yaml by tools/gen_sdk.py. Do not edit."""

from __future__ import annotations

from abc import abstractmethod
from typing import Any
from urllib.parse import quote

from neura_sdk.models import (
    ActivationRequestCreate,
    ActivationRequestPage,
    AgentUpgradeCreate,
    ApiTokenCreate,
    ApiTokenPage,
    ApiTokenWithSecret,
    ArtifactSignature,
    AuditRecordPage,
    CompatibilityCheckRequest,
    CompatibilityReport,
    CurrentPrincipal,
    Deployment,
    DeploymentCreate,
    DeploymentPage,
    DeploymentUpdate,
    EnrollmentToken,
    EnrollmentTokenCreate,
    ExecutionProfile,
    ExecutionProfileCreate,
    ExecutionProfilePage,
    Fault,
    FaultPage,
    Member,
    MemberCreate,
    MemberPage,
    MemberUpdate,
    MetricsQuery,
    MetricsResult,
    ModelArtifact,
    ModelArtifactCreate,
    ModelArtifactPage,
    Operation,
    Organization,
    Project,
    ProjectCreate,
    ProjectPage,
    ProjectUpdate,
    Revision,
    RevisionCreate,
    RevisionPage,
    Robot,
    RobotCreate,
    RobotPage,
    RobotProfile,
    RobotProfileCreate,
    RobotProfilePage,
    RobotUpdate,
    RollbackCreate,
    SealedWeights,
    SealedWeightsPage,
    Server,
    ServerPage,
    ServerUpdate,
    ServiceMeta,
    Session,
    SessionPage,
    Site,
    SiteCreate,
    SitePage,
    SiteUpdate,
    SupportGrant,
    SupportGrantCreate,
    SupportGrantPage,
)


def _segment(value: str) -> str:
    """One path segment: every reserved character, including "/", escaped."""
    return quote(str(value), safe="")


class Operations:
    """One method per API operation. Bodies and results are plain JSON objects."""

    @abstractmethod
    def _call(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        content_type: str | None = None,
        if_match: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any: ...

    def revoke_api_token(
        self,
        api_token_id: str,
        *,
        if_match: str,
    ) -> None:
        """Revoke an API token. (``DELETE /v1/api-tokens/{api_token_id}``)"""
        self._call(
            "DELETE",
            f"/v1/api-tokens/{_segment(api_token_id)}",
            if_match=if_match,
        )

    def get_deployment(
        self,
        deployment_id: str,
    ) -> Deployment:
        """Get a deployment. (``GET /v1/deployments/{deployment_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/deployments/{_segment(deployment_id)}",
        )

    def update_deployment(
        self,
        deployment_id: str,
        *,
        body: DeploymentUpdate,
        if_match: str,
    ) -> Deployment:
        """Update a deployment. (``PATCH /v1/deployments/{deployment_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "PATCH",
            f"/v1/deployments/{_segment(deployment_id)}",
            body=body,
            content_type="application/merge-patch+json",
            if_match=if_match,
        )

    def delete_deployment(
        self,
        deployment_id: str,
        *,
        if_match: str,
    ) -> None:
        """Delete a deployment. (``DELETE /v1/deployments/{deployment_id}``)"""
        self._call(
            "DELETE",
            f"/v1/deployments/{_segment(deployment_id)}",
            if_match=if_match,
        )

    def list_activation_requests(
        self,
        deployment_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> ActivationRequestPage:
        """
        List activation requests. (``GET /v1/deployments/{deployment_id}/activation-
        requests``)
        """
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/deployments/{_segment(deployment_id)}/activation-requests",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def request_activation(
        self,
        deployment_id: str,
        *,
        body: ActivationRequestCreate,
        idempotency_key: str | None = None,
    ) -> Operation:
        """
        Request activation of a prepared revision. (``POST
        /v1/deployments/{deployment_id}/activation-requests``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/deployments/{_segment(deployment_id)}/activation-requests",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def list_revisions(
        self,
        deployment_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> RevisionPage:
        """List a deployment's revisions. (``GET /v1/deployments/{deployment_id}/revisions``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/deployments/{_segment(deployment_id)}/revisions",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def create_revision(
        self,
        deployment_id: str,
        *,
        body: RevisionCreate,
        idempotency_key: str | None = None,
    ) -> Revision:
        """Create an immutable revision. (``POST /v1/deployments/{deployment_id}/revisions``)"""
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/deployments/{_segment(deployment_id)}/revisions",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def rollback_deployment(
        self,
        deployment_id: str,
        *,
        body: RollbackCreate,
        idempotency_key: str | None = None,
    ) -> Operation:
        """
        Roll back to an earlier compatible revision. (``POST
        /v1/deployments/{deployment_id}/rollbacks``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/deployments/{_segment(deployment_id)}/rollbacks",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def get_execution_profile(
        self,
        execution_profile_id: str,
    ) -> ExecutionProfile:
        """
        Get a registered execution profile. (``GET /v1/execution-
        profiles/{execution_profile_id}``)
        """
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/execution-profiles/{_segment(execution_profile_id)}",
        )

    def get_fault(
        self,
        fault_id: str,
    ) -> Fault:
        """Get a fault. (``GET /v1/faults/{fault_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/faults/{_segment(fault_id)}",
        )

    def get_current_principal(
        self,
    ) -> CurrentPrincipal:
        """Who the caller is and which organizations it can access. (``GET /v1/me``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            "/v1/me",
        )

    def get_meta(
        self,
    ) -> ServiceMeta:
        """Service and API version information. (``GET /v1/meta``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            "/v1/meta",
        )

    def get_model_artifact(
        self,
        model_artifact_id: str,
    ) -> ModelArtifact:
        """Get a registered model artifact. (``GET /v1/model-artifacts/{model_artifact_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/model-artifacts/{_segment(model_artifact_id)}",
        )

    def add_model_artifact_signature(
        self,
        model_artifact_id: str,
        *,
        body: ArtifactSignature,
        idempotency_key: str | None = None,
    ) -> ModelArtifact:
        """
        Add a publisher signature to a model artifact. (``POST /v1/model-
        artifacts/{model_artifact_id}/signatures``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/model-artifacts/{_segment(model_artifact_id)}/signatures",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def get_operation(
        self,
        operation_id: str,
    ) -> Operation:
        """Get an operation. (``GET /v1/operations/{operation_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/operations/{_segment(operation_id)}",
        )

    def cancel_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> Operation:
        """
        Cancel an operation that has not finished. (``POST
        /v1/operations/{operation_id}/cancel``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/operations/{_segment(operation_id)}/cancel",
            idempotency_key=idempotency_key,
        )

    def get_organization(
        self,
        organization_id: str,
    ) -> Organization:
        """Get an organization. (``GET /v1/organizations/{organization_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/organizations/{_segment(organization_id)}",
        )

    def list_api_tokens(
        self,
        organization_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> ApiTokenPage:
        """List API tokens. (``GET /v1/organizations/{organization_id}/api-tokens``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/organizations/{_segment(organization_id)}/api-tokens",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def create_api_token(
        self,
        organization_id: str,
        *,
        body: ApiTokenCreate,
        idempotency_key: str | None = None,
    ) -> ApiTokenWithSecret:
        """
        Create an API token. The secret is returned once. (``POST
        /v1/organizations/{organization_id}/api-tokens``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/organizations/{_segment(organization_id)}/api-tokens",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def list_projects(
        self,
        organization_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> ProjectPage:
        """List projects. (``GET /v1/organizations/{organization_id}/projects``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/organizations/{_segment(organization_id)}/projects",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def create_project(
        self,
        organization_id: str,
        *,
        body: ProjectCreate,
        idempotency_key: str | None = None,
    ) -> Project:
        """Create a project. (``POST /v1/organizations/{organization_id}/projects``)"""
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/organizations/{_segment(organization_id)}/projects",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def list_support_grants(
        self,
        organization_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> SupportGrantPage:
        """
        List support access grants. (``GET /v1/organizations/{organization_id}/support-
        grants``)
        """
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/organizations/{_segment(organization_id)}/support-grants",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def create_support_grant(
        self,
        organization_id: str,
        *,
        body: SupportGrantCreate,
        idempotency_key: str | None = None,
    ) -> SupportGrant:
        """
        Grant time-boxed support access. (``POST /v1/organizations/{organization_id}/support-
        grants``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/organizations/{_segment(organization_id)}/support-grants",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def get_project(
        self,
        project_id: str,
    ) -> Project:
        """Get a project. (``GET /v1/projects/{project_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}",
        )

    def update_project(
        self,
        project_id: str,
        *,
        body: ProjectUpdate,
        if_match: str,
    ) -> Project:
        """Update a project. (``PATCH /v1/projects/{project_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "PATCH",
            f"/v1/projects/{_segment(project_id)}",
            body=body,
            content_type="application/merge-patch+json",
            if_match=if_match,
        )

    def list_audit_records(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> AuditRecordPage:
        """List audit records. (``GET /v1/projects/{project_id}/audit-records``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/audit-records",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def check_compatibility(
        self,
        project_id: str,
        *,
        body: CompatibilityCheckRequest,
    ) -> CompatibilityReport:
        """
        Check a model artifact, robot profile and execution profile together. (``POST
        /v1/projects/{project_id}/compatibility-checks``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/projects/{_segment(project_id)}/compatibility-checks",
            body=body,
            content_type="application/json",
        )

    def list_deployments(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> DeploymentPage:
        """List deployments. (``GET /v1/projects/{project_id}/deployments``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/deployments",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def create_deployment(
        self,
        project_id: str,
        *,
        body: DeploymentCreate,
        idempotency_key: str | None = None,
    ) -> Deployment:
        """Create a deployment. (``POST /v1/projects/{project_id}/deployments``)"""
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/projects/{_segment(project_id)}/deployments",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def list_execution_profiles(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> ExecutionProfilePage:
        """
        List registered execution profiles. (``GET /v1/projects/{project_id}/execution-
        profiles``)
        """
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/execution-profiles",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def register_execution_profile(
        self,
        project_id: str,
        *,
        body: ExecutionProfileCreate,
        idempotency_key: str | None = None,
    ) -> ExecutionProfile:
        """
        Register an immutable execution profile document. (``POST
        /v1/projects/{project_id}/execution-profiles``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/projects/{_segment(project_id)}/execution-profiles",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def list_faults(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> FaultPage:
        """List faults. (``GET /v1/projects/{project_id}/faults``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/faults",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def query_metrics(
        self,
        project_id: str,
        *,
        body: MetricsQuery,
    ) -> MetricsResult:
        """Query aggregate metrics. (``POST /v1/projects/{project_id}/metrics/query``)"""
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/projects/{_segment(project_id)}/metrics/query",
            body=body,
            content_type="application/json",
        )

    def list_model_artifacts(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> ModelArtifactPage:
        """List registered model artifacts. (``GET /v1/projects/{project_id}/model-artifacts``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/model-artifacts",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def register_model_artifact(
        self,
        project_id: str,
        *,
        body: ModelArtifactCreate,
        idempotency_key: str | None = None,
    ) -> ModelArtifact:
        """
        Register an immutable model artifact document. (``POST
        /v1/projects/{project_id}/model-artifacts``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/projects/{_segment(project_id)}/model-artifacts",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def list_robot_profiles(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> RobotProfilePage:
        """List registered robot profiles. (``GET /v1/projects/{project_id}/robot-profiles``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/robot-profiles",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def register_robot_profile(
        self,
        project_id: str,
        *,
        body: RobotProfileCreate,
        idempotency_key: str | None = None,
    ) -> RobotProfile:
        """
        Register an immutable robot profile document. (``POST
        /v1/projects/{project_id}/robot-profiles``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/projects/{_segment(project_id)}/robot-profiles",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def list_robots(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> RobotPage:
        """List robots. (``GET /v1/projects/{project_id}/robots``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/robots",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def create_robot(
        self,
        project_id: str,
        *,
        body: RobotCreate,
        idempotency_key: str | None = None,
    ) -> Robot:
        """Create a robot. (``POST /v1/projects/{project_id}/robots``)"""
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/projects/{_segment(project_id)}/robots",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def list_sessions(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> SessionPage:
        """List live and recent robot sessions. (``GET /v1/projects/{project_id}/sessions``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/sessions",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def list_sites(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> SitePage:
        """List sites. (``GET /v1/projects/{project_id}/sites``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/sites",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def create_site(
        self,
        project_id: str,
        *,
        body: SiteCreate,
        idempotency_key: str | None = None,
    ) -> Site:
        """Create a site. (``POST /v1/projects/{project_id}/sites``)"""
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/projects/{_segment(project_id)}/sites",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def get_revision(
        self,
        revision_id: str,
    ) -> Revision:
        """Get a revision. (``GET /v1/revisions/{revision_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/revisions/{_segment(revision_id)}",
        )

    def prepare_revision(
        self,
        revision_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> Operation:
        """
        Prepare a revision at its site: pull, verify, warm and profile. (``POST
        /v1/revisions/{revision_id}/prepare``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/revisions/{_segment(revision_id)}/prepare",
            idempotency_key=idempotency_key,
        )

    def get_robot_profile(
        self,
        robot_profile_id: str,
    ) -> RobotProfile:
        """Get a registered robot profile. (``GET /v1/robot-profiles/{robot_profile_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/robot-profiles/{_segment(robot_profile_id)}",
        )

    def get_robot(
        self,
        robot_id: str,
    ) -> Robot:
        """Get a robot. (``GET /v1/robots/{robot_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/robots/{_segment(robot_id)}",
        )

    def update_robot(
        self,
        robot_id: str,
        *,
        body: RobotUpdate,
        if_match: str,
    ) -> Robot:
        """Update a robot. (``PATCH /v1/robots/{robot_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "PATCH",
            f"/v1/robots/{_segment(robot_id)}",
            body=body,
            content_type="application/merge-patch+json",
            if_match=if_match,
        )

    def delete_robot(
        self,
        robot_id: str,
        *,
        if_match: str,
    ) -> None:
        """Delete a robot. (``DELETE /v1/robots/{robot_id}``)"""
        self._call(
            "DELETE",
            f"/v1/robots/{_segment(robot_id)}",
            if_match=if_match,
        )

    def get_server(
        self,
        server_id: str,
    ) -> Server:
        """Get a server, its GPUs and preflight results. (``GET /v1/servers/{server_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/servers/{_segment(server_id)}",
        )

    def update_server(
        self,
        server_id: str,
        *,
        body: ServerUpdate,
        if_match: str,
    ) -> Server:
        """Update a server's GPU allocation or labels. (``PATCH /v1/servers/{server_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "PATCH",
            f"/v1/servers/{_segment(server_id)}",
            body=body,
            content_type="application/merge-patch+json",
            if_match=if_match,
        )

    def delete_server(
        self,
        server_id: str,
        *,
        if_match: str,
    ) -> None:
        """Unenroll a server and revoke its certificate. (``DELETE /v1/servers/{server_id}``)"""
        self._call(
            "DELETE",
            f"/v1/servers/{_segment(server_id)}",
            if_match=if_match,
        )

    def upgrade_agent(
        self,
        server_id: str,
        *,
        body: AgentUpgradeCreate,
        idempotency_key: str | None = None,
    ) -> Operation:
        """
        Upgrade a server's site agent to a signed release. (``POST
        /v1/servers/{server_id}/agent-upgrades``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/servers/{_segment(server_id)}/agent-upgrades",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def get_session(
        self,
        session_id: str,
    ) -> Session:
        """Get a session. (``GET /v1/sessions/{session_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/sessions/{_segment(session_id)}",
        )

    def get_site(
        self,
        site_id: str,
    ) -> Site:
        """Get a site. (``GET /v1/sites/{site_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/sites/{_segment(site_id)}",
        )

    def update_site(
        self,
        site_id: str,
        *,
        body: SiteUpdate,
        if_match: str,
    ) -> Site:
        """Update a site. (``PATCH /v1/sites/{site_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "PATCH",
            f"/v1/sites/{_segment(site_id)}",
            body=body,
            content_type="application/merge-patch+json",
            if_match=if_match,
        )

    def delete_site(
        self,
        site_id: str,
        *,
        if_match: str,
    ) -> None:
        """Delete a site. (``DELETE /v1/sites/{site_id}``)"""
        self._call(
            "DELETE",
            f"/v1/sites/{_segment(site_id)}",
            if_match=if_match,
        )

    def create_enrollment_token(
        self,
        site_id: str,
        *,
        body: EnrollmentTokenCreate,
        idempotency_key: str | None = None,
    ) -> EnrollmentToken:
        """
        Create a one-time server enrollment token. (``POST /v1/sites/{site_id}/enrollment-
        tokens``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/sites/{_segment(site_id)}/enrollment-tokens",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def list_servers(
        self,
        site_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> ServerPage:
        """List servers enrolled at a site. (``GET /v1/sites/{site_id}/servers``)"""
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/sites/{_segment(site_id)}/servers",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def revoke_support_grant(
        self,
        support_grant_id: str,
        *,
        if_match: str,
    ) -> None:
        """Revoke a support grant. (``DELETE /v1/support-grants/{support_grant_id}``)"""
        self._call(
            "DELETE",
            f"/v1/support-grants/{_segment(support_grant_id)}",
            if_match=if_match,
        )

    def list_members(
        self,
        organization_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
        label: list[str] | None = None,
    ) -> MemberPage:
        """
        List the organization's members and invitations. (``GET
        /v1/organizations/{organization_id}/members``)
        """
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/organizations/{_segment(organization_id)}/members",
            query={"cursor": cursor, "page_size": page_size, "label": label},
        )

    def add_member(
        self,
        organization_id: str,
        *,
        body: MemberCreate,
        idempotency_key: str | None = None,
    ) -> Member:
        """
        Invite a person by email or add an SSO subject. (``POST
        /v1/organizations/{organization_id}/members``)
        """
        return self._call(  # type: ignore[no-any-return]
            "POST",
            f"/v1/organizations/{_segment(organization_id)}/members",
            body=body,
            content_type="application/json",
            idempotency_key=idempotency_key,
        )

    def update_member(
        self,
        member_id: str,
        *,
        body: MemberUpdate,
        if_match: str,
    ) -> Member:
        """Change a member's role. (``PATCH /v1/members/{member_id}``)"""
        return self._call(  # type: ignore[no-any-return]
            "PATCH",
            f"/v1/members/{_segment(member_id)}",
            body=body,
            content_type="application/merge-patch+json",
            if_match=if_match,
        )

    def remove_member(
        self,
        member_id: str,
        *,
        if_match: str,
    ) -> None:
        """Remove a member or withdraw an invitation. (``DELETE /v1/members/{member_id}``)"""
        self._call(
            "DELETE",
            f"/v1/members/{_segment(member_id)}",
            if_match=if_match,
        )

    def list_sealed_weights(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        page_size: int | None = None,
    ) -> SealedWeightsPage:
        """
        List the sealed weight files of a project. (``GET /v1/projects/{project_id}/sealed-
        weights``)
        """
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/sealed-weights",
            query={"cursor": cursor, "page_size": page_size},
        )

    def get_sealed_weights(
        self,
        project_id: str,
        digest: str,
    ) -> SealedWeights:
        """
        Get a sealed weight file's record. (``GET /v1/projects/{project_id}/sealed-
        weights/{digest}``)
        """
        return self._call(  # type: ignore[no-any-return]
            "GET",
            f"/v1/projects/{_segment(project_id)}/sealed-weights/{_segment(digest)}",
        )

    def delete_sealed_weights(
        self,
        project_id: str,
        digest: str,
        *,
        if_match: str,
    ) -> None:
        """
        Remove a sealed weight file from Neura's registry. (``DELETE
        /v1/projects/{project_id}/sealed-weights/{digest}``)
        """
        self._call(
            "DELETE",
            f"/v1/projects/{_segment(project_id)}/sealed-weights/{_segment(digest)}",
            if_match=if_match,
        )
