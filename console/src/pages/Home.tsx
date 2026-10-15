// Overview: the project at a glance (design §19.3). What needs attention comes first; then the
// steps from an empty project to robots running a model; then deployments, how the fleet is
// being served right now, and what just changed.
import { useQueries } from "@tanstack/react-query";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import { useEventFeed } from "../api/events";
import {
  useArtifacts,
  useDeployments,
  useExecutionProfiles,
  useFaults,
  useMetric,
  useProject,
  useRobotProfiles,
  useRobots,
  useSessions,
  useSites,
} from "../api/queries";
import { ActivityFeed } from "../components/Activity";
import {
  AlertIcon,
  BotIcon,
  BuildingIcon,
  ChevronRightIcon,
  PlusIcon,
  PulseIcon,
  RocketIcon,
} from "../components/Icons";
import {
  Card,
  Empty,
  LineChart,
  Loading,
  Meter,
  PageHeader,
  Reason,
  Sparkline,
  Status,
  useLiveSeries,
} from "../components/ui";
import { usePermissions, useScope } from "../lib/context";

interface Step {
  title: string;
  text: string;
  done: boolean;
  to: string;
  action: string;
}

export function Home() {
  const { orgId, projectId, base } = useScope();
  const can = usePermissions(orgId);
  const project = useProject(projectId);
  const sites = useSites(projectId);
  const robots = useRobots(projectId);
  const sessions = useSessions(projectId);
  const faults = useFaults(projectId);
  const deployments = useDeployments(projectId);
  const artifacts = useArtifacts(projectId);
  const robotProfiles = useRobotProfiles(projectId);
  const executionProfiles = useExecutionProfiles(projectId);
  const events = useEventFeed(projectId);
  const missRate = useMetric(projectId, "deadline_miss_rate");
  const servers = useQueries({
    queries: (sites.data ?? []).map((site) => ({
      queryKey: ["p", projectId, "servers", site.metadata.id],
      queryFn: async () =>
        unwrap(
          await api.GET("/v1/sites/{site_id}/servers", {
            params: { path: { site_id: site.metadata.id }, query: { page_size: 200 } },
          }),
        ).items,
    })),
  });

  const sessionList = sessions.data ?? [];
  // A robot that reconnected can still have an older session reported until it expires: count
  // each robot's newest live session once.
  const live = [
    ...new Map(
      sessionList.filter((s) => s.state !== "Disconnected").map((s) => [s.robot_id, s]),
    ).values(),
  ];
  const running = (robots.data ?? []).filter((r) => r.status.phase === "Running").length;
  const at = sessions.dataUpdatedAt;
  const avg = (pick: (s: (typeof live)[number]) => number | undefined) => {
    const values = live.map(pick).filter((v): v is number => typeof v === "number");
    return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  };
  const runningSeries = useLiveSeries(
    `${projectId}:running`,
    sessions.data ? live.filter((s) => s.state === "Running").length : undefined,
    at,
  );
  const liveSeries = useLiveSeries(
    `${projectId}:live`,
    sessions.data ? live.length : undefined,
    at,
  );
  const bufferSeries = useLiveSeries(
    `${projectId}:buffer`,
    sessions.data ? avg((s) => s.metrics?.buffer_actions) : undefined,
    at,
  );
  const rttSeries = useLiveSeries(
    `${projectId}:rtt`,
    sessions.data ? avg((s) => s.metrics?.rtt_p99_ms) : undefined,
    at,
  );

  if (sites.isPending || robots.isPending || deployments.isPending) return <Loading />;

  const siteList = sites.data ?? [];
  const robotList = robots.data ?? [];
  const deploymentList = deployments.data ?? [];
  const serverCount = servers.reduce((n, q) => n + (q.data?.length ?? 0), 0);
  const connected = siteList.filter((s) => s.status.connected).length;
  const latched = (faults.data ?? []).filter((f) => f.latched && !f.cleared_at);
  const contracts =
    (artifacts.data?.length ?? 0) > 0 &&
    (robotProfiles.data?.length ?? 0) > 0 &&
    (executionProfiles.data?.length ?? 0) > 0;
  const prepared = deploymentList.some((d) => d.status.prepared_revision_id);
  const active = deploymentList.some((d) =>
    Object.values(d.status.active_revision_ids ?? {}).some(Boolean),
  );

  const steps: Step[] = [
    {
      title: "Create a site",
      text: "A site is a place where robots and the GPU servers that serve them share a network.",
      done: siteList.length > 0,
      to: `${base}/sites`,
      action: "Add a site",
    },
    {
      title: "Connect a GPU server",
      text: "Run one command on the server. It dials out to Neura; no inbound ports.",
      done: serverCount > 0,
      to: siteList[0] ? `${base}/sites/${siteList[0].metadata.id}?connect=1` : `${base}/sites`,
      action: "Connect a server",
    },
    {
      title: "Register your model and robot",
      text: "A model artifact, a robot profile and an execution profile describe what runs and how.",
      done: contracts,
      to: `${base}/models`,
      action: "Register contracts",
    },
    {
      title: "Add robots",
      text: "Each robot belongs to a site and has a robot profile.",
      done: robotList.length > 0,
      to: `${base}/robots`,
      action: "Add robots",
    },
    {
      title: "Create a deployment and prepare it",
      text: "The site verifies the contracts and estimates how many robots it can serve.",
      done: prepared,
      to: `${base}/deployments`,
      action: "Open deployments",
    },
    {
      title: "Activate, with approval at the site",
      text: "The cloud requests activation; a person at the site approves it. Robots switch at a task boundary.",
      done: active,
      to: `${base}/deployments`,
      action: "Request activation",
    },
  ];
  const current = steps.findIndex((s) => !s.done);

  const attention = [
    ...siteList
      .filter((s) => !s.status.connected && s.status.observed_at)
      .map((s) => ({
        key: s.metadata.id,
        what: <Link to={`${base}/sites/${s.metadata.id}`}>{s.spec.display_name}</Link>,
        state: <Status phase="Offline" />,
        why: "The site agent is not connected. The site keeps running on its committed configuration.",
      })),
    ...robotList
      .filter((r) => r.status.phase === "NeedsAttention")
      .map((r) => ({
        key: r.metadata.id,
        what: <Link to={`${base}/robots/${r.metadata.id}`}>{r.spec.display_name}</Link>,
        state: <Status phase="NeedsAttention" />,
        why: <Reason code={r.status.reason} detail={r.status.message} />,
      })),
    ...latched.slice(0, 10).map((f) => ({
      key: f.metadata.id,
      what: (
        <Link to={`${base}/robots/${f.robot_id}`}>
          {robotList.find((r) => r.metadata.id === f.robot_id)?.spec.display_name ?? f.robot_id}
        </Link>
      ),
      state: <Status phase="Faulted" />,
      why: <Reason code={f.reason} detail={f.message} />,
    })),
  ];

  const spark = (series: [number, number | null][]) =>
    series.map((p) => p[1]).filter((v): v is number => v !== null);

  return (
    <>
      <PageHeader
        title={project.data?.spec.display_name ?? "Project"}
        description="What is running, what needs attention, and what to do next."
        actions={
          can.deploy ? (
            <>
              <Link className="button" to={`${base}/robots`}>
                <BotIcon /> Robots
              </Link>
              <Link className="button primary" to={`${base}/deployments?new=1`}>
                <PlusIcon /> New deployment
              </Link>
            </>
          ) : null
        }
      />

      <div className="grid cols-4">
        <Tile
          label="Robots running"
          icon={<BotIcon />}
          value={running}
          of={robotList.length}
          to={`${base}/robots`}
          trend={spark(runningSeries)}
        />
        <Tile
          label="Live sessions"
          icon={<PulseIcon />}
          value={live.length}
          to={`${base}/sessions`}
          trend={spark(liveSeries)}
          note={`on ${connected} of ${siteList.length} connected sites`}
        />
        <Tile
          label="GPU servers"
          icon={<BuildingIcon />}
          value={serverCount}
          to={`${base}/sites`}
          note={`${siteList.length} ${siteList.length === 1 ? "site" : "sites"}`}
        />
        <Tile
          label="Open faults"
          icon={<AlertIcon />}
          value={latched.length}
          to={`${base}/faults`}
          tone={latched.length > 0 ? "danger" : undefined}
          note={latched.length > 0 ? "latched: reset at the site" : "none latched"}
        />
      </div>

      {attention.length > 0 ? (
        <Card title="Needs attention" description="Faulted robots and offline sites first." flush>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">What</th>
                  <th scope="col">State</th>
                  <th scope="col">Why</th>
                </tr>
              </thead>
              <tbody>
                {attention.map((item) => (
                  <tr key={item.key}>
                    <td>{item.what}</td>
                    <td>{item.state}</td>
                    <td>{item.why}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}

      {current !== -1 ? (
        <Card
          title="Get to robots running a model"
          description={`${steps.filter((s) => s.done).length} of ${steps.length} steps done`}
          flush
        >
          <ol className="steps">
            {steps.map((step, i) => (
              <li
                key={step.title}
                className={`step${step.done ? " done" : ""}${i === current ? " current" : ""}`}
              >
                <span className="step-mark" aria-hidden="true">
                  {step.done ? "✓" : i + 1}
                </span>
                <div>
                  <div className="step-title">
                    {step.title}
                    <span className="visually-hidden">{step.done ? " (done)" : ""}</span>
                  </div>
                  <div className="step-text">{step.text}</div>
                </div>
                {i === current ? (
                  <Link className="button primary small" to={step.to}>
                    {step.action}
                  </Link>
                ) : null}
              </li>
            ))}
          </ol>
        </Card>
      ) : null}

      <div className="grid main-side">
        <div className="stack" style={{ gap: 16 }}>
          <Card
            title="Deployments"
            flush
            actions={<Link to={`${base}/deployments`}>All deployments</Link>}
          >
            {deploymentList.length === 0 ? (
              <Empty
                title="No deployments yet"
                icon={<RocketIcon />}
                actions={
                  can.deploy ? (
                    <Link className="button primary small" to={`${base}/deployments?new=1`}>
                      New deployment
                    </Link>
                  ) : null
                }
              >
                <p>A deployment runs one model on a set of robots at a site.</p>
              </Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Deployment</th>
                      <th scope="col">Phase</th>
                      <th scope="col">On the active revision</th>
                      <th scope="col">
                        <span className="visually-hidden">Open</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {deploymentList.map((d) => {
                      const actives = Object.values(d.status.active_revision_ids ?? {}).filter(
                        Boolean,
                      );
                      const site = siteList.find((s) => s.metadata.id === d.spec.site_id);
                      return (
                        <tr key={d.metadata.id}>
                          <td>
                            <Link to={`${base}/deployments/${d.metadata.id}`}>
                              {d.spec.display_name}
                            </Link>
                            <span className="sub">{site?.spec.display_name ?? "—"}</span>
                          </td>
                          <td>
                            <Status phase={d.status.phase} />
                          </td>
                          <td style={{ minWidth: 160 }}>
                            <div className="spread small" style={{ marginBottom: 4 }}>
                              <span className="secondary">robots</span>
                              <span className="num">
                                {actives.length} / {d.spec.robot_ids.length}
                              </span>
                            </div>
                            <Meter
                              value={actives.length}
                              max={d.spec.robot_ids.length}
                              tone="ok"
                              label={`${actives.length} of ${d.spec.robot_ids.length} robots on the active revision`}
                            />
                          </td>
                          <td className="num">
                            <Link
                              to={`${base}/deployments/${d.metadata.id}`}
                              aria-label={`Open ${d.spec.display_name}`}
                              className="button ghost small icon"
                            >
                              <ChevronRightIcon />
                            </Link>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
          <div className="grid cols-2">
            <Card
              title="Action buffer"
              description="Mean actions queued per live robot, as sites report."
              actions={<Link to={`${base}/sessions`}>Sessions</Link>}
            >
              <LineChart
                label="Mean actions queued"
                points={bufferSeries}
                format={(v) => v.toFixed(0)}
                empty="Appears once robots are served"
              />
            </Card>
            <Card title="Round trip, p99" description="Mean over live robots, milliseconds.">
              <LineChart
                label="Round trip p99"
                points={rttSeries}
                color="var(--chart-3)"
                format={(v) => v.toFixed(0)}
                empty="Appears once robots are served"
              />
            </Card>
          </div>
          <Card
            title="Deadline misses"
            description="Share of inference requests that missed their deadline, last hour, from site telemetry."
          >
            <LineChart
              label="Deadline miss rate"
              points={missRate.data ?? []}
              color="var(--chart-4)"
              format={(v) => `${(v * 100).toFixed(1)}%`}
              empty={
                missRate.isPending
                  ? "Loading…"
                  : "No telemetry in the last hour (sites may run minimal telemetry)"
              }
            />
          </Card>
        </div>
        <Card
          title="Live activity"
          description="Changes as they happen."
          flush
          actions={<Link to={`${base}/activity`}>All activity</Link>}
        >
          <ActivityFeed events={events.slice(-80).reverse()} compact />
        </Card>
      </div>
    </>
  );
}

function Tile({
  label: text,
  icon,
  value,
  of,
  note,
  to,
  trend,
  tone,
}: {
  label: string;
  icon: React.ReactNode;
  value: number;
  of?: number;
  note?: string;
  to: string;
  trend?: number[];
  tone?: "danger";
}) {
  return (
    <Link
      to={to}
      className="card stat interactive"
      style={{ color: "inherit", textDecoration: "none" }}
    >
      <span className="stat-label">
        {icon}
        {text}
      </span>
      <span
        className="stat-value"
        style={tone === "danger" ? { color: "var(--danger)" } : undefined}
      >
        {value}
        {of !== undefined ? <span className="of"> / {of}</span> : null}
      </span>
      <span className="spread">
        <span className="stat-note">{note ?? " "}</span>
        {trend && trend.length > 1 ? <Sparkline values={trend} label={`${text} trend`} /> : null}
      </span>
    </Link>
  );
}
