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
import { documentTitle } from "../components/Contracts";
import {
  Badge,
  Card,
  DataAge,
  Empty,
  ErrorState,
  Field,
  Loading,
  PageHeader,
  Reason,
  Status,
  Time,
} from "../components/ui";
import { useScope } from "../lib/context";
import { shortId } from "../lib/format";
import { FaultTable } from "./Robots";

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
        description="How many robots each site's server can serve at each prepared revision's execution profile, from the same admission model the gateway enforces. The estimate states its assumptions."
      />
      <Card flush>
        {rows.length === 0 ? (
          <Empty title="No estimates yet">
            <p>A site estimates capacity when it prepares a revision.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Site · release</th>
                  <th scope="col">Execution profile</th>
                  <th scope="col" className="num">
                    Robots it can run
                  </th>
                  <th scope="col" className="num">
                    In this release
                  </th>
                  <th scope="col">Limited by</th>
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
                  return (
                    <tr key={revision.metadata.id}>
                      <td>
                        {sites.data?.find((s) => s.metadata.id === deployment.spec.site_id)?.spec
                          .display_name ?? "—"}
                        <div className="small">
                          <Link to={`${base}/releases/${deployment.metadata.id}`}>
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
  const { projectId, base } = useScope();
  const sessions = useSessions(projectId);
  const robots = useRobots(projectId);
  const [showEnded, setShowEnded] = useState(false);
  const list = (sessions.data ?? []).filter((s) => showEnded || s.state !== "Disconnected");
  return (
    <>
      <PageHeader
        title="Live sessions"
        description="One row per robot session on a site's gateway, as the site reports it. No video or payloads: only states and aggregates."
        actions={
          <label className="row small">
            <input
              type="checkbox"
              checked={showEnded}
              onChange={(e) => setShowEnded(e.target.checked)}
            />{" "}
            Show ended
          </label>
        }
      />
      <Card flush>
        {sessions.isPending ? (
          <Loading />
        ) : sessions.isError ? (
          <ErrorState error={sessions.error} />
        ) : list.length === 0 ? (
          <Empty title="No live sessions">
            <p>Sessions appear when robots connect to their site's gateway.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Live sessions</caption>
              <thead>
                <tr>
                  <th scope="col">Robot</th>
                  <th scope="col">State</th>
                  <th scope="col">Revision</th>
                  <th scope="col" className="num">
                    Buffer (actions)
                  </th>
                  <th scope="col" className="num">
                    Chunks
                  </th>
                  <th scope="col" className="num">
                    RTT p99
                  </th>
                  <th scope="col" className="num">
                    Task epoch
                  </th>
                  <th scope="col">Last report</th>
                </tr>
              </thead>
              <tbody>
                {list.map((s) => (
                  <tr key={s.metadata.id}>
                    <td>
                      <Link to={`${base}/robots/${s.robot_id}`}>
                        {robots.data?.find((r) => r.metadata.id === s.robot_id)?.spec
                          .display_name ?? shortId(s.robot_id)}
                      </Link>
                    </td>
                    <td>
                      <Status phase={s.state} />
                      {s.reason ? (
                        <div className="small">
                          <Reason code={s.reason} />
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <code>{shortId(s.revision_id)}</code>
                    </td>
                    <td className="num">{s.metrics?.buffer_actions ?? "—"}</td>
                    <td className="num">{s.metrics?.chunks ?? "—"}</td>
                    <td className="num">
                      {s.metrics?.rtt_p99_ms !== undefined
                        ? `${s.metrics.rtt_p99_ms.toFixed(1)} ms`
                        : "—"}
                    </td>
                    <td className="num">{s.task_epoch}</td>
                    <td>
                      <DataAge
                        ageSeconds={s.data_age_seconds}
                        fetchedAt={sessions.dataUpdatedAt}
                        staleAfter={30}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}

export function Faults() {
  const { projectId } = useScope();
  const faults = useFaults(projectId);
  const [onlyOpen, setOnlyOpen] = useState(true);
  const list = (faults.data ?? []).filter((f) => !onlyOpen || !f.cleared_at);
  return (
    <>
      <PageHeader
        title="Faults"
        description="Faults the sites reported, with the response the robot took locally. Latched faults stay until someone resets them at the site."
        actions={
          <label className="row small">
            <input
              type="checkbox"
              checked={onlyOpen}
              onChange={(e) => setOnlyOpen(e.target.checked)}
            />{" "}
            Open only
          </label>
        }
      />
      <Card flush>
        {faults.isPending ? (
          <Loading />
        ) : faults.isError ? (
          <ErrorState error={faults.error} />
        ) : list.length === 0 ? (
          <Empty title={onlyOpen ? "No open faults" : "No faults reported"} />
        ) : (
          <FaultTable faults={[...list].reverse()} />
        )}
      </Card>
    </>
  );
}

export function Audit() {
  const { projectId } = useScope();
  const audit = useAudit(projectId);
  const [filter, setFilter] = useState("");
  const list = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const items = [...(audit.data ?? [])].reverse();
    if (!q) return items;
    return items.filter((r) =>
      `${r.action} ${r.actor} ${r.target} ${r.result}`.toLowerCase().includes(q),
    );
  }, [audit.data, filter]);
  return (
    <>
      <PageHeader
        title="Audit log"
        description="Every change and every instruction sent to a site, with who did it and the result. Written in the same transaction as the change."
      />
      <Card flush>
        <div className="card-body" style={{ borderBottom: "1px solid var(--border)" }}>
          <Field id="audit-filter" label="Filter" hint="Matches action, actor, target and result.">
            <input
              id="audit-filter"
              type="text"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </Field>
        </div>
        {audit.isPending ? (
          <Loading />
        ) : audit.isError ? (
          <ErrorState error={audit.error} />
        ) : list.length === 0 ? (
          <Empty title="No audit records" />
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Audit records</caption>
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Actor</th>
                  <th scope="col">Action</th>
                  <th scope="col">Target</th>
                  <th scope="col">Result</th>
                </tr>
              </thead>
              <tbody>
                {list.map((r) => (
                  <tr key={r.metadata.id}>
                    <td className="small">
                      <Time iso={r.occurred_at} />
                    </td>
                    <td className="small">
                      {r.actor}
                      <div className="muted">{r.actor_kind?.replace("_", " ")}</div>
                    </td>
                    <td>
                      <code>{r.action}</code>
                    </td>
                    <td>
                      <code>{shortId(r.target)}</code>
                    </td>
                    <td>
                      <Badge tone={r.result === "succeeded" ? "ok" : "danger"}>{r.result}</Badge>
                      {r.reason ? (
                        <div className="small">
                          <Reason code={r.reason} />
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
