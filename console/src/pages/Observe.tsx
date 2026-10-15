// Capacity, live sessions, faults and the audit log (design §19.3).
import { useQueries } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import {
  useAudit,
  useDeployments,
  useExecutionProfiles,
  useFaults,
  useRobots,
  useSessions,
  useSites,
} from "../api/queries";
import { useEventFeed } from "../api/events";
import { ActivityFeed, describe } from "../components/Activity";
import { documentTitle, sharingCase } from "../components/Contracts";
import { ActivityIcon, AlertIcon, GaugeIcon, PulseIcon } from "../components/Icons";
import { AuditTable, FaultTable, SessionTable } from "../components/Tables";
import {
  Badge,
  Card,
  Empty,
  ErrorState,
  Loading,
  Meter,
  PageHeader,
  SearchInput,
  Segmented,
  Stat,
  TabPanel,
  Tabs,
  Time,
  useTab,
  type TabSpec,
} from "../components/ui";
import { useScope } from "../lib/context";
import { ago, secondsSince, shortId } from "../lib/format";
import { useNow } from "../lib/useNow";

export function Capacity() {
  const { projectId, base } = useScope();
  const deployments = useDeployments(projectId);
  const sites = useSites(projectId);
  const profiles = useExecutionProfiles(projectId);
  const revisionQueries = useQueries({
    queries: (deployments.data ?? []).map((d) => ({
      queryKey: ["p", projectId, "revisions", d.metadata.id],
      queryFn: async () =>
        unwrap(
          await api.GET("/v1/deployments/{deployment_id}/revisions", {
            params: { path: { deployment_id: d.metadata.id }, query: { page_size: 200 } },
          }),
        ).items,
    })),
  });
  if (deployments.isPending) return <Loading />;
  const rows = (deployments.data ?? []).flatMap((d, i) =>
    (revisionQueries[i]?.data ?? [])
      .filter((r) => r.status.capacity)
      .map((r) => ({
        deployment: d,
        revision: r,
        capacity: r.status.capacity as Schemas["CapacityEstimate"],
      })),
  );

  return (
    <>
      <PageHeader
        title="Capacity"
        icon={<GaugeIcon />}
        description="How many robots each site's server can serve at each prepared revision's execution profile, from the same admission model the gateway enforces. Every estimate states its assumptions."
      />
      {rows.length > 0 ? (
        <div className="grid cols-3">
          {rows.slice(0, 3).map(({ deployment, revision, capacity }) => {
            const used = deployment.spec.robot_ids.length;
            const share = capacity.max_robots > 0 ? used / capacity.max_robots : 1;
            return (
              <div className="card stat" key={revision.metadata.id}>
                <span className="stat-label">
                  {deployment.spec.display_name} · <code>{shortId(revision.metadata.id)}</code>
                </span>
                <span className="stat-value">
                  {used}
                  <span className="of"> / {capacity.max_robots} robots</span>
                </span>
                <Meter
                  value={used}
                  max={capacity.max_robots}
                  tone={share > 1 ? "danger" : share > 0.8 ? "warn" : "ok"}
                  label={`${used} of ${capacity.max_robots} robots used`}
                />
                <span className="stat-note">
                  limited by {capacity.limiting_factor.replaceAll("_", " ")}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
      <Card flush>
        {rows.length === 0 ? (
          <Empty title="No estimates yet" icon={<GaugeIcon />}>
            <p>A site estimates capacity when it prepares a revision, from its measured latency.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Site · deployment</th>
                  <th scope="col">Execution profile</th>
                  <th scope="col" className="num">
                    Robots it can run
                  </th>
                  <th scope="col" className="num">
                    In this deployment
                  </th>
                  <th scope="col">Limited by</th>
                  <th scope="col">Device</th>
                  <th scope="col">Assumptions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ deployment, revision, capacity }) => {
                  const used = deployment.spec.robot_ids.length;
                  const profile = profiles.data?.find(
                    (p) => p.metadata.id === revision.spec.execution_profile_id,
                  );
                  const a = capacity.assumptions;
                  const sharing = sharingCase(capacity.sharing_case);
                  return (
                    <tr key={revision.metadata.id}>
                      <td>
                        {sites.data?.find((s) => s.metadata.id === deployment.spec.site_id)?.spec
                          .display_name ?? "—"}
                        <div className="small">
                          <Link to={`${base}/deployments/${deployment.metadata.id}`}>
                            {deployment.spec.display_name}
                          </Link>{" "}
                          <span className="muted mono">{shortId(revision.metadata.id)}</span>
                        </div>
                      </td>
                      <td>{documentTitle(profile)}</td>
                      <td className="num">
                        <strong>{capacity.max_robots}</strong>
                      </td>
                      <td className="num">
                        {used}{" "}
                        {used > capacity.max_robots ? (
                          <Badge tone="danger">Over capacity</Badge>
                        ) : (
                          <span className="muted">({capacity.max_robots - used} spare)</span>
                        )}
                      </td>
                      <td>{capacity.limiting_factor.replaceAll("_", " ")}</td>
                      <td className="small">
                        {a?.device ? <span className="mono">{a.device}</span> : "—"}
                        <div title={sharing.detail}>{sharing.label}</div>
                        {a?.co_located_groups ? (
                          <div className="muted">
                            {a.co_located_groups} other{" "}
                            {a.co_located_groups === 1 ? "group" : "groups"} on the device
                          </div>
                        ) : null}
                      </td>
                      <td className="small">
                        {!a ? (
                          <span title="This site runs minimal telemetry: the estimate's inputs stay at the site.">
                            {capacity.source === "measured_profile" ? "Measured" : "Configured"}{" "}
                            latency profile; inputs kept at the site
                          </span>
                        ) : (
                          <details>
                            <summary>
                              {capacity.source === "measured_profile" ? "Measured" : "Configured"}{" "}
                              latency profile
                            </summary>
                            <dl className="kv" style={{ marginTop: 8 }}>
                              <dt>Control period</dt>
                              <dd>{a.control_period_ms} ms</dd>
                              <dt>Starvation budget</dt>
                              <dd>{a.starvation_budget_ms} ms</dd>
                              <dt>Request interval</dt>
                              <dd>{a.request_interval_ms} ms</dd>
                              <dt>Link allowance</dt>
                              <dd>{a.link_allowance_ms} ms</dd>
                              <dt>Freshness</dt>
                              <dd>
                                {a.dispatch_freshness_ms ?? "—"} ms at dispatch,{" "}
                                {a.acceptance_freshness_ms ?? "—"} ms at acceptance
                              </dd>
                              <dt>Batching</dt>
                              <dd>
                                up to {a.max_batch}, {Math.round((a.reserve_fraction ?? 0) * 100)}%
                                reserve
                              </dd>
                              <dt>p99 latency</dt>
                              <dd>
                                {Object.entries(a.latency_p99_ms_by_batch ?? {})
                                  .map(([batch, latency]) => `batch ${batch}: ${latency} ms`)
                                  .join(", ")}
                              </dd>
                            </dl>
                          </details>
                        )}
                        <div className="muted">
                          Estimated <Time iso={capacity.estimated_at} />
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

export function Sessions() {
  const { projectId } = useScope();
  const sessions = useSessions(projectId);
  const robots = useRobots(projectId);
  const [show, setShow] = useState<"live" | "all">("live");
  const [query, setQuery] = useState("");
  const all = sessions.data ?? [];
  const list = all.filter((s) => {
    if (show === "live" && s.state === "Disconnected") return false;
    if (!query) return true;
    const name = robots.data?.find((r) => r.metadata.id === s.robot_id)?.spec.display_name ?? "";
    return `${name} ${s.robot_id} ${s.revision_id} ${s.state}`
      .toLowerCase()
      .includes(query.toLowerCase());
  });
  const count = (state: string) => all.filter((s) => s.state === state).length;
  return (
    <>
      <PageHeader
        title="Live sessions"
        icon={<PulseIcon />}
        description="One row per robot session on a site's gateway, as the site reports it, refreshed every few seconds. No video or payloads: only states and aggregates."
      />
      <div className="grid cols-4">
        <Stat label="Running" value={count("Running")} />
        <Stat label="Waiting (starved, holding)" value={count("Waiting")} />
        <Stat label="Ready or armed" value={count("Ready") + count("Armed")} />
        <Stat label="Faulted" value={count("Faulted")} />
      </div>
      <div className="toolbar">
        <SearchInput
          value={query}
          onChange={setQuery}
          placeholder="Filter by robot, revision or state"
          label="Filter sessions"
        />
        <Segmented
          label="Show"
          value={show}
          onChange={setShow}
          options={[
            { id: "live", label: "Live" },
            { id: "all", label: "Include ended" },
          ]}
        />
        <span className="grow" />
        <span className="muted small">Trends show what this console observed since it opened.</span>
      </div>
      <Card flush>
        {sessions.isPending ? (
          <Loading />
        ) : sessions.isError ? (
          <ErrorState error={sessions.error} />
        ) : (
          <SessionTable
            sessions={list}
            fetchedAt={sessions.dataUpdatedAt}
            caption="Live sessions"
          />
        )}
      </Card>
    </>
  );
}

export function Faults() {
  const { projectId } = useScope();
  const faults = useFaults(projectId);
  const [show, setShow] = useState<"open" | "all">("open");
  const list = (faults.data ?? []).filter((f) => show === "all" || !f.cleared_at);
  return (
    <>
      <PageHeader
        title="Faults"
        icon={<AlertIcon />}
        description="Faults the sites reported, with the response the robot took locally. A latched fault stays until a person resets it at the site: nothing here can clear it."
      />
      <div className="toolbar">
        <Segmented
          label="Show"
          value={show}
          onChange={setShow}
          options={[
            { id: "open", label: "Open" },
            { id: "all", label: "All" },
          ]}
        />
      </div>
      <Card flush>
        {faults.isPending ? (
          <Loading />
        ) : faults.isError ? (
          <ErrorState error={faults.error} />
        ) : list.length === 0 ? (
          <Empty
            title={show === "open" ? "No open faults" : "No faults reported"}
            icon={<AlertIcon />}
          >
            <p>
              Every robot is running or ready. Faults appear here as soon as a site reports one.
            </p>
          </Empty>
        ) : (
          <FaultTable faults={[...list].reverse()} showRobot />
        )}
      </Card>
    </>
  );
}

const ACTIVITY_TABS: TabSpec[] = [
  { id: "live", label: "Live events" },
  { id: "audit", label: "Audit log" },
];

/** Activity: what is changing now (the event stream) and what changed (the audit log). */
export function Activity() {
  const { projectId } = useScope();
  const [tab, setTab] = useTab(ACTIVITY_TABS);
  return (
    <>
      <PageHeader
        title="Activity"
        icon={<ActivityIcon />}
        description="Live events show changes as they happen. The audit log records every change and every instruction sent to a site, with who did it and the result, written in the same transaction as the change."
      />
      <Tabs label="Activity" tabs={ACTIVITY_TABS} value={tab} onChange={setTab} />
      {tab === "live" ? <LiveEvents projectId={projectId} /> : <Audit />}
    </>
  );
}

function LiveEvents({ projectId }: { projectId: string }) {
  const events = useEventFeed(projectId);
  const { base } = useScope();
  const now = useNow(5000);
  return (
    <TabPanel label="Live events">
      <Card
        title="Event stream"
        description="Received since this console opened; the oldest scroll away after 300."
        flush
      >
        {events.length === 0 ? (
          <ActivityFeed events={[]} />
        ) : (
          <ol className="log" aria-label="Events, newest first">
            {[...events].reverse().map((event) => {
              const d = describe(event);
              return (
                <li key={event.cursor}>
                  <span className="ts" title={event.occurred_at}>
                    {ago(secondsSince(event.occurred_at, now))}
                  </span>
                  <span className={`ty ${d.tone}`}>{event.type}</span>
                  <span>
                    {d.noun}{" "}
                    {d.path ? (
                      <Link to={`${base}/${d.path}`}>{event.resource}</Link>
                    ) : (
                      event.resource
                    )}
                    {event.resource_version ? (
                      <span className="muted"> v{event.resource_version}</span>
                    ) : null}
                  </span>
                </li>
              );
            })}
          </ol>
        )}
      </Card>
    </TabPanel>
  );
}

function Audit() {
  const { projectId } = useScope();
  const audit = useAudit(projectId);
  const [filter, setFilter] = useState("");
  const [result, setResult] = useState<"all" | "failed">("all");
  const list = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return [...(audit.data ?? [])].reverse().filter((r) => {
      if (result === "failed" && r.result === "succeeded") return false;
      return !q || `${r.action} ${r.actor} ${r.target} ${r.result}`.toLowerCase().includes(q);
    });
  }, [audit.data, filter, result]);
  return (
    <TabPanel label="Audit log">
      <div className="toolbar">
        <SearchInput
          value={filter}
          onChange={setFilter}
          placeholder="Action, actor, target or result"
          label="Filter audit records"
        />
        <Segmented
          label="Result"
          value={result}
          onChange={setResult}
          options={[
            { id: "all", label: "All results" },
            { id: "failed", label: "Failed or denied" },
          ]}
        />
        <span className="grow" />
        <span className="muted small">{list.length} records</span>
      </div>
      <Card flush>
        {audit.isPending ? (
          <Loading />
        ) : audit.isError ? (
          <ErrorState error={audit.error} />
        ) : (
          <AuditTable records={list} />
        )}
      </Card>
    </TabPanel>
  );
}
