// Organization settings: members and roles, API tokens, support access, sign-in (design §19.2,
// §19.3, §20.7). Admin only; the server enforces it.
import { useQueries } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router";

import { api, ifMatch, unwrap, type Schemas, idempotent } from "../api/client";
import {
  useApiMutation,
  useApiTokens,
  useMe,
  useMembers,
  useProjects,
  usePublisherKeys,
  useSupportGrants,
} from "../api/queries";
import { useAuth } from "../auth/AuthProvider";
import { Dialog } from "../components/Dialog";
import { GearIcon } from "../components/Icons";
import { useToasts } from "../components/Toasts";
import {
  Badge,
  Callout,
  Card,
  CopyField,
  Empty,
  ErrorState,
  Field,
  KeyValue,
  Loading,
  PageHeader,
  Status,
  TabPanel,
  Tabs,
  Time,
  useTab,
  type TabSpec,
} from "../components/ui";
import { usePermissions } from "../lib/context";

const ROLES: { value: Schemas["MemberRole"]; label: string; text: string }[] = [
  { value: "viewer", label: "Viewer", text: "Sees status, metrics and audit; changes nothing." },
  {
    value: "deployer",
    label: "Deployer",
    text: "Registers models, prepares revisions, requests activation and rollback.",
  },
  {
    value: "site_operator",
    label: "Site operator",
    text: "Approves activations locally at their sites; sees state here.",
  },
  {
    value: "admin",
    label: "Admin",
    text: "Manages the organization, projects, sites, members and tokens.",
  },
];

export function Settings() {
  const { orgId = "" } = useParams();
  const can = usePermissions(orgId);
  const me = useMe();
  if (me.isPending) return <Loading />;
  if (!can.admin) {
    return (
      <>
        <PageHeader title="Settings" />
        <Callout tone="info" title="Admins only">
          Ask an admin of this organization to change members, tokens or support access.
        </Callout>
      </>
    );
  }
  return <SettingsTabs orgId={orgId} />;
}

const SETTINGS_TABS: TabSpec[] = [
  { id: "members", label: "Members" },
  { id: "tokens", label: "API tokens" },
  { id: "publishers", label: "Publisher keys" },
  { id: "support", label: "Support access" },
  { id: "sign-in", label: "Sign-in" },
];

function SettingsTabs({ orgId }: { orgId: string }) {
  const [tab, setTab] = useTab(SETTINGS_TABS);
  return (
    <>
      <PageHeader
        icon={<GearIcon />}
        title="Organization settings"
        description="Who can do what. Deploying and approving physical activation are always separate: activation is approved at the site."
      />
      <Tabs label="Settings" tabs={SETTINGS_TABS} value={tab} onChange={setTab} />
      {tab === "members" ? (
        <TabPanel label="Members">
          <Members orgId={orgId} />
        </TabPanel>
      ) : null}
      {tab === "tokens" ? (
        <TabPanel label="API tokens">
          <ApiTokens orgId={orgId} />
        </TabPanel>
      ) : null}
      {tab === "publishers" ? (
        <TabPanel label="Publisher keys">
          <PublisherKeys orgId={orgId} />
        </TabPanel>
      ) : null}
      {tab === "support" ? (
        <TabPanel label="Support access">
          <SupportAccess orgId={orgId} />
        </TabPanel>
      ) : null}
      {tab === "sign-in" ? (
        <TabPanel label="Sign-in">
          <SignIn />
        </TabPanel>
      ) : null}
    </>
  );
}

/** The keys policy versions are signed with: each machine that runs `neura policy push`
 *  registers its own. Sites run only versions signed by a key their operator trusts. */
function PublisherKeys({ orgId }: { orgId: string }) {
  const keys = usePublisherKeys(orgId);
  if (keys.isPending) return <Loading />;
  if (keys.isError) return <ErrorState error={keys.error} />;
  return (
    <Card
      title="Publisher keys"
      description="New sites trust these: the install commands carry them. Removing one here does not make a site distrust it; its operator does that on the server."
      flush={keys.data.length > 0}
    >
      {keys.data.length === 0 ? (
        <p className="secondary">
          None yet. The first <code>neura policy push</code> registers its machine&apos;s key.
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">Publisher keys</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Key</th>
                <th scope="col">Added</th>
              </tr>
            </thead>
            <tbody>
              {keys.data.map((key) => (
                <tr key={key.metadata.id}>
                  <td>{key.spec.display_name}</td>
                  <td>
                    <code>{key.status.key_id}</code>
                  </td>
                  <td>
                    <Time iso={key.metadata.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function Members({ orgId }: { orgId: string }) {
  const members = useMembers(orgId);
  const me = useMe();
  const toasts = useToasts();
  const [inviting, setInviting] = useState(false);
  const update = useApiMutation(
    async ({ member, role }: { member: Schemas["Member"]; role: Schemas["MemberRole"] }) =>
      unwrap(
        await api.PATCH("/v1/members/{member_id}", {
          params: {
            path: { member_id: member.metadata.id },
            header: ifMatch(member.metadata.version),
          },
          body: { role },
        }),
      ),
    [["org", orgId, "members"]],
  );
  const remove = useApiMutation(
    async (member: Schemas["Member"]) => {
      await api.DELETE("/v1/members/{member_id}", {
        params: {
          path: { member_id: member.metadata.id },
          header: ifMatch(member.metadata.version),
        },
      });
    },
    [["org", orgId, "members"]],
  );
  return (
    <Card
      title="Members"
      description="People sign in with your SSO. Invite them by the email address your identity provider verifies."
      actions={
        <button type="button" className="button primary small" onClick={() => setInviting(true)}>
          Invite
        </button>
      }
      flush
    >
      {members.isPending ? (
        <Loading />
      ) : members.isError ? (
        <ErrorState error={members.error} />
      ) : members.data.length === 0 ? (
        <Empty title="No members yet" />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Person</th>
                <th scope="col">State</th>
                <th scope="col">Role</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {members.data.map((m) => (
                <tr key={m.metadata.id}>
                  <td>
                    {m.email ?? <code>{m.subject}</code>}
                    {m.email && m.subject ? (
                      <div className="muted small mono">{m.subject}</div>
                    ) : null}
                    {me.data?.subject === m.subject ? (
                      <span className="muted small"> (you)</span>
                    ) : null}
                  </td>
                  <td>
                    <Status phase={m.state} />
                  </td>
                  <td>
                    <label className="visually-hidden" htmlFor={`role-${m.metadata.id}`}>
                      Role of {m.email ?? m.subject}
                    </label>
                    <select
                      id={`role-${m.metadata.id}`}
                      value={m.role}
                      style={{ width: "auto" }}
                      onChange={(e) =>
                        update.mutate(
                          { member: m, role: e.target.value as Schemas["MemberRole"] },
                          {
                            onSuccess: () => toasts.ok("Role changed."),
                            onError: (err) => toasts.error(err),
                          },
                        )
                      }
                    >
                      {ROLES.map((r) => (
                        <option key={r.value} value={r.value}>
                          {r.label}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <button
                      type="button"
                      className="button small ghost"
                      onClick={() =>
                        remove.mutate(m, {
                          onSuccess: () =>
                            toasts.ok(
                              m.state === "Invited" ? "Invitation withdrawn." : "Member removed.",
                            ),
                          onError: (err) => toasts.error(err),
                        })
                      }
                    >
                      {m.state === "Invited" ? "Withdraw" : "Remove"}
                      <span className="visually-hidden"> {m.email ?? m.subject}</span>
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <InviteMember open={inviting} onClose={() => setInviting(false)} orgId={orgId} />
    </Card>
  );
}

function InviteMember({
  open,
  onClose,
  orgId,
}: {
  open: boolean;
  onClose: () => void;
  orgId: string;
}) {
  const toasts = useToasts();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Schemas["MemberRole"]>("viewer");
  const invite = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/organizations/{organization_id}/members", {
          params: { path: { organization_id: orgId }, header: idempotent() },
          body: { email: email.trim(), role },
        }),
      ),
    [["org", orgId, "members"]],
  );
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Invite a member"
      description="The invitation becomes active the first time they sign in with this verified email."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          invite.mutate(undefined, {
            onSuccess: () => {
              toasts.ok(`Invited ${email.trim()}.`);
              setEmail("");
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          <Field id="invite-email" label="Email">
            <input
              id="invite-email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <fieldset className="field">
            <legend>Role</legend>
            <div className="checks">
              {ROLES.map((r) => (
                <label key={r.value} style={{ alignItems: "flex-start" }}>
                  <input
                    type="radio"
                    name="role"
                    checked={role === r.value}
                    onChange={() => setRole(r.value)}
                  />
                  <span>
                    <strong>{r.label}</strong>
                    <span className="muted small" style={{ display: "block" }}>
                      {r.text}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
          {invite.isError ? <ErrorState error={invite.error} title="Could not invite" /> : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!email.trim() || invite.isPending}
          >
            Send invitation
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function ApiTokens({ orgId }: { orgId: string }) {
  const tokens = useApiTokens(orgId);
  const projects = useProjects(orgId);
  const toasts = useToasts();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [scope, setScope] = useState<"read" | "deploy" | "admin">("read");
  const [days, setDays] = useState(30);
  const [projectId, setProjectId] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const create = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/organizations/{organization_id}/api-tokens", {
          params: { path: { organization_id: orgId }, header: idempotent() },
          body: {
            display_name: name.trim(),
            scopes:
              scope === "admin" ? ["admin"] : scope === "deploy" ? ["read", "deploy"] : ["read"],
            expires_in_days: days,
            ...(projectId ? { project_ids: [projectId] } : {}),
          },
        }),
      ),
    [["org", orgId, "api-tokens"]],
  );
  const revoke = useApiMutation(
    async (token: Schemas["ApiToken"]) => {
      await api.DELETE("/v1/api-tokens/{api_token_id}", {
        params: {
          path: { api_token_id: token.metadata.id },
          header: ifMatch(token.metadata.version),
        },
      });
    },
    [["org", orgId, "api-tokens"]],
  );
  return (
    <Card
      title="API tokens"
      description="For automation, CI and the SDK. A token's secret is shown once; store it in your secret manager."
      actions={
        <button
          type="button"
          className="button small"
          onClick={() => {
            setSecret(null);
            setCreating(true);
          }}
        >
          Create token
        </button>
      }
      flush
    >
      {tokens.isPending ? (
        <Loading />
      ) : tokens.isError ? (
        <ErrorState error={tokens.error} />
      ) : tokens.data.length === 0 ? (
        <Empty title="No API tokens" />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Token</th>
                <th scope="col">Scopes</th>
                <th scope="col">Expires</th>
                <th scope="col">Last used</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {tokens.data.map((t) => (
                <tr key={t.metadata.id}>
                  <td>
                    {t.display_name}
                    <div className="muted small mono">{t.metadata.id}</div>
                  </td>
                  <td>
                    {t.scopes.join(", ")}
                    {t.project_ids?.length ? (
                      <div className="muted small">{t.project_ids.length} project only</div>
                    ) : null}
                  </td>
                  <td className="small">
                    <Time iso={t.expires_at} />
                  </td>
                  <td className="small">
                    {t.last_used_at ? (
                      <Time iso={t.last_used_at} />
                    ) : (
                      <span className="muted">Never</span>
                    )}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="button small ghost"
                      onClick={() =>
                        revoke.mutate(t, {
                          onSuccess: () => toasts.ok("Token revoked."),
                          onError: (e) => toasts.error(e),
                        })
                      }
                    >
                      Revoke<span className="visually-hidden"> {t.display_name}</span>
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Dialog
        open={creating}
        onClose={() => setCreating(false)}
        title={secret ? "Token created" : "Create an API token"}
      >
        {secret ? (
          <>
            <div className="dialog-body">
              <Callout tone="warn" title="Copy it now">
                The secret is not shown again.
              </Callout>
              <CopyField value={secret} label="API token" />
            </div>
            <div className="dialog-footer form-actions">
              <button type="button" className="button primary" onClick={() => setCreating(false)}>
                Done
              </button>
            </div>
          </>
        ) : (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate(undefined, { onSuccess: (t) => setSecret(t.secret) });
            }}
          >
            <div className="dialog-body">
              <Field id="token-name" label="Name" hint="What uses it, for example ci-releases.">
                <input
                  id="token-name"
                  type="text"
                  required
                  maxLength={200}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </Field>
              <Field id="token-scope" label="Access">
                <select
                  id="token-scope"
                  value={scope}
                  onChange={(e) => setScope(e.target.value as typeof scope)}
                >
                  <option value="read">Read</option>
                  <option value="deploy">Read and deploy</option>
                  <option value="admin">Admin</option>
                </select>
              </Field>
              <Field id="token-project" label="Project">
                <select
                  id="token-project"
                  value={projectId}
                  onChange={(e) => setProjectId(e.target.value)}
                >
                  <option value="">All projects</option>
                  {(projects.data ?? []).map((p) => (
                    <option key={p.metadata.id} value={p.metadata.id}>
                      {p.spec.display_name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field id="token-days" label="Expires after (days)">
                <input
                  id="token-days"
                  type="number"
                  min={1}
                  max={365}
                  value={days}
                  onChange={(e) => setDays(Number(e.target.value))}
                />
              </Field>
              {create.isError ? (
                <ErrorState error={create.error} title="Could not create the token" />
              ) : null}
            </div>
            <div className="dialog-footer form-actions">
              <button type="button" className="button" onClick={() => setCreating(false)}>
                Cancel
              </button>
              <button
                type="submit"
                className="button primary"
                disabled={!name.trim() || create.isPending}
              >
                Create
              </button>
            </div>
          </form>
        )}
      </Dialog>
    </Card>
  );
}

function SupportAccess({ orgId }: { orgId: string }) {
  const grants = useSupportGrants(orgId);
  const projects = useProjects(orgId);
  // Support grants are per site; sites belong to projects: list every project's sites.
  const siteQueries = useQueries({
    queries: (projects.data ?? []).map((project) => ({
      queryKey: ["p", project.metadata.id, "sites"],
      queryFn: async () =>
        unwrap(
          await api.GET("/v1/projects/{project_id}/sites", {
            params: { path: { project_id: project.metadata.id }, query: { page_size: 200 } },
          }),
        ).items,
    })),
  });
  const sites = { data: siteQueries.flatMap((q) => q.data ?? []) };
  const toasts = useToasts();
  const [open, setOpen] = useState(false);
  const [siteId, setSiteId] = useState("");
  const [minutes, setMinutes] = useState(60);
  const [justification, setJustification] = useState("");
  const [operations, setOperations] = useState<string[]>(["read_status"]);
  const site = siteId || sites.data[0]?.metadata.id || "";
  const create = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/organizations/{organization_id}/support-grants", {
          params: { path: { organization_id: orgId }, header: idempotent() },
          body: {
            site_id: site,
            operations: operations as Schemas["SupportGrantCreate"]["operations"],
            duration_minutes: minutes,
            justification,
          },
        }),
      ),
    [["org", orgId, "support-grants"]],
  );
  const revoke = useApiMutation(
    async (grant: Schemas["SupportGrant"]) => {
      await api.DELETE("/v1/support-grants/{support_grant_id}", {
        params: {
          path: { support_grant_id: grant.metadata.id },
          header: ifMatch(grant.metadata.version),
        },
      });
    },
    [["org", orgId, "support-grants"]],
  );
  const ops = ["read_status", "collect_diagnostics", "prepare_revision", "rollback"];
  return (
    <Card
      title="Support access"
      description="Neura support has no standing access. Grant it for one site, for a limited time and a limited set of operations; every use is audited."
      actions={
        <button type="button" className="button small" onClick={() => setOpen(true)}>
          Grant access
        </button>
      }
      flush
    >
      {(grants.data ?? []).length === 0 ? (
        <p className="card-body muted">No support access granted.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Site</th>
                <th scope="col">Operations</th>
                <th scope="col">State</th>
                <th scope="col">Expires</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {(grants.data ?? []).map((g) => (
                <tr key={g.metadata.id}>
                  <td>
                    <code>{g.site_id}</code>
                    <div className="muted small">{g.justification}</div>
                  </td>
                  <td>{g.operations.join(", ").replaceAll("_", " ")}</td>
                  <td>
                    <Status phase={g.state} />
                  </td>
                  <td className="small">
                    <Time iso={g.expires_at} />
                  </td>
                  <td>
                    {g.state === "Active" ? (
                      <button
                        type="button"
                        className="button small ghost"
                        onClick={() =>
                          revoke.mutate(g, {
                            onSuccess: () => toasts.ok("Access revoked."),
                            onError: (e) => toasts.error(e),
                          })
                        }
                      >
                        Revoke
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Dialog open={open} onClose={() => setOpen(false)} title="Grant support access">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate(undefined, {
              onSuccess: () => {
                toasts.ok("Support access granted.");
                setOpen(false);
              },
            });
          }}
        >
          <div className="dialog-body">
            <Field id="grant-site" label="Site">
              <select id="grant-site" value={site} onChange={(e) => setSiteId(e.target.value)}>
                {sites.data.map((s) => (
                  <option key={s.metadata.id} value={s.metadata.id}>
                    {s.spec.display_name}
                  </option>
                ))}
              </select>
            </Field>
            <fieldset className="field">
              <legend>Operations</legend>
              <div className="checks">
                {ops.map((op) => (
                  <label key={op}>
                    <input
                      type="checkbox"
                      checked={operations.includes(op)}
                      onChange={(e) =>
                        setOperations(
                          e.target.checked
                            ? [...operations, op]
                            : operations.filter((o) => o !== op),
                        )
                      }
                    />
                    {op.replaceAll("_", " ")}
                  </label>
                ))}
              </div>
            </fieldset>
            <Field id="grant-minutes" label="Duration (minutes)">
              <input
                id="grant-minutes"
                type="number"
                min={15}
                max={1440}
                value={minutes}
                onChange={(e) => setMinutes(Number(e.target.value))}
              />
            </Field>
            <Field
              id="grant-why"
              label="Why"
              hint="At least 10 characters; recorded in the audit log."
            >
              <input
                id="grant-why"
                type="text"
                required
                minLength={10}
                maxLength={2000}
                value={justification}
                onChange={(e) => setJustification(e.target.value)}
              />
            </Field>
            {create.isError ? (
              <ErrorState error={create.error} title="Could not grant access" />
            ) : null}
          </div>
          <div className="dialog-footer form-actions">
            <button type="button" className="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <button
              type="submit"
              className="button primary"
              disabled={
                !site || operations.length === 0 || justification.length < 10 || create.isPending
              }
            >
              Grant
            </button>
          </div>
        </form>
      </Dialog>
    </Card>
  );
}

function SignIn() {
  const { meta } = useAuth();
  const oidc = meta?.authentication.oidc;
  return (
    <Card title="Sign-in" description="Configured on the control plane.">
      <KeyValue
        items={[
          [
            "Single sign-on",
            oidc ? <Badge tone="ok">OIDC</Badge> : <Badge tone="muted">Not configured</Badge>,
          ],
          ["Issuer", oidc?.issuer ?? "—"],
          ["Console client", oidc?.client_id ?? "—"],
          ["API tokens", meta?.authentication.api_tokens ? "Accepted" : "Not accepted"],
        ]}
      />
    </Card>
  );
}
