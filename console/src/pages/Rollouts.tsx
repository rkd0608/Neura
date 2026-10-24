// Rollouts: every change to what robots run, step by step, as `neura deploy` shows it (design §29).
import { Link, useParams } from "react-router";

import { api, idempotent, unwrap, type Schemas } from "../api/client";
import {
  useApiMutation,
  useFleetActivity,
  useRobots,
  useRollout,
  useRollouts,
  useSites,
} from "../api/queries";
import { RocketIcon } from "../components/Icons";
import {
  Badge,
  Callout,
  Card,
  Empty,
  ErrorState,
  KeyValue,
  Loading,
  PageHeader,
  Reason,
  Time,
} from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import {
  KIND_LABELS,
  STEP_LABELS,
  TERMINAL,
  rolloutLabel,
  rolloutTone,
  stepTone,
} from "../lib/fleet";

type Rollout = Schemas["Rollout"];

const STEP_MARKS: Record<string, string> = {
  passed: "✓",
  failed: "✗",
  running: "◐",
  skipped: "–",
  pending: "·",
};

export function Rollouts() {
  const { projectId, base } = useScope();
  const rollouts = useRollouts(projectId);
  const robots = useRobots(projectId);
  if (rollouts.isPending) return <Loading />;
  if (rollouts.isError) return <ErrorState error={rollouts.error} />;
  const names = (ids: string[]) =>
    ids.map((id) => robots.data?.find((r) => r.metadata.id === id)?.spec.name ?? id).join(", ");
  return (
    <>
      <PageHeader
        title="Rollouts"
        icon={<RocketIcon />}
        description="Every change to what robots run: deploys, tries on a bench, rollbacks."
      />
      {rollouts.data.length === 0 ? (
        <Empty title="No rollouts yet" icon={<RocketIcon />}>
          <p>Deploy a version from its policy, or from the command line:</p>
          <pre className="code">neura deploy pick --to arm-1</pre>
        </Empty>
      ) : (
        <Card flush>
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Rollouts</caption>
              <thead>
                <tr>
                  <th scope="col">Rollout</th>
                  <th scope="col">State</th>
                  <th scope="col">Robots</th>
                  <th scope="col">Started</th>
                </tr>
              </thead>
              <tbody>
                {rollouts.data.map((rollout) => (
                  <tr key={rollout.metadata.id}>
                    <td>
                      <Link to={`${base}/rollouts/${rollout.metadata.id}`}>
                        {KIND_LABELS[rollout.spec.kind] ?? rollout.spec.kind}: {rollout.status.name}
                      </Link>
                    </td>
                    <td>
                      <Badge tone={rolloutTone(rollout.status.state)}>
                        {rolloutLabel(rollout.status.state)}
                      </Badge>
                    </td>
                    <td>{names(rollout.spec.robot_ids)}</td>
                    <td>
                      <Time iso={rollout.metadata.created_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

function Steps({ rollout }: { rollout: Rollout }) {
  return (
    <ol className="steps" aria-label="Steps">
      {rollout.status.steps.map((step) => (
        <li key={step.name} className={`step ${step.state}`}>
          <Badge tone={stepTone(step.state)}>
            <span aria-hidden="true">{STEP_MARKS[step.state] ?? ""}</span>{" "}
            {STEP_LABELS[step.name] ?? step.name}
          </Badge>
          <span className="visually-hidden">{step.state}</span>
          {step.summary ? <span className="secondary">{step.summary}</span> : null}
        </li>
      ))}
    </ol>
  );
}

export function RolloutPage() {
  const { orgId, projectId, base } = useScope();
  const { rolloutId = "" } = useParams();
  const rollout = useRollout(projectId, rolloutId);
  const robots = useRobots(projectId);
  const sites = useSites(projectId);
  const activity = useFleetActivity(projectId, { rollout_id: rolloutId });
  const can = usePermissions(orgId);
  const cancel = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/rollouts/{rollout_id}/cancel", {
          params: { path: { rollout_id: rolloutId }, header: idempotent() },
        }),
      ),
    [
      ["p", projectId, "rollout", rolloutId],
      ["p", projectId, "rollouts"],
    ],
  );
  if (rollout.isPending) return <Loading />;
  if (rollout.isError) return <ErrorState error={rollout.error} />;
  const r = rollout.data;
  const state = r.status.state;
  const site = sites.data?.find((s) => s.metadata.id === r.status.site_id);
  const names = r.spec.robot_ids
    .map((id) => robots.data?.find((x) => x.metadata.id === id)?.spec.name ?? id)
    .join(", ");
  return (
    <>
      <PageHeader
        title={`${KIND_LABELS[r.spec.kind] ?? r.spec.kind}: ${r.status.name}`}
        icon={<RocketIcon />}
        breadcrumbs={<Link to={`${base}/rollouts`}>Rollouts</Link>}
        meta={<Badge tone={rolloutTone(state)}>{rolloutLabel(state)}</Badge>}
        actions={
          can.deploy && !TERMINAL.has(state) && state !== "switching" ? (
            <button
              type="button"
              className="button"
              disabled={cancel.isPending}
              onClick={() => cancel.mutate(undefined)}
            >
              Cancel
            </button>
          ) : null
        }
      />
      {state === "awaiting_approval" ? (
        <Callout tone="warn" title="Waiting for a person at the site">
          <p>
            On {site?.spec.name ?? "the site"}&apos;s server, run <code>neura approve</code>. No one
            can approve it from the cloud.
          </p>
        </Callout>
      ) : null}
      {state === "stopped" ? (
        <Callout tone="danger" title="Stopped">
          <Reason code={r.status.reason} detail={r.status.message} />
          <p className="secondary">The robots kept running what they ran before.</p>
        </Callout>
      ) : null}
      {cancel.isError ? <ErrorState error={cancel.error} /> : null}
      <div className="grid cols-2">
        <Card title="Steps">
          <Steps rollout={r} />
        </Card>
        <Card title="Details">
          <KeyValue
            items={[
              ["Robots", names],
              ["Site", site ? site.spec.name : r.status.site_id],
              ["Started", <Time key="started" iso={r.metadata.created_at} />],
              ["Finished", <Time key="finished" iso={r.status.finished_at} />],
              ["Approved by", r.status.approved_by ?? "—"],
              ["Note", r.spec.note || "—"],
            ]}
          />
        </Card>
      </div>
      <Card title="What happened">
        {activity.data?.length ? (
          <ul className="stack" style={{ gap: 6, paddingLeft: 0, listStyle: "none" }}>
            {activity.data.map((item) => (
              <li key={item.cursor} className="row" style={{ gap: 10 }}>
                <span className="muted small">
                  <Time iso={item.occurred_at} />
                </span>
                <span className={item.level === "info" ? undefined : "secondary"}>
                  {item.summary}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">Nothing yet.</p>
        )}
      </Card>
    </>
  );
}
