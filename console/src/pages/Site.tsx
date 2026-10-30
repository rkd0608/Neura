// A site: its connection, offline allowance, servers with GPUs and preflight, and connecting a
// new server (design §17.2, §19.3).
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { api, ifMatch, unwrap, type Schemas, idempotent, specVersion } from "../api/client";
import { useApiMutation, useRobots, useServers, useSite } from "../api/queries";
import { Dialog } from "../components/Dialog";
import { BotIcon, BuildingIcon, ServerIcon } from "../components/Icons";
import { useToasts } from "../components/Toasts";
import { Field } from "../components/Field";
import {
  Badge,
  Callout,
  Card,
  CopyField,
  DataAge,
  Empty,
  ErrorState,
  KeyValue,
  Loading,
  PageHeader,
  Reason,
  Stat,
  Status,
  TabPanel,
  Tabs,
  Time,
  useTab,
  type TabSpec,
} from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { duration } from "../lib/format";
import { useNow } from "../lib/useNow";

type Site = Schemas["Site"];
type Server = Schemas["Server"];

export function SitePage() {
  const { orgId, projectId, base } = useScope();
  const { siteId = "" } = useParams();
  const [search] = useSearchParams();
  const site = useSite(projectId, siteId);
  const [connecting, setConnecting] = useState(search.has("connect"));
  const servers = useServers(projectId, siteId, connecting);
  const robots = useRobots(projectId);
  const can = usePermissions(orgId, projectId);
  const [editing, setEditing] = useState(false);
  const tabs: TabSpec[] = [
    { id: "overview", label: "Overview" },
    { id: "servers", label: "Servers", count: servers.data?.length ?? null },
    {
      id: "robots",
      label: "Robots",
      count: (robots.data ?? []).filter((r) => r.spec.site_id === siteId).length,
    },
  ];
  const [tab, setTab] = useTab(tabs, search.has("connect") ? "servers" : "overview");

  if (site.isPending) return <Loading />;
  if (site.isError) return <ErrorState error={site.error} />;
  const s = site.data;
  const stale = !s.status.connected && s.status.observed_at !== null;
  const atSite = (robots.data ?? []).filter((r) => r.spec.site_id === siteId);

  return (
    <>
      <PageHeader
        breadcrumbs={<Link to={`${base}/sites`}>Sites & servers</Link>}
        icon={<BuildingIcon />}
        title={s.spec.display_name}
        meta={
          <>
            <Status phase={s.status.phase} />
            <DataAge
              ageSeconds={s.status.data_age_seconds}
              fetchedAt={site.dataUpdatedAt}
              never="The site has never connected"
            />
            <span>agent {s.status.agent_version ?? "not enrolled"}</span>
            <span className="tag">{s.metadata.id}</span>
          </>
        }
        actions={
          <>
            {can.admin ? (
              <button type="button" className="button" onClick={() => setEditing(true)}>
                Edit site
              </button>
            ) : null}
            {can.admin ? (
              <button type="button" className="button primary" onClick={() => setConnecting(true)}>
                <ServerIcon /> Connect a server
              </button>
            ) : null}
          </>
        }
      />
      {stale ? (
        <Callout tone="warn" title="The site is not connected">
          What you see is its last report. The site keeps operating on its committed configuration
          until its offline allowance runs out.
        </Callout>
      ) : null}
      {connecting ? (
        <ConnectServer site={s} servers={servers.data ?? []} onDone={() => setConnecting(false)} />
      ) : null}
      <Tabs label="Site" tabs={tabs} value={tab} onChange={setTab} />
      {tab === "overview" ? (
        <TabPanel label="Overview">
          <div className="grid cols-4">
            <Stat label="GPU servers" value={servers.data?.length ?? "—"} />
            <Stat
              label="GPUs for Neura"
              value={(servers.data ?? []).reduce(
                (n, x) => n + x.spec.allocated_gpu_indexes.length,
                0,
              )}
              note={`of ${(servers.data ?? []).reduce((n, x) => n + (x.status.gpus?.length ?? 0), 0)} reported`}
            />
            <Stat
              label="Robots running"
              value={atSite.filter((r) => r.status.phase === "Running").length}
              note={`of ${atSite.length} at this site`}
            />
            <Stat
              label="Offline allowance"
              value={`${s.spec.offline_authorization_hours} h`}
              note="without reaching Neura"
            />
          </div>
          <div className="grid cols-2">
            <Card title="Connection">
              <KeyValue
                items={[
                  ["Agent connected", s.status.connected ? "Yes" : "No"],
                  ["Agent version", s.status.agent_version ?? "—"],
                  [
                    "Offline allowance",
                    <OfflineAllowance key="offline" site={s} fetchedAt={site.dataUpdatedAt} />,
                  ],
                  ["Identifier", <code key="id">{s.metadata.id}</code>],
                ]}
              />
            </Card>
            <RobotsHere robots={atSite} stale={stale} base={base} />
          </div>
        </TabPanel>
      ) : null}
      {tab === "servers" ? (
        <TabPanel label="Servers">
          <Card
            title="GPU servers"
            description="Servers connected to this site, as their agents report them."
          >
            {servers.isPending ? (
              <Loading />
            ) : servers.isError ? (
              <ErrorState error={servers.error} />
            ) : servers.data.length === 0 ? (
              <Empty
                title="No server connected yet"
                icon={<ServerIcon />}
                actions={
                  can.admin ? (
                    <button
                      type="button"
                      className="button primary small"
                      onClick={() => setConnecting(true)}
                    >
                      Connect a server
                    </button>
                  ) : null
                }
              >
                <p>Connect the GPU server at this site with one command.</p>
              </Empty>
            ) : (
              <div className="stack" style={{ gap: 28 }}>
                {servers.data.map((server) => (
                  <ServerDetail
                    key={server.metadata.id}
                    server={server}
                    fetchedAt={servers.dataUpdatedAt}
                    canEdit={can.admin}
                    projectId={projectId}
                    siteId={siteId}
                  />
                ))}
              </div>
            )}
          </Card>
        </TabPanel>
      ) : null}
      {tab === "robots" ? (
        <TabPanel label="Robots">
          <RobotsHere robots={atSite} stale={stale} base={base} />
        </TabPanel>
      ) : null}
      <EditSite open={editing} onClose={() => setEditing(false)} site={s} projectId={projectId} />
    </>
  );
}

function RobotsHere({
  robots,
  stale,
  base,
}: {
  robots: Schemas["Robot"][];
  stale: boolean;
  base: string;
}) {
  return (
    <Card
      title="Robots at this site"
      flush
      actions={<Link to={`${base}/robots`}>Manage robots</Link>}
    >
      {robots.length === 0 ? (
        <Empty title="No robots at this site yet" icon={<BotIcon />} />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Robot</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {robots.map((r) => (
                <tr key={r.metadata.id}>
                  <td>
                    <Link to={`${base}/robots/${r.metadata.id}`}>{r.spec.display_name}</Link>
                  </td>
                  <td>
                    <Status phase={r.status.phase} stale={stale} />
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

function OfflineAllowance({ site, fetchedAt }: { site: Site; fetchedAt: number }) {
  const now = useNow(1000);
  const { offline_authorization_expires_at: expires, observed_at: observed } = site.status;
  if (!expires || !observed || site.status.data_age_seconds == null) {
    return <span>{site.spec.offline_authorization_hours} h, starting from its next contact</span>;
  }
  // Both timestamps come from the control plane's clock: their difference is a duration.
  const atReport = (new Date(expires).getTime() - new Date(observed).getTime()) / 1000;
  const left = atReport - site.status.data_age_seconds - Math.max(0, now - fetchedAt) / 1000;
  if (left <= 0) return <Badge tone="danger">Expired: the site has stopped serving</Badge>;
  return (
    <span>
      {duration(left)} left{" "}
      <span className="muted">of {site.spec.offline_authorization_hours} h</span>
    </span>
  );
}

function ConnectServer({
  site,
  servers,
  onDone,
}: {
  site: Site;
  servers: Server[];
  onDone: () => void;
}) {
  const [known] = useState(() => new Set(servers.map((s) => s.metadata.id)));
  const create = useApiMutation(async () =>
    unwrap(
      await api.POST("/v1/sites/{site_id}/enrollment-tokens", {
        params: { path: { site_id: site.metadata.id }, header: idempotent() },
        body: { ttl_minutes: 60 },
      }),
    ),
  );
  const fresh = servers.filter((s) => !known.has(s.metadata.id));
  const token = create.data;

  return (
    <Card
      title="Connect a GPU server"
      description="The agent dials out to Neura over TLS. No inbound ports, no VPN. Its key is generated on the server and never leaves it."
      actions={
        <button type="button" className="button small ghost" onClick={onDone}>
          Close
        </button>
      }
    >
      <ol className="stack" style={{ paddingLeft: 18, margin: 0, gap: 16 }}>
        <li>
          <strong>Generate a one-time install command</strong>
          <p className="secondary small">
            It works once, for this site only, and expires in an hour.
          </p>
          <div style={{ marginTop: 8 }}>
            {token ? (
              <div className="stack">
                <CopyField value={token.install_command} label="Install command" />
                <span className="muted small">
                  Expires <Time iso={token.expires_at} />. It is shown only now.
                </span>
              </div>
            ) : (
              <button
                type="button"
                className="button primary"
                disabled={create.isPending}
                onClick={() => create.mutate(undefined)}
              >
                {create.isPending ? "Generating…" : "Generate install command"}
              </button>
            )}
            {create.isError ? <ErrorState error={create.error} /> : null}
          </div>
        </li>
        <li>
          <strong>Run it on the server</strong>
          <p className="secondary small">
            The agent checks the server first (driver, GPUs, disk) and reports what it finds here.
          </p>
        </li>
        <li aria-live="polite">
          <strong>Check the server</strong>
          {fresh.length === 0 ? (
            <p className="secondary small">{token ? "Waiting for the server to enroll…" : "—"}</p>
          ) : (
            fresh.map((server) => (
              <div key={server.metadata.id} className="stack" style={{ marginTop: 8 }}>
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
            ))
          )}
        </li>
      </ol>
    </Card>
  );
}

function Preflight({ checks }: { checks: Schemas["PreflightCheck"][] }) {
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

function ServerDetail({
  server,
  fetchedAt,
  canEdit,
  projectId,
  siteId,
}: {
  server: Server;
  fetchedAt: number;
  canEdit: boolean;
  projectId: string;
  siteId: string;
}) {
  const toasts = useToasts();
  const gpus = server.status.gpus ?? [];
  const [allocated, setAllocated] = useState<number[]>(server.spec.allocated_gpu_indexes);
  const dirty =
    allocated.length !== server.spec.allocated_gpu_indexes.length ||
    allocated.some((i) => !server.spec.allocated_gpu_indexes.includes(i));
  const save = useApiMutation(async () => {
    const path = { server_id: server.metadata.id };
    const latest = unwrap(await api.GET("/v1/servers/{server_id}", { params: { path } }));
    const version = await specVersion(latest, server.spec);
    return unwrap(
      await api.PATCH("/v1/servers/{server_id}", {
        params: { path, header: ifMatch(version) },
        body: { spec: { allocated_gpu_indexes: [...allocated].sort((a, b) => a - b) } },
      }),
    );
  }, [["p", projectId, "servers", siteId]]);
  return (
    <section className="stack" aria-label={`Server ${server.spec.display_name}`}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h3>{server.status.hostname || server.spec.display_name}</h3>
          <span className="muted small">
            Driver {server.status.driver_version ?? "—"} · CUDA {server.status.cuda_version ?? "—"}{" "}
            · <DataAge ageSeconds={server.status.data_age_seconds} fetchedAt={fetchedAt} />
          </span>
        </div>
      </div>
      {gpus.length === 0 ? (
        <p className="muted">The agent reports no GPUs on this server.</p>
      ) : (
        <fieldset>
          <legend className="small secondary" style={{ marginBottom: 6 }}>
            GPUs Neura may use. Others stay untouched, so Neura can share the server with your own
            jobs.
          </legend>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Use</th>
                  <th scope="col">GPU</th>
                  <th scope="col" className="num">
                    Memory
                  </th>
                  <th scope="col">MIG</th>
                </tr>
              </thead>
              <tbody>
                {gpus.map((gpu) => (
                  <tr key={gpu.index}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Allow Neura to use GPU ${gpu.index}`}
                        checked={allocated.includes(gpu.index)}
                        disabled={!canEdit}
                        onChange={(event) =>
                          setAllocated((current) =>
                            event.target.checked
                              ? [...current, gpu.index]
                              : current.filter((i) => i !== gpu.index),
                          )
                        }
                      />
                    </td>
                    <td>
                      {gpu.index}: {gpu.model}
                    </td>
                    <td className="num">{(gpu.memory_mib / 1024).toFixed(0)} GiB</td>
                    <td>{gpu.mig_enabled ? "Enabled" : "Off"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {canEdit && dirty ? (
            <div className="form-actions" style={{ marginTop: 8 }}>
              <button
                type="button"
                className="button primary small"
                disabled={save.isPending}
                onClick={() =>
                  save.mutate(undefined, {
                    onSuccess: () => toasts.ok("GPU allocation saved."),
                    onError: (e) => toasts.error(e),
                  })
                }
              >
                Save GPU allocation
              </button>
            </div>
          ) : null}
        </fieldset>
      )}
      <Preflight checks={server.status.preflight ?? []} />
    </section>
  );
}

function EditSite({
  open,
  onClose,
  site,
  projectId,
}: {
  open: boolean;
  onClose: () => void;
  site: Site;
  projectId: string;
}) {
  const toasts = useToasts();
  const [name, setName] = useState(site.spec.display_name);
  const [hours, setHours] = useState(site.spec.offline_authorization_hours);
  const save = useApiMutation(async () => {
    const path = { site_id: site.metadata.id };
    const latest = unwrap(await api.GET("/v1/sites/{site_id}", { params: { path } }));
    const version = await specVersion(latest, site.spec);
    return unwrap(
      await api.PATCH("/v1/sites/{site_id}", {
        params: { path, header: ifMatch(version) },
        body: { spec: { display_name: name, offline_authorization_hours: hours } },
      }),
    );
  }, [["p", projectId]]);
  return (
    <Dialog open={open} onClose={onClose} title="Edit site">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate(undefined, {
            onSuccess: () => {
              toasts.ok("Site saved. The site picks up the change from its next desired state.");
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          <Field id="edit-site-name" label="Site name">
            <input
              type="text"
              required
              maxLength={200}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field id="edit-site-hours" label="Offline allowance (hours)">
            <input
              type="number"
              min={1}
              max={8760}
              value={hours}
              onChange={(event) => setHours(Number(event.target.value))}
            />
          </Field>
          {save.isError ? <ErrorState error={save.error} title="Could not save" /> : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={save.isPending}>
            Save
          </button>
        </div>
      </form>
    </Dialog>
  );
}
