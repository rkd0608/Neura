"""Who is calling and what they may do (design §19.2, §20.7).

Permissions are evaluated per organization (and, for API tokens, optionally per project). No role
or token scope can approve physical activation: that only happens locally at the site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from neura_contracts import ReasonCode
from neura_control.problems import ProblemError


class Permission(StrEnum):
    READ = "read"
    DEPLOY = "deploy"
    ADMIN = "admin"


class Role(StrEnum):
    VIEWER = "viewer"
    DEPLOYER = "deployer"
    SITE_OPERATOR = "site_operator"
    ADMIN = "admin"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.DEPLOYER: frozenset({Permission.READ, Permission.DEPLOY}),
    # Site operators act locally; in the cloud they can see their sites' state.
    Role.SITE_OPERATOR: frozenset({Permission.READ}),
    Role.ADMIN: frozenset({Permission.READ, Permission.DEPLOY, Permission.ADMIN}),
}

SCOPE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "read": frozenset({Permission.READ}),
    "deploy": frozenset({Permission.READ, Permission.DEPLOY}),
    "admin": frozenset({Permission.READ, Permission.DEPLOY, Permission.ADMIN}),
}


class PrincipalKind(StrEnum):
    USER = "user"
    API_TOKEN = "api_token"  # noqa: S105 - a principal kind, not a secret
    SITE_AGENT = "site_agent"
    SUPPORT = "support"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    kind: PrincipalKind
    # organization id -> permissions
    grants: dict[str, frozenset[Permission]] = field(default_factory=dict)
    # For API tokens restricted to some projects; None means every project in the organization.
    project_ids: frozenset[str] | None = None

    def permissions(
        self, organization_id: str, project_id: str | None = None
    ) -> frozenset[Permission]:
        granted = self.grants.get(organization_id, frozenset())
        if self.project_ids is None:
            return granted
        if project_id is None:
            # A principal limited to some projects may see its organization but never act on it:
            # members, API tokens, support grants and new projects are organization-wide, so
            # allowing them would let a project-limited token grant itself the whole organization.
            return granted & {Permission.READ}
        if project_id not in self.project_ids:
            return frozenset()
        return granted

    def is_member(self, organization_id: str) -> bool:
        return organization_id in self.grants

    def require(
        self, permission: Permission, organization_id: str, project_id: str | None = None
    ) -> None:
        """Raise 404 if the caller cannot see the organization (or project), 403 if it can see
        it but lacks the permission. Not-found for invisible resources avoids leaking existence."""
        visible = self.permissions(organization_id, project_id)
        if not visible:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        if permission not in visible:
            raise ProblemError(ReasonCode.API_FORBIDDEN, f"requires the {permission} permission")
