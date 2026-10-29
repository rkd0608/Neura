// Robots: the project's robots, and one robot's page with its sessions, faults and what it runs
// (design §19.3).
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, ifMatch, unwrap, type Schemas, idempotent } from "../api/client";
import {
  useApiMutation,
  useDeployments,
  useFaults,
  useRobot,
  useRobotProfiles,
  useRobots,
  useSessions,
  useSites,
} from "../api/queries";
import { Dialog } from "../components/Dialog";
import { BotIcon, PlusIcon } from "../components/Icons";
import { FaultTable } from "../components/Tables";
import { useToasts } from "../components/Toasts";
import { Field } from "../components/Field";
import {
  Card,
  DataAge,
  Empty,
  ErrorState,
  KeyValue,
  Loading,
  PageHeader,
  Reason,
  SearchInput,
  Stat,
  Status,
  TabPanel,
  Tabs,
  useTab,
  type TabSpec,
} from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { shortId, slug } from "../lib/format";
import { label, tone } from "../lib/phases";

function profileName(profile: Schemas["RobotProfile"] | undefined): string {
  const meta = profile?.document.metadata as { name?: string; version?: string } | undefined;
  return meta?.name ? `${meta.name}${meta.version ? ` ${meta.version}` : ""}` : "—";
}

export function Robots() {
  const { orgId, projectId, base } = useScope();
  const robots = useRobots(projectId);
  const sites = useSites(projectId);
  const profiles = useRobotProfiles(projectId);
  const sessions = useSessions(projectId);
  const can = usePermissions(orgId, projectId);
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [siteFilter, setSiteFilter] = useState("all");
  const siteName = (id: string) =>
    sites.data?.find((s) => s.metadata.id === id)?.spec.display_name ?? shortId(id);
  const connected = (id: string) => sites.data?.find((s) => s.metadata.id === id)?.status.connected;
  const list = (robots.data ?? []).filter((r) => {
    if (siteFilter !== "all" && r.spec.site_id !== siteFilter) return false;
    if (!query) return true;
    return `${r.spec.display_name} ${r.spec.name} ${r.status.phase} ${siteName(r.spec.site_id)}`
      .toLowerCase()
      .includes(query.toLowerCase());
  });
  const phases = (robots.data ?? []).reduce<Record<string, number>>((acc, r) => {
    const key = r.status.phase;
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <>
      <PageHeader
        title="Robots"
        icon={<BotIcon />}
        description="Each robot belongs to a site and has a robot profile. States are what the site last observed; an offline site's robots show their last known state, greyed."
        actions={
          can.admin ? (
            <button type="button" className="button primary" onClick={() => setOpen(true)}>
              <PlusIcon /> Add robots
            </button>
          ) : null
        }
      />
      {robots.data && robots.data.length > 0 ? (
        <div className="row" aria-label="Robots by state" role="group">
          {Object.entries(phases).map(([phase, n]) => (
            <span key={phase} className={`badge ${tone(phase)}`}>
              {n} {label(phase).toLowerCase()}
            </span>
          ))}
        </div>
      ) : null}
      <div className="toolbar">
        <SearchInput
          value={query}
          onChange={setQuery}
          placeholder="Filter by name, state or site"
          label="Filter robots"
        />
        <label className="visually-hidden" htmlFor="robot-site-filter">
          Site
        </label>
        <select
          id="robot-site-filter"
          value={siteFilter}
          onChange={(e) => setSiteFilter(e.target.value)}
          style={{ width: "auto", minWidth: 160 }}
        >
          <option value="all">All sites</option>
          {(sites.data ?? []).map((s) => (
            <option key={s.metadata.id} value={s.metadata.id}>
              {s.spec.display_name}
            </option>
          ))}
        </select>
        <span className="grow" />
        <span className="muted small">
          {list.length} of {robots.data?.length ?? 0}
        </span>
      </div>
      <Card flush>
        {robots.isPending ? (
          <Loading />
        ) : robots.isError ? (
          <ErrorState error={robots.error} />
        ) : robots.data.length === 0 ? (
          <Empty
            title="No robots yet"
            icon={<BotIcon />}
            actions={
              can.admin ? (
                <button
                  type="button"
                  className="button primary small"
                  onClick={() => setOpen(true)}
                >
                  Add robots
                </button>
              ) : null
            }
          >
            <p>Register a robot profile under Models & contracts first, then add your robots.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Robots</caption>
              <thead>
                <tr>
                  <th scope="col">Robot</th>
                  <th scope="col">State</th>
                  <th scope="col">Site</th>
                  <th scope="col">Active revision</th>
                  <th scope="col" className="num">
                    Buffer
                  </th>
                  <th scope="col">Last report</th>
                </tr>
              </thead>
              <tbody>
                {list.map((robot) => {
                  const stale = connected(robot.spec.site_id) === false;
                  const session = sessions.data?.find(
                    (x) => x.robot_id === robot.metadata.id && x.state !== "Disconnected",
                  );
                  const to = `${base}/robots/${robot.metadata.id}`;
                  return (
                    <tr
                      key={robot.metadata.id}
                      className="clickable"
                      onClick={(event) => {
                        if ((event.target as HTMLElement).closest("a")) return;
                        void navigate(to);
                      }}
                    >
                      <td>
                        <Link to={to}>{robot.spec.display_name}</Link>
                        <span className="sub mono">{robot.spec.name}</span>
                      </td>
                      <td>
                        <Status phase={robot.status.phase} stale={stale} />
                        {robot.status.reason ? (
                          <div className="small">
                            <Reason code={robot.status.reason} />
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <Link to={`${base}/sites/${robot.spec.site_id}`}>
                          {siteName(robot.spec.site_id)}
                        </Link>
                      </td>
                      <td>
                        {robot.status.active_revision_id ? (
                          <span className="tag">{shortId(robot.status.active_revision_id)}</span>
                        ) : (
                          <span className="muted">None</span>
                        )}
                      </td>
                      <td className="num">
                        {session?.metrics?.buffer_actions ?? <span className="muted">—</span>}
                      </td>
                      <td>
                        <DataAge
                          ageSeconds={robot.status.data_age_seconds}
                          fetchedAt={robots.dataUpdatedAt}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <AddRobots
        open={open}
        onClose={() => setOpen(false)}
        projectId={projectId}
        sites={sites.data ?? []}
        profiles={profiles.data ?? []}
        base={base}
      />
    </>
  );
}

function AddRobots({
  open,
  onClose,
  projectId,
  sites,
  profiles,
  base,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  sites: Schemas["Site"][];
  profiles: Schemas["RobotProfile"][];
  base: string;
}) {
  const toasts = useToasts();
  const [name, setName] = useState("");
  const [count, setCount] = useState(1);
  const [siteId, setSiteId] = useState("");
  const [profileId, setProfileId] = useState("");
  const site = siteId || sites[0]?.metadata.id || "";
  const profile = profileId || profiles[0]?.metadata.id || "";
  const create = useApiMutation(async () => {
    const base = slug(name) || "robot";
    const created = [];
    for (let i = 0; i < count; i++) {
      const suffix = count > 1 ? `-${i}` : "";
      created.push(
        unwrap(
          await api.POST("/v1/projects/{project_id}/robots", {
            params: { path: { project_id: projectId }, header: idempotent() },
            body: {
              spec: {
                name: `${base}${suffix}`,
                display_name: count > 1 ? `${name.trim()} ${i}` : name.trim(),
                site_id: site,
                robot_profile_id: profile,
              },
            },
          }),
        ),
      );
    }
    return created;
  }, [["p", projectId]]);
  const missing =
    sites.length === 0
      ? "Add a site first."
      : profiles.length === 0
        ? "Register a robot profile under Models first."
        : null;
  return (
    <Dialog open={open} onClose={onClose} title="Add robots">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(undefined, {
            onSuccess: (robots) => {
              toasts.ok(`Added ${robots.length === 1 ? "1 robot" : `${robots.length} robots`}.`);
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          {missing ? (
            <p className="callout warn">
              {missing}{" "}
              <Link to={sites.length === 0 ? `${base}/sites` : `${base}/models`}>Go there</Link>
            </p>
          ) : null}
          <Field
            id="robot-name"
            label="Name"
            hint="Robots at a site authenticate to the gateway with this name."
          >
            <input
              type="text"
              required
              maxLength={190}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field
            id="robot-count"
            label="How many"
            hint={count > 1 ? `Named ${slug(name) || "robot"}-0 … -${count - 1}` : undefined}
          >
            <input
              type="number"
              min={1}
              max={64}
              value={count}
              onChange={(event) => setCount(Math.max(1, Math.min(64, Number(event.target.value))))}
            />
          </Field>
          <Field id="robot-site" label="Site">
            <select value={site} onChange={(event) => setSiteId(event.target.value)}>
              {sites.map((s) => (
                <option key={s.metadata.id} value={s.metadata.id}>
                  {s.spec.display_name}
                </option>
              ))}
            </select>
          </Field>
          <Field id="robot-profile" label="Robot profile">
            <select value={profile} onChange={(event) => setProfileId(event.target.value)}>
              {profiles.map((p) => (
                <option key={p.metadata.id} value={p.metadata.id}>
                  {profileName(p)}
                </option>
              ))}
            </select>
          </Field>
          {create.isError ? (
            <ErrorState error={create.error} title="Could not add the robots" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!name.trim() || !site || !profile || create.isPending}
          >
            {create.isPending ? "Adding…" : "Add"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

export function RobotPage() {
  const { orgId, projectId, base } = useScope();
  const { robotId = "" } = useParams();
  const navigate = useNavigate();
  const toasts = useToasts();
  const robot = useRobot(projectId, robotId);
  const sites = useSites(projectId);
  const profiles = useRobotProfiles(projectId);
  const sessions = useSessions(projectId);
  const faults = useFaults(projectId);
  const deployments = useDeployments(projectId);
  const can = usePermissions(orgId, projectId);
  const [confirming, setConfirming] = useState(false);
  const mineCount = (sessions.data ?? []).filter((x) => x.robot_id === robotId).length;
  const faultCount = (faults.data ?? []).filter(
    (f) => f.robot_id === robotId && f.latched && !f.cleared_at,
  ).length;
  const tabs: TabSpec[] = [
    { id: "overview", label: "Overview" },
    { id: "sessions", label: "Sessions", count: mineCount },
    { id: "faults", label: "Faults", count: faultCount },
  ];
  const [tab, setTab] = useTab(tabs);
  const remove = useApiMutation(
    async (version: string) => {
      await api.DELETE("/v1/robots/{robot_id}", {
        params: { path: { robot_id: robotId }, header: ifMatch(version) },
      });
    },
    [["p", projectId, "robots"]],
  );

  if (robot.isPending) return <Loading />;
  if (robot.isError) return <ErrorState error={robot.error} />;
  const r = robot.data;
  const site = sites.data?.find((s) => s.metadata.id === r.spec.site_id);
  const profile = profiles.data?.find((p) => p.metadata.id === r.spec.robot_profile_id);
  const mine = (sessions.data ?? []).filter((s) => s.robot_id === robotId);
  const myFaults = (faults.data ?? []).filter((f) => f.robot_id === robotId);
  const inDeployments = (deployments.data ?? []).filter((d) => d.spec.robot_ids.includes(robotId));
  const stale = site ? !site.status.connected : false;
  const live = mine.find((x) => x.state !== "Disconnected");

  return (
    <>
      <PageHeader
        breadcrumbs={<Link to={`${base}/robots`}>Robots</Link>}
        icon={<BotIcon />}
        title={r.spec.display_name}
        meta={
          <>
            <Status phase={r.status.phase} stale={stale} />
            <span>
              at{" "}
              {site ? (
                <Link to={`${base}/sites/${site.metadata.id}`}>{site.spec.display_name}</Link>
              ) : (
                "—"
              )}
            </span>
            <DataAge ageSeconds={r.status.data_age_seconds} fetchedAt={robot.dataUpdatedAt} />
            <span className="tag">{r.metadata.id}</span>
          </>
        }
        actions={
          can.admin ? (
            <button type="button" className="button danger" onClick={() => setConfirming(true)}>
              Remove robot
            </button>
          ) : null
        }
      />
      <Tabs label="Robot" tabs={tabs} value={tab} onChange={setTab} />
      {tab === "overview" ? (
        <TabPanel label="Overview">
          <div className="grid cols-4">
            <Stat label="Buffer (actions)" value={live?.metrics?.buffer_actions ?? "—"} />
            <Stat
              label="RTT p99"
              value={
                live?.metrics?.rtt_p99_ms !== undefined
                  ? `${live.metrics.rtt_p99_ms.toFixed(1)} ms`
                  : "—"
              }
            />
            <Stat label="Chunks" value={live?.metrics?.chunks ?? "—"} />
            <Stat label="Task epoch" value={r.status.task_epoch ?? "—"} />
          </div>
          <div className="grid cols-2">
            <Card title="Robot">
              <KeyValue
                items={[
                  ["Name", <code key="n">{r.spec.name}</code>],
                  [
                    "Site",
                    site ? (
                      <Link to={`${base}/sites/${site.metadata.id}`}>{site.spec.display_name}</Link>
                    ) : (
                      "—"
                    ),
                  ],
                  [
                    "Robot profile",
                    profile ? (
                      <Link key="p" to={`${base}/models/RobotProfile/${profile.metadata.id}`}>
                        {profileName(profile)}
                      </Link>
                    ) : (
                      "—"
                    ),
                  ],
                  [
                    "Deployments",
                    inDeployments.length
                      ? inDeployments.map((d) => (
                          <Link
                            key={d.metadata.id}
                            to={`${base}/deployments/${d.metadata.id}`}
                            style={{ marginRight: 8 }}
                          >
                            {d.spec.display_name}
                          </Link>
                        ))
                      : "None",
                  ],
                ]}
              />
            </Card>
            <Card title="Observed">
              <KeyValue
                items={[
                  ["State", <Status key="s" phase={r.status.phase} stale={stale} />],
                  [
                    "Reason",
                    r.status.reason ? (
                      <Reason code={r.status.reason} detail={r.status.message} />
                    ) : (
                      "—"
                    ),
                  ],
                  [
                    "Active revision",
                    r.status.active_revision_id ? (
                      <code>{r.status.active_revision_id}</code>
                    ) : (
                      "None"
                    ),
                  ],
                  [
                    "Lease holder",
                    r.status.lease_holder_session_id ? (
                      <code>{r.status.lease_holder_session_id}</code>
                    ) : (
                      "None"
                    ),
                  ],
                ]}
              />
            </Card>
          </div>
        </TabPanel>
      ) : null}
      {tab === "sessions" ? (
        <TabPanel label="Sessions">
          <Card
            title="Sessions"
            flush
            description="Each session is one lease on the gateway; a new one starts when the robot reconnects or switches revision."
          >
            {mine.length === 0 ? (
              <Empty title="No sessions reported" />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Session</th>
                      <th scope="col">State</th>
                      <th scope="col">Revision</th>
                      <th scope="col" className="num">
                        Lease generation
                      </th>
                      <th scope="col" className="num">
                        Buffer
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {mine.map((x) => (
                      <tr key={x.metadata.id}>
                        <td>
                          <code>{shortId(x.metadata.id)}</code>
                        </td>
                        <td>
                          <Status phase={x.state} />
                        </td>
                        <td>
                          <span className="tag">{shortId(x.revision_id)}</span>
                        </td>
                        <td className="num">{x.lease_generation}</td>
                        <td className="num">{x.metrics?.buffer_actions ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </TabPanel>
      ) : null}
      {tab === "faults" ? (
        <TabPanel label="Faults">
          <Card title="Faults" flush>
            <FaultTable faults={myFaults} />
          </Card>
        </TabPanel>
      ) : null}
      <Dialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Remove ${r.spec.display_name}?`}
        description="The robot leaves every deployment; the site stops serving it from its next desired state."
      >
        <div className="dialog-body">
          {remove.isError ? (
            <ErrorState error={remove.error} title="Could not remove the robot" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={() => setConfirming(false)}>
            Cancel
          </button>
          <button
            type="button"
            className="button danger"
            disabled={remove.isPending}
            onClick={() =>
              remove.mutate(r.metadata.version, {
                onSuccess: () => {
                  toasts.ok("Robot removed.");
                  void navigate(`${base}/robots`);
                },
              })
            }
          >
            Remove
          </button>
        </div>
      </Dialog>
    </>
  );
}

export { FaultTable };
