"""Organization members: invitations by verified email, roles, and the last-admin guard (design
§19.2, §20.7); and the sign-in configuration the console reads from /v1/meta."""

from __future__ import annotations

from typing import Any

from conftest import AppFactory, ConformanceClient, OidcIssuer
from neura_control.security.principal import Role

ADMIN = "https://sso.example.com|root"


def members(client: ConformanceClient, org: str) -> list[dict[str, Any]]:
    response = client.get(f"/v1/organizations/{org}/members")
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def new_project(client: ConformanceClient, org: str) -> str:
    project: str = client.post(
        f"/v1/organizations/{org}/projects", {"spec": {"name": "p", "display_name": "P"}}
    ).json()["metadata"]["id"]
    return project


def test_an_invitation_by_email_binds_on_first_verified_sign_in(make_api: AppFactory) -> None:
    issuer = OidcIssuer()
    client, org = make_api(oidc_verifier=issuer.verifier, memberships={ADMIN: Role.ADMIN})
    project = new_project(client, org)
    invited = client.post(
        f"/v1/organizations/{org}/members", {"email": "Ana@Customer.example", "role": "deployer"}
    )
    assert invited.status_code == 201, invited.text
    member = invited.json()
    assert (member["email"], member["state"], member["subject"]) == (
        "ana@customer.example",
        "Invited",
        None,
    )

    # An unverified email claim does not accept the invitation.
    unverified = client.as_token(
        issuer.token("ana", email="ana@customer.example", email_verified=False)
    )
    assert unverified.get(f"/v1/projects/{project}").status_code == 404

    ana = client.as_token(issuer.token("ana", email="ANA@customer.example", email_verified=True))
    assert ana.get(f"/v1/projects/{project}").status_code == 200
    me = ana.get("/v1/me").json()
    assert me["organizations"][0]["permissions"] == ["deploy", "read"]
    accepted = next(m for m in members(client, org) if m["email"] == "ana@customer.example")
    assert (accepted["state"], accepted["subject"]) == ("Active", "https://sso.example.com|ana")

    # The invitation is bound to that subject: someone else with the same email later gets nothing.
    other = client.as_token(issuer.token("eve", email="ana@customer.example", email_verified=True))
    assert other.get(f"/v1/projects/{project}").status_code == 404


def test_members_can_be_added_by_subject_and_duplicates_conflict(make_api: AppFactory) -> None:
    client, org = make_api()
    added = client.post(
        f"/v1/organizations/{org}/members",
        {"subject": "https://sso.example.com|bob", "role": "viewer"},
    )
    assert added.status_code == 201
    assert added.json()["state"] == "Active"
    again = client.post(
        f"/v1/organizations/{org}/members",
        {"subject": "https://sso.example.com|bob", "role": "admin"},
    )
    assert again.status_code == 409
    for body in ({"role": "viewer"}, {"role": "viewer", "email": "a@b.co", "subject": "x|y"}):
        assert client.post(f"/v1/organizations/{org}/members", body).status_code == 422
    assert (
        client.post(
            f"/v1/organizations/{org}/members", {"role": "owner", "email": "a@b.co"}
        ).status_code
        == 422
    )


def test_roles_change_with_if_match_and_an_admin_always_remains(make_api: AppFactory) -> None:
    issuer = OidcIssuer()
    client, org = make_api(oidc_verifier=issuer.verifier, memberships={ADMIN: Role.ADMIN})
    root = next(m for m in members(client, org) if m["subject"] == ADMIN)
    path = f"/v1/members/{root['metadata']['id']}"

    demote = client.patch(path, {"role": "viewer"}, version=root["metadata"]["version"])
    assert demote.status_code == 409
    assert demote.json()["reason"] == "API_CONFLICT"
    assert client.delete(path, version=root["metadata"]["version"]).status_code == 409

    second = client.post(
        f"/v1/organizations/{org}/members",
        {"subject": "https://sso.example.com|deputy", "role": "admin"},
    ).json()
    stale = client.patch(path, {"role": "viewer"}, version="999")
    assert stale.status_code == 412
    demoted = client.patch(path, {"role": "viewer"}, version=root["metadata"]["version"])
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["role"] == "viewer"
    # The demoted user is now a viewer.
    viewer = client.as_token(issuer.token("root"))
    assert viewer.get(f"/v1/organizations/{org}/members").status_code == 403
    removed = client.delete(
        f"/v1/members/{second['metadata']['id']}", version=second["metadata"]["version"]
    )
    assert removed.status_code == 409  # deputy is now the only active admin


def test_only_admins_manage_members(make_api: AppFactory) -> None:
    issuer = OidcIssuer()
    client, org = make_api(
        oidc_verifier=issuer.verifier,
        memberships={"https://sso.example.com|dee": Role.DEPLOYER},
    )
    deployer = client.as_token(issuer.token("dee"))
    assert deployer.get(f"/v1/organizations/{org}/members").status_code == 403
    response = deployer.post(
        f"/v1/organizations/{org}/members", {"email": "x@y.example", "role": "admin"}
    )
    assert response.status_code == 403


def test_meta_publishes_how_to_sign_in(make_api: AppFactory) -> None:
    client, _ = make_api()
    auth = client.get("/v1/meta", token=None).json()["authentication"]
    assert auth == {"api_tokens": True, "oidc": None}

    sso, _ = make_api(
        settings_overrides={
            "oidc_issuer": "https://sso.example.com",
            "oidc_audience": "neura",
            "oidc_jwks_url": "https://sso.example.com/jwks",
            "console_oidc_client_id": "neura-console",
        }
    )
    oidc = sso.get("/v1/meta", token=None).json()["authentication"]["oidc"]
    assert oidc == {
        "issuer": "https://sso.example.com",
        "client_id": "neura-console",
        "audience": "neura",
        "scopes": ["openid", "profile", "email"],
    }
