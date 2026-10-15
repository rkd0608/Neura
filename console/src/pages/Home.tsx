// Home: the project at a glance, items needing attention first (design §19.3), and the steps
// from an empty project to robots running a model.
import { useQueries } from "@tanstack/react-query";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import {
  useArtifacts,
  useDeployments,
  useExecutionProfiles,
  useFaults,
  useProject,
  useRobotProfiles,
  useRobots,
  useSessions,
  useSites,
} from "../api/queries";
import { Card, Loading, PageHeader, Reason, Stat, Status } from "../components/ui";
import { useScope } from "../lib/context";

interface Step {
  title: string;
  text: string;
  done: boolean;
  to: string;
  action: string;
}

export function Home() {
  const { projectId, base } = useScope();
  const project = useProject(projectId);
  const sites = useSites(projectId);
  const robots = useRobots(projectId);
  const sessions = useSessions(projectId);
  const faults = useFaults(projectId);
  const deployments = useDeployments(projectId);
  const artifacts = useArtifacts(projectId);
  const robotProfiles = useRobotProfiles(projectId);
  const executionProfiles = useExecutionProfiles(projectId);
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

  if (sites.isPending || robots.isPending || deployments.isPending) return <Loading />;

  const siteList = sites.data ?? [];
  const robotList = robots.data ?? [];
  const deploymentList = deployments.data ?? [];
  const serverCount = servers.reduce((n, q) => n + (q.data?.length ?? 0), 0);
  const connected = siteList.filter((s) => s.status.connected).length;
  const running = robotList.filter((r) => r.status.phase === "Running").length;
  const latched = (faults.data ?? []).filter((f) => f.latched && !f.cleared_at);
  const live = (sessions.data ?? []).filter((s) => s.state !== "Disconnected");
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
      to: siteList[0] ? `${base}/sites/${siteList[0].metadata.id}` : `${base}/sites`,
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
      title: "Create a release and prepare it",
      text: "The site verifies the contracts and estimates how many robots it can serve.",
      done: prepared,
      to: `${base}/releases`,
      action: "Open releases",
    },
    {
      title: "Activate, with approval at the site",
      text: "The cloud requests activation; a person at the site approves it. Robots switch at a task boundary.",
      done: active,
      to: `${base}/releases`,
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

  return (
    <>
      <PageHeader
        title={project.data?.spec.display_name ?? "Project"}
        description="What is running, what needs attention, and what to do next."
      />
      <div className="grid cols-4">
        <Stat label="Sites connected" value={`${connected} / ${siteList.length}`} />
        <Stat label="GPU servers" value={serverCount} />
        <Stat label="Robots running" value={`${running} / ${robotList.length}`} />
        <Stat label="Live sessions" value={live.length} note={`${latched.length} latched faults`} />
      </div>
      {attention.length > 0 ? (
        <Card title="Needs attention" flush>
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
      <Card title="Releases" flush actions={<Link to={`${base}/releases`}>All releases</Link>}>
        {deploymentList.length === 0 ? (
          <p className="card-body muted">No releases yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Release</th>
                  <th scope="col">Phase</th>
                  <th scope="col" className="num">
                    Robots on the active revision
                  </th>
                </tr>
              </thead>
              <tbody>
                {deploymentList.map((d) => {
                  const actives = Object.values(d.status.active_revision_ids ?? {}).filter(Boolean);
                  return (
                    <tr key={d.metadata.id}>
                      <td>
                        <Link to={`${base}/releases/${d.metadata.id}`}>{d.spec.display_name}</Link>
                      </td>
                      <td>
                        <Status phase={d.status.phase} />
                      </td>
                      <td className="num">
                        {actives.length} / {d.spec.robot_ids.length}
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
