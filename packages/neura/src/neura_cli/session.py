"""A signed-in CLI: its API client, its workspace, and names turned into identifiers.

People type names (`plant-1`, `arm-2`, `pick@v3`); the API takes identifiers. Every lookup that
fails says what exists instead, so the next command can be typed from the error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from neura_cli.config import Config
from neura_cli.ui import Output
from neura_sdk import Client


class CliError(Exception):
    """A failure to show a person: what happened, then why and what to do, one line each."""

    def __init__(self, what: str, *details: str) -> None:
        super().__init__(what)
        self.what = what
        self.details = details


def client_for(config: Config, *, token: str | None = None) -> Client:
    return Client(config.api_url, token=token or config.token or "", user_agent="neura-cli/0.1")


def public_client(config: Config, *, max_retries: int = 3) -> Client:
    """For what needs no sign-in: starting a sign-in, the API's metadata."""
    return Client(config.api_url, token="", user_agent="neura-cli/0.1", max_retries=max_retries)


@dataclass
class Session:
    config: Config
    client: Client
    organization_id: str
    project_id: str

    @classmethod
    def open(cls, config: Config) -> Session:
        if not config.token:
            raise CliError("You are not signed in.", "Run: neura login")
        client = client_for(config)
        if config.project_id and config.organization_id:
            return cls(config, client, config.organization_id, config.project_id)
        workspace = client.get_workspace(organization_id=config.organization_id)
        config.organization_id = workspace["organization_id"]
        config.project_id = workspace["project_id"]
        return cls(config, client, config.organization_id, config.project_id)

    # --- lookups ----------------------------------------------------------------------------

    def sites(self) -> list[dict[str, Any]]:
        return list(self.client.paginate(self.client.list_sites, self.project_id))

    def site(self, name: str | None) -> dict[str, Any]:
        sites = self.sites()
        if name is None:
            if len(sites) == 1:
                return sites[0]
            if not sites:
                raise CliError(
                    "This workspace has no site yet.", "Create one: neura site create NAME"
                )
            raise CliError(
                "Which site?",
                "This workspace has " + ", ".join(s["spec"]["name"] for s in sites) + ".",
                "Name one with --site.",
            )
        for site in sites:
            if name in (site["spec"]["name"], site["metadata"]["id"]):
                return site
        raise CliError(
            f"There is no site {name}.",
            "Sites: " + (", ".join(s["spec"]["name"] for s in sites) or "none yet") + ".",
        )

    def robots(self, site_id: str | None = None) -> list[dict[str, Any]]:
        robots = list(self.client.paginate(self.client.list_robots, self.project_id))
        if site_id is not None:
            robots = [r for r in robots if r["spec"]["site_id"] == site_id]
        return sorted(robots, key=lambda r: r["spec"]["name"])

    def robot_ids(self, names: list[str]) -> list[dict[str, Any]]:
        robots = self.robots()
        by_name = {r["spec"]["name"]: r for r in robots} | {r["metadata"]["id"]: r for r in robots}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise CliError(
                f"There is no robot {', '.join(missing)}.",
                "Robots: " + (", ".join(r["spec"]["name"] for r in robots) or "none yet") + ".",
                "Robots appear when a site's server finds them: neura robots",
            )
        return [by_name[n] for n in names]

    def policies(self) -> list[dict[str, Any]]:
        return list(self.client.paginate(self.client.list_policies, self.project_id))

    def policy(self, name: str, *, create: bool = False) -> dict[str, Any]:
        for policy in self.policies():
            if name in (policy["spec"]["name"], policy["metadata"]["id"]):
                return policy
        if create:
            created = self.client.create_policy(self.project_id, body={"spec": {"name": name}})
            return cast(dict[str, Any], created)
        names = ", ".join(p["spec"]["name"] for p in self.policies()) or "none yet"
        raise CliError(
            f"There is no policy {name}.",
            f"Policies: {names}.",
            "Add one: neura policy push PATH --name NAME",
        )

    def version(self, reference: str) -> dict[str, Any]:
        """``pick`` (its latest version), ``pick@v3``, ``pick@3`` or a ``pv_`` identifier."""
        if reference.startswith("pv_"):
            return cast(dict[str, Any], self.client.get_policy_version(reference))
        name, _, wanted = reference.partition("@")
        policy = self.policy(name)
        versions = list(
            self.client.paginate(self.client.list_policy_versions, policy["metadata"]["id"])
        )
        if not versions:
            raise CliError(
                f"{name} has no versions yet.", f"Push one: neura policy push PATH --name {name}"
            )
        if not wanted:
            return cast(dict[str, Any], max(versions, key=lambda v: v["status"]["number"]))
        number = wanted.lstrip("v")
        for version in versions:
            if str(version["status"]["number"]) == number:
                return cast(dict[str, Any], version)
        numbers = sorted(v["status"]["number"] for v in versions)
        raise CliError(
            f"{name} has no version {wanted}.",
            "It has " + ", ".join(f"v{n}" for n in numbers) + ".",
        )

    def robot_profile(self, wanted: str | None) -> dict[str, Any]:
        """The robot profile a policy is for: a robot's (``--for arm-1``), a profile by name
        (``--for so101-follower``), or the only one the workspace's robots use."""
        profiles = list(self.client.paginate(self.client.list_robot_profiles, self.project_id))
        by_id = {p["metadata"]["id"]: p for p in profiles}
        robots = self.robots()
        if wanted is not None:
            for robot in robots:
                if wanted in (robot["spec"]["name"], robot["metadata"]["id"]):
                    return dict(by_id[robot["spec"]["robot_profile_id"]]["document"])
            for profile in profiles:
                if wanted in (profile["document"]["metadata"]["name"], profile["metadata"]["id"]):
                    return dict(profile["document"])
            raise CliError(
                f"There is no robot or robot profile {wanted}.",
                "Robots: " + (", ".join(r["spec"]["name"] for r in robots) or "none yet") + ".",
            )
        used = sorted({r["spec"]["robot_profile_id"] for r in robots})
        candidates = used or sorted(by_id)
        if len(candidates) == 1:
            return dict(by_id[candidates[0]]["document"])
        if not candidates:
            raise CliError(
                "This workspace has no robots yet, so there is nothing to fit the policy to.",
                "Add a site and its robots: neura site create NAME, then neura site join NAME",
            )
        names = sorted({by_id[c]["document"]["metadata"]["name"] for c in candidates})
        raise CliError(
            "Which robot is this policy for?",
            "The workspace has " + ", ".join(names) + ".",
            "Name a robot or a profile: --for NAME",
        )


@dataclass
class Context:
    """What every command gets: the configuration, the output, and a session on first use."""

    config: Config
    out: Output
    _session: Session | None = field(default=None, repr=False)

    def session(self) -> Session:
        if self._session is None:
            self._session = Session.open(self.config)
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.client.close()
            self._session = None
