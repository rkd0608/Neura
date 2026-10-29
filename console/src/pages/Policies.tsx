// Policies: what robots can run, their versions, and deploying one (design §29).
import { useId, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, idempotent, unwrap, type Schemas } from "../api/client";
import {
  useApiMutation,
  usePolicies,
  usePolicy,
  usePolicyVersions,
  useRobots,
  useSites,
} from "../api/queries";
import { Dialog } from "../components/Dialog";
import { CubeIcon, RocketIcon } from "../components/Icons";
import { Field } from "../components/Field";
import { Badge, Card, Empty, ErrorState, Loading, PageHeader, Time } from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { plural } from "../lib/format";

type Version = Schemas["PolicyVersion"];

const SOURCE_LABELS: Record<string, string> = {
  lerobot: "LeRobot",
  python: "Python",
  container: "Container",
};

const PUSH = "neura policy push ./checkpoint --name pick";

export function Policies() {
  const { projectId, base } = useScope();
  const policies = usePolicies(projectId);
  const robots = useRobots(projectId);
  if (policies.isPending) return <Loading />;
  if (policies.isError) return <ErrorState error={policies.error} />;
  const runningOn = (name: string) =>
    (robots.data ?? []).filter((r) => r.status.policy_name === name).length;
  return (
    <>
      <PageHeader
        title="Policies"
        icon={<CubeIcon />}
        description="What your robots can run. Each push is a new version, signed by the machine that pushed it."
      />
      {policies.data.length === 0 ? (
        <Empty title="No policies yet" icon={<CubeIcon />}>
          <p>
            Push a LeRobot checkpoint, a Python class with <code>@neura.policy</code>, or a
            container image. Its files go to your sites, never to Neura.
          </p>
          <pre className="code">{PUSH}</pre>
        </Empty>
      ) : (
        <Card flush>
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Policies</caption>
              <thead>
                <tr>
                  <th scope="col">Policy</th>
                  <th scope="col">Latest</th>
                  <th scope="col" className="num">
                    Versions
                  </th>
                  <th scope="col">Running on</th>
                </tr>
              </thead>
              <tbody>
                {policies.data.map((policy) => {
                  const on = runningOn(policy.spec.name);
                  return (
                    <tr key={policy.metadata.id}>
                      <td>
                        <Link to={`${base}/policies/${policy.metadata.id}`}>
                          {policy.spec.name}
                        </Link>
                      </td>
                      <td>
                        {policy.status.latest_version ? `v${policy.status.latest_version}` : "—"}
                      </td>
                      <td className="num">{policy.status.versions}</td>
                      <td>{on ? plural(on, "robot") : <span className="muted">No robot</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

export function PolicyPage() {
  const { orgId, projectId, base } = useScope();
  const { policyId = "" } = useParams();
  const policy = usePolicy(projectId, policyId);
  const versions = usePolicyVersions(projectId, policyId);
  const robots = useRobots(projectId);
  const can = usePermissions(orgId, projectId);
  const [deploying, setDeploying] = useState<Version | null>(null);
  if (policy.isPending || versions.isPending) return <Loading />;
  if (policy.isError) return <ErrorState error={policy.error} />;
  if (versions.isError) return <ErrorState error={versions.error} />;
  const robotName = (id: string) => robots.data?.find((r) => r.metadata.id === id)?.spec.name ?? id;
  return (
    <>
      <PageHeader
        title={policy.data.spec.name}
        icon={<CubeIcon />}
        breadcrumbs={<Link to={`${base}/policies`}>Policies</Link>}
        description={policy.data.spec.description || undefined}
      />
      {versions.data.length === 0 ? (
        <Empty title="No versions yet" icon={<CubeIcon />}>
          <pre className="code">{`neura policy push ./checkpoint --name ${policy.data.spec.name}`}</pre>
        </Empty>
      ) : (
        <Card title="Versions" flush>
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Versions of {policy.data.spec.name}</caption>
              <thead>
                <tr>
                  <th scope="col">Version</th>
                  <th scope="col">Source</th>
                  <th scope="col">Added</th>
                  <th scope="col">Running on</th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {versions.data.map((version) => (
                  <tr key={version.metadata.id}>
                    <td>
                      <strong>v{version.status.number}</strong>
                      {version.spec.notes ? (
                        <div className="muted small">{version.spec.notes}</div>
                      ) : null}
                    </td>
                    <td>
                      <span className="row" style={{ gap: 6 }}>
                        {SOURCE_LABELS[version.spec.source.kind] ?? version.spec.source.kind}
                        {version.status.signed ? (
                          <Badge tone="ok">Signed</Badge>
                        ) : (
                          <Badge tone="warn">Unsigned</Badge>
                        )}
                      </span>
                    </td>
                    <td>
                      <Time iso={version.metadata.created_at} />
                    </td>
                    <td>
                      {version.status.running_robot_ids.length ? (
                        version.status.running_robot_ids.map(robotName).join(", ")
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="num">
                      {can.deploy ? (
                        <button
                          type="button"
                          className="button small"
                          onClick={() => setDeploying(version)}
                        >
                          <RocketIcon /> Deploy v{version.status.number}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      {deploying ? <Deploy version={deploying} onClose={() => setDeploying(null)} /> : null}
    </>
  );
}

const SHADOW_CHOICES: [string, number | null][] = [
  ["Default (10 minutes on production sites)", null],
  ["No shadow test", 0],
  ["10 minutes", 600],
  ["1 hour", 3600],
];

function Deploy({ version, onClose }: { version: Version; onClose: () => void }) {
  const { projectId, base } = useScope();
  const sites = useSites(projectId);
  const robots = useRobots(projectId);
  const navigate = useNavigate();
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [shadow, setShadow] = useState(0);
  const [note, setNote] = useState("");
  const noteId = useId();
  const shadowId = useId();
  const create = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/projects/{project_id}/rollouts", {
          params: { path: { project_id: projectId }, header: idempotent() },
          body: {
            spec: {
              kind: "deploy",
              robot_ids: [...chosen],
              policy_version_id: version.metadata.id,
              shadow_s: SHADOW_CHOICES[shadow]?.[1] ?? null,
              note,
            },
          },
        }),
      ),
    [["p", projectId, "rollouts"]],
  );
  const toggle = (id: string) =>
    setChosen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  return (
    <Dialog
      open
      title={`Deploy ${version.status.name}`}
      description="Neura checks it fits, the site prepares and times it, and a person at a production site approves it before robots switch, between tasks."
      onClose={onClose}
    >
      <form
        className="stack"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(undefined, {
            onSuccess: (rollout) => {
              onClose();
              void navigate(`${base}/rollouts/${rollout.metadata.id}`);
            },
          });
        }}
      >
        {(sites.data ?? []).map((site) => {
          const here = (robots.data ?? []).filter((r) => r.spec.site_id === site.metadata.id);
          if (here.length === 0) return null;
          return (
            <fieldset key={site.metadata.id} className="stack" style={{ gap: 6 }}>
              <legend>{site.spec.name}</legend>
              {here.map((robot) => (
                <label key={robot.metadata.id} className="row">
                  <input
                    type="checkbox"
                    checked={chosen.has(robot.metadata.id)}
                    onChange={() => toggle(robot.metadata.id)}
                  />
                  {robot.spec.name}
                  {robot.status.policy_name ? (
                    <span className="muted small">
                      runs {robot.status.policy_name} v{robot.status.policy_version}
                    </span>
                  ) : null}
                </label>
              ))}
            </fieldset>
          );
        })}
        <Field id={shadowId} label="Shadow test">
          <select value={shadow} onChange={(e) => setShadow(Number(e.target.value))}>
            {SHADOW_CHOICES.map(([text], i) => (
              <option key={text} value={i}>
                {text}
              </option>
            ))}
          </select>
        </Field>
        <Field id={noteId} label="Why (for the history)">
          <input value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        {create.isError ? <ErrorState error={create.error} /> : null}
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="button ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={chosen.size === 0 || create.isPending}
          >
            {create.isPending ? "Starting…" : `Deploy to ${plural(chosen.size, "robot")}`}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
