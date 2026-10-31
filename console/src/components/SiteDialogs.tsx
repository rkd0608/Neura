// Making a site and connecting its servers (design §17.2, §29.7): one dialog for each, opened from
// Fleet, Sites and a site's page, with one set of defaults.
import { useId, useState } from "react";

import { api, idempotent, unwrap, type Schemas } from "../api/client";
import { useApiMutation, useServers } from "../api/queries";
import { useScope } from "../lib/context";
import { slug } from "../lib/format";
import { Dialog } from "./Dialog";
import { Field } from "./Field";
import { Badge, Callout, CopyField, ErrorState, Loading, Reason, Time } from "./ui";

type Site = Schemas["Site"];
type Server = Schemas["Server"];

/** How long a new site keeps running on its configuration without Neura, unless its admin says
 *  otherwise: the default of `neura site create --offline-hours`. */
const OFFLINE_HOURS = 72;

export function NewSite({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated?: (site: Site) => void;
}) {
  return (
    <Dialog open={open} title="Create a site" onClose={onClose}>
      <SiteForm onClose={onClose} onCreated={onCreated} />
    </Dialog>
  );
}

/** The form, mounted only while its dialog is open: each opening starts empty. */
function SiteForm({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated?: (site: Site) => void;
}) {
  const { projectId } = useScope();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"bench" | "production">("production");
  const [hours, setHours] = useState(OFFLINE_HOURS);
  const nameId = useId();
  const hoursId = useId();
  const create = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/projects/{project_id}/sites", {
          params: { path: { project_id: projectId }, header: idempotent() },
          body: {
            spec: {
              name: slug(name) || "site",
              display_name: name.trim(),
              offline_authorization_hours: hours,
              kind,
            },
          },
        }),
      ),
    [["p", projectId, "sites"]],
  );
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate(undefined, {
          onSuccess: (site) => {
            onClose();
            onCreated?.(site);
          },
        });
      }}
    >
      <div className="dialog-body">
        <Field
          id={nameId}
          label="Name"
          hint={
            name
              ? `Its identifier: ${slug(name) || "site"}`
              : "For example, the warehouse or cell name."
          }
        >
          <input
            type="text"
            required
            maxLength={200}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <fieldset className="stack" style={{ gap: 6 }}>
          <legend>Kind</legend>
          <label className="row">
            <input
              type="radio"
              name="kind"
              checked={kind === "production"}
              onChange={() => setKind("production")}
            />
            Production: a person at the site approves every change
          </label>
          <label className="row">
            <input
              type="radio"
              name="kind"
              checked={kind === "bench"}
              onChange={() => setKind("bench")}
            />
            Bench: it approves its own changes, for trying things
          </label>
        </fieldset>
        <Field
          id={hoursId}
          label="Offline allowance (hours)"
          hint="How long the site keeps running on its committed configuration without reaching Neura. After that it stops serving until it reconnects."
        >
          <input
            type="number"
            min={1}
            max={8760}
            required
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
          />
        </Field>
        {create.isError ? (
          <ErrorState error={create.error} title="Could not create the site" />
        ) : null}
      </div>
      <div className="dialog-footer form-actions">
        <button type="button" className="button" onClick={onClose}>
          Cancel
        </button>
        <button
          type="submit"
          className="button primary"
          disabled={!name.trim() || create.isPending}
        >
          {create.isPending ? "Creating…" : "Create site"}
        </button>
      </div>
    </form>
  );
}

/** The single-use commands that make a computer a server of the site, and the servers that
 *  enroll with them, with their preflight checks. */
export function ConnectServer({
  site,
  open,
  onClose,
}: {
  site: Site;
  open: boolean;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={open}
      title={`Add a server to ${site.spec.name}`}
      description="Each command works once, for this site only, and expires in an hour."
      onClose={onClose}
    >
      <Enrollment site={site} onClose={onClose} />
    </Dialog>
  );
}

/** The dialog's body, mounted only while it is open: closing it forgets the commands, so opening
 *  it again starts from Generate. It follows the site's servers while open. */
function Enrollment({ site, onClose }: { site: Site; onClose: () => void }) {
  const { projectId } = useScope();
  const servers = useServers(projectId, site.metadata.id, true);
  if (servers.isPending) return <Loading />;
  if (servers.isError) return <ErrorState error={servers.error} />;
  return <Commands site={site} servers={servers.data} onClose={onClose} />;
}

function Commands({
  site,
  servers,
  onClose,
}: {
  site: Site;
  servers: Server[];
  onClose: () => void;
}) {
  // The servers there were when the dialog opened: any other one enrolled with these commands.
  const [known] = useState(() => new Set(servers.map((s) => s.metadata.id)));
  const issue = useApiMutation(async () =>
    unwrap(
      await api.POST("/v1/sites/{site_id}/enrollment-tokens", {
        params: { path: { site_id: site.metadata.id }, header: idempotent() },
        body: { ttl_minutes: 60 },
      }),
    ),
  );
  const token = issue.data;
  const fresh = servers.filter((s) => !known.has(s.metadata.id));
  return (
    <>
      <div className="dialog-body">
        {token ? (
          <>
            {token.join_command ? (
              <Command label="A workstation or bench" value={token.join_command} />
            ) : null}
            <Command label="A production server" value={token.install_command} />
            <p className="muted small">
              Expires <Time iso={token.expires_at} />. It is shown only now.
            </p>
          </>
        ) : (
          <button
            type="button"
            className="button primary"
            disabled={issue.isPending}
            onClick={() => issue.mutate(undefined)}
          >
            {issue.isPending ? "Generating…" : "Generate the commands"}
          </button>
        )}
        {issue.isError ? <ErrorState error={issue.error} /> : null}
        <p className="muted small">
          The agent checks the server first (driver, GPUs, disk) and reports what it finds here. It
          dials out to Neura: no inbound ports. Its key is made on the server and never leaves it.
        </p>
        <div aria-live="polite" className="stack">
          {token && fresh.length === 0 ? (
            <p className="secondary small">Waiting for the server to enroll…</p>
          ) : null}
          {fresh.map((server) => (
            <div key={server.metadata.id} className="stack">
              <Callout
                tone="ok"
                title={`${server.status.hostname || server.spec.display_name} enrolled`}
              >
                {(server.status.preflight ?? []).every((c) => c.passed)
                  ? "Every preflight check passed."
                  : "Some preflight checks failed; see the fixes below."}
              </Callout>
              <Preflight checks={server.status.preflight ?? []} />
            </div>
          ))}
        </div>
      </div>
      <div className="dialog-footer form-actions">
        <button type="button" className="button" onClick={onClose}>
          Done
        </button>
      </div>
    </>
  );
}

/** A command, with where it is run shown above it in the words its copy button says. */
function Command({ label, value }: { label: string; value: string }) {
  return (
    <div className="stack" style={{ gap: 6 }}>
      <span className="small secondary">{label}</span>
      <CopyField value={value} label={label} />
    </div>
  );
}

export function Preflight({ checks }: { checks: Schemas["PreflightCheck"][] }) {
  if (checks.length === 0) return <p className="muted">No preflight results reported.</p>;
  return (
    <div className="table-wrap">
      <table>
        <caption className="visually-hidden">Preflight checks</caption>
        <thead>
          <tr>
            <th scope="col">Check</th>
            <th scope="col">Result</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          {checks.map((check) => (
            <tr key={check.name}>
              <td>
                <code>{check.name}</code>
              </td>
              <td>
                {check.passed ? (
                  <Badge tone="ok">Passed</Badge>
                ) : (
                  <Badge tone="danger">Failed</Badge>
                )}
              </td>
              <td>
                <div>{check.detail}</div>
                {!check.passed && check.fix ? (
                  <div className="small" style={{ marginTop: 4 }}>
                    <strong>Fix:</strong> {check.fix}
                  </div>
                ) : null}
                {!check.passed && check.reason ? <Reason code={check.reason} /> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
