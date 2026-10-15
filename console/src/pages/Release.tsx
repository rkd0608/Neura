// One release: robots with desired, prepared and active revisions shown separately (§19.1),
// revisions with compatibility and site-computed capacity, prepare, activation with approval at
// the site (§20.9), and rollback.
import { useState } from "react";
import { Link, useParams } from "react-router";

import { api, unwrap, type Schemas, idempotent } from "../api/client";
import {
  useActivationRequests,
  useApiMutation,
  useArtifacts,
  useDeployment,
  useExecutionProfiles,
  useOperation,
  useRevisions,
  useRobotProfiles,
  useRobots,
  useSites,
} from "../api/queries";
import { documentTitle } from "../components/Contracts";
import { Dialog } from "../components/Dialog";
import { useToasts } from "../components/Toasts";
import {
  Badge,
  Callout,
  Card,
  Empty,
  ErrorState,
  Field,
  Loading,
  PageHeader,
  Reason,
  Status,
  Time,
} from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { shortId } from "../lib/format";

type Revision = Schemas["Revision"];

export function ReleasePage() {
  const { orgId, projectId, base } = useScope();
  const { deploymentId = "" } = useParams();
  const deployment = useDeployment(projectId, deploymentId);
  const revisions = useRevisions(projectId, deploymentId);
  const requests = useActivationRequests(projectId, deploymentId);
  const robots = useRobots(projectId);
  const sites = useSites(projectId);
  const artifacts = useArtifacts(projectId);
  const robotProfiles = useRobotProfiles(projectId);
  const executionProfiles = useExecutionProfiles(projectId);
  const can = usePermissions(orgId);
  const [dialog, setDialog] = useState<"revision" | "activate" | "rollback" | null>(null);
  const [tracking, setTracking] = useState<string | null>(null);

  if (deployment.isPending) return <Loading />;
  if (deployment.isError) return <ErrorState error={deployment.error} />;
  const d = deployment.data;
  const site = sites.data?.find((s) => s.metadata.id === d.spec.site_id);
  const revisionList = [...(revisions.data ?? [])].reverse();
  const prepared = revisionList.filter((r) => r.status.phase === "Prepared");
  const active = d.status.active_revision_ids ?? {};
  const name = (id: string | null | undefined, list: { metadata: { id: string } }[]) =>
    documentTitle(list.find((x) => x.metadata.id === id) as never);
  const pending = (requests.data ?? []).filter((r) => r.state === "AwaitingLocalApproval");
  const stale = site ? !site.status.connected : false;

  return (
    <>
      <PageHeader
        breadcrumbs={<Link to={`${base}/releases`}>Releases</Link>}
        title={d.spec.display_name}
        description={
          <span className="row">
            <Status phase={d.status.phase} />
            <span className="secondary">
              at{" "}
              {site ? (
                <Link to={`${base}/sites/${site.metadata.id}`}>{site.spec.display_name}</Link>
              ) : (
                "—"
              )}{" "}
              · {d.spec.robot_ids.length} robots ·{" "}
              {d.spec.rollout.strategy === "single_robot_canary"
                ? "canary first"
                : "all at task boundary"}
            </span>
          </span>
        }
        actions={
          can.deploy ? (
            <>
              <button type="button" className="button" onClick={() => setDialog("revision")}>
                New revision
              </button>
              <button
                type="button"
                className="button"
                disabled={revisionList.length < 2}
                onClick={() => setDialog("rollback")}
              >
                Roll back
              </button>
              <button
                type="button"
                className="button primary"
                disabled={prepared.length === 0}
                onClick={() => setDialog("activate")}
              >
                Request activation
              </button>
            </>
          ) : null
        }
      />

      {pending.length > 0 ? (
        <Callout tone="warn" title="Waiting for approval at the site">
          <span>
            Activation of{" "}
            {pending.map((p) => (
              <code key={p.metadata.id}>{shortId(p.revision_id)}</code>
            ))}{" "}
            was requested. A person at the site must approve it there: the cloud cannot start
            motion. Robots switch at their next task boundary after approval.
          </span>
        </Callout>
      ) : null}
      {tracking ? (
        <OperationProgress
          projectId={projectId}
          operationId={tracking}
          onClose={() => setTracking(null)}
        />
      ) : null}

      <Card
        title="Robots"
        description="Observed per robot. A request never shows as running until the site reports it."
        flush
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Robot</th>
                <th scope="col">State</th>
                <th scope="col">Active revision (observed)</th>
                <th scope="col">Prepared at the site</th>
              </tr>
            </thead>
            <tbody>
              {d.spec.robot_ids.map((robotId) => {
                const robot = robots.data?.find((r) => r.metadata.id === robotId);
                const activeId = active[robotId] ?? null;
                return (
                  <tr key={robotId}>
                    <td>
                      <Link to={`${base}/robots/${robotId}`}>
                        {robot?.spec.display_name ?? shortId(robotId)}
                      </Link>
                      {d.spec.rollout.canary_robot_id === robotId ? (
                        <span style={{ marginLeft: 6 }}>
                          <Badge tone="info">Canary</Badge>
                        </span>
                      ) : null}
                    </td>
                    <td>
                      <Status phase={robot?.status.phase} stale={stale} />
                    </td>
                    <td>
                      {activeId ? (
                        <code>{shortId(activeId)}</code>
                      ) : (
                        <span className="muted">None</span>
                      )}
                    </td>
                    <td>
                      {d.status.prepared_revision_id ? (
                        <code>{shortId(d.status.prepared_revision_id)}</code>
                      ) : (
                        <span className="muted">None</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card
        title="Revisions"
        description="Immutable: a model artifact, a robot profile and an execution profile."
        flush
      >
        {revisions.isPending ? (
          <Loading />
        ) : revisionList.length === 0 ? (
          <Empty title="No revisions yet">
            <p>Add a revision with the model and profiles to run.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Revision</th>
                  <th scope="col">Runs</th>
                  <th scope="col">Compatibility</th>
                  <th scope="col">Phase</th>
                  <th scope="col">Capacity at the site</th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {revisionList.map((rev) => (
                  <RevisionRow
                    key={rev.metadata.id}
                    revision={rev}
                    activeCount={
                      Object.values(active).filter((id) => id === rev.metadata.id).length
                    }
                    artifact={name(rev.spec.model_artifact_id, artifacts.data ?? [])}
                    profile={name(rev.spec.execution_profile_id, executionProfiles.data ?? [])}
                    robot={name(rev.spec.robot_profile_id, robotProfiles.data ?? [])}
                    canDeploy={can.deploy}
                    projectId={projectId}
                    deploymentId={deploymentId}
                    onOperation={setTracking}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Activation requests" flush>
        {(requests.data ?? []).length === 0 ? (
          <p className="card-body muted">No activations requested yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Requested</th>
                  <th scope="col">Revision</th>
                  <th scope="col">Robots</th>
                  <th scope="col">State</th>
                  <th scope="col">Approved by (at the site)</th>
                </tr>
              </thead>
              <tbody>
                {[...(requests.data ?? [])].reverse().map((r) => (
                  <tr key={r.metadata.id}>
                    <td>
                      <Time iso={r.metadata.created_at} />
                    </td>
                    <td>
                      <code>{shortId(r.revision_id)}</code>
                    </td>
                    <td>{r.robot_ids.length}</td>
                    <td>
                      <Status phase={r.state} />
                    </td>
                    <td>{r.approved_by ?? <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {dialog === "revision" ? (
        <NewRevision
          projectId={projectId}
          deploymentId={deploymentId}
          artifacts={artifacts.data ?? []}
          robotProfiles={robotProfiles.data ?? []}
          executionProfiles={executionProfiles.data ?? []}
          onClose={() => setDialog(null)}
        />
      ) : null}
      {dialog === "activate" ? (
        <RequestActivation
          deployment={d}
          prepared={prepared}
          robots={robots.data ?? []}
          projectId={projectId}
          onClose={() => setDialog(null)}
          onOperation={setTracking}
        />
      ) : null}
      {dialog === "rollback" ? (
        <Rollback
          deployment={d}
          revisions={revisionList.filter((r) => r.status.phase === "Prepared")}
          projectId={projectId}
          onClose={() => setDialog(null)}
          onOperation={setTracking}
        />
      ) : null}
    </>
  );
}

function RevisionRow({
  revision,
  activeCount,
  artifact,
  profile,
  robot,
  canDeploy,
  projectId,
  deploymentId,
  onOperation,
}: {
  revision: Revision;
  activeCount: number;
  artifact: string;
  profile: string;
  robot: string;
  canDeploy: boolean;
  projectId: string;
  deploymentId: string;
  onOperation: (id: string) => void;
}) {
  const toasts = useToasts();
  const prepare = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/revisions/{revision_id}/prepare", {
          params: { path: { revision_id: revision.metadata.id }, header: idempotent() },
        }),
      ),
    [["p", projectId, "revisions", deploymentId]],
  );
  const compatibility = revision.status.compatibility;
  const capacity = revision.status.capacity;
  return (
    <tr>
      <td>
        <code>{shortId(revision.metadata.id)}</code>
        {activeCount > 0 ? (
          <div style={{ marginTop: 4 }}>
            <Badge tone="ok">Active on {activeCount}</Badge>
          </div>
        ) : null}
        <div className="muted small">
          <Time iso={revision.metadata.created_at} />
        </div>
      </td>
      <td className="small">
        {artifact}
        <div className="muted">
          {profile} · on {robot}
        </div>
      </td>
      <td>
        {compatibility ? (
          compatibility.compatible ? (
            <Badge tone="ok">Compatible</Badge>
          ) : (
            <span className="stack" style={{ gap: 4 }}>
              <Badge tone="danger">{compatibility.issues.length} issues</Badge>
              {compatibility.issues.slice(0, 2).map((issue, i) => (
                <span key={i} className="small">
                  <Reason code={issue.code} detail={issue.path} />
                </span>
              ))}
            </span>
          )
        ) : (
          "—"
        )}
      </td>
      <td>
        <Status phase={revision.status.phase} />
        {revision.status.reason ? (
          <div className="small">
            <Reason code={revision.status.reason} detail={revision.status.message} />
          </div>
        ) : null}
      </td>
      <td className="small">
        {capacity ? (
          <span title={`Limited by ${capacity.limiting_factor.replaceAll("_", " ")}`}>
            <strong>{capacity.max_robots}</strong> robots per batch group
            <div className="muted">limited by {capacity.limiting_factor.replaceAll("_", " ")}</div>
          </span>
        ) : (
          <span className="muted">Estimated when prepared</span>
        )}
      </td>
      <td>
        {canDeploy &&
        revision.status.phase !== "Prepared" &&
        compatibility?.compatible !== false ? (
          <button
            type="button"
            className="button small"
            disabled={prepare.isPending || revision.status.phase === "Preparing"}
            onClick={() =>
              prepare.mutate(undefined, {
                onSuccess: (operation) => {
                  toasts.info("Sent to the site; it verifies the contracts before reporting back.");
                  onOperation(operation.metadata.id);
                },
                onError: (e) => toasts.error(e),
              })
            }
          >
            {revision.status.phase === "Preparing" ? "Preparing…" : "Prepare at site"}
          </button>
        ) : null}
      </td>
    </tr>
  );
}

function OperationProgress({
  projectId,
  operationId,
  onClose,
}: {
  projectId: string;
  operationId: string;
  onClose: () => void;
}) {
  const operation = useOperation(projectId, operationId);
  const o = operation.data;
  const done = o && ["Succeeded", "Failed", "Cancelled"].includes(o.state);
  const tone = !o
    ? "info"
    : o.state === "Succeeded"
      ? "ok"
      : o.state === "Failed"
        ? "danger"
        : "info";
  const what =
    o?.kind === "prepare_revision"
      ? "Preparing at the site"
      : o?.kind === "rollback"
        ? "Rollback"
        : "Activation";
  const cancel = useApiMutation(async () =>
    unwrap(
      await api.POST("/v1/operations/{operation_id}/cancel", {
        params: { path: { operation_id: operationId }, header: idempotent() },
      }),
    ),
  );
  return (
    <div aria-live="polite">
      <Callout tone={tone} title={`${what}: ${o ? o.state : "sending"}`}>
        {o?.reason ? (
          <Reason code={o.reason} detail={o.message} />
        ) : (
          <span className="secondary">{operationText(o)}</span>
        )}
        <span className="row" style={{ marginTop: 6 }}>
          {!done && o ? (
            <button
              type="button"
              className="button small"
              disabled={cancel.isPending}
              onClick={() => cancel.mutate(undefined)}
            >
              Cancel
            </button>
          ) : null}
          <button type="button" className="button small ghost" onClick={onClose}>
            Dismiss
          </button>
        </span>
      </Callout>
    </div>
  );
}

/** What an operation's state means, in words (the site's own message while it is running). */
function operationText(o: Schemas["Operation"] | undefined): string {
  if (!o) return "Sending to the site…";
  if (o.state === "Succeeded") {
    if (o.kind === "prepare_revision")
      return "The site verified the contracts and estimated how many robots it can serve.";
    return "Every robot runs the revision, as the site reports.";
  }
  if (o.state === "Cancelled") return "Cancelled; nothing changed at the site.";
  return o.message || "Waiting for the site to acknowledge it.";
}

function NewRevision({
  projectId,
  deploymentId,
  artifacts,
  robotProfiles,
  executionProfiles,
  onClose,
}: {
  projectId: string;
  deploymentId: string;
  artifacts: Schemas["ModelArtifact"][];
  robotProfiles: Schemas["RobotProfile"][];
  executionProfiles: Schemas["ExecutionProfile"][];
  onClose: () => void;
}) {
  const toasts = useToasts();
  const [executionProfileId, setExecutionProfileId] = useState(
    executionProfiles.at(-1)?.metadata.id ?? "",
  );
  const [notes, setNotes] = useState("");
  const chosen = executionProfiles.find((p) => p.metadata.id === executionProfileId);
  const binding = (
    chosen?.document.spec as
      { binding?: { model_artifact?: string; robot_profile?: string } } | undefined
  )?.binding;
  const artifact = artifacts.find((a) => a.digest === binding?.model_artifact);
  const robot = robotProfiles.find((r) => r.digest === binding?.robot_profile);
  const create = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/deployments/{deployment_id}/revisions", {
          params: { path: { deployment_id: deploymentId }, header: idempotent() },
          body: {
            spec: {
              model_artifact_id: artifact?.metadata.id ?? "",
              robot_profile_id: robot?.metadata.id ?? "",
              execution_profile_id: executionProfileId,
              notes,
            },
          },
        }),
      ),
    [["p", projectId, "revisions", deploymentId]],
  );
  const missing = !artifact || !robot;
  return (
    <Dialog
      open
      onClose={onClose}
      title="New revision"
      description="Pick the execution profile to run. It binds exactly one model artifact and robot profile by digest."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(undefined, {
            onSuccess: (rev) => {
              toasts.ok(
                rev.status.compatibility?.compatible
                  ? "Revision created. Prepare it at the site next."
                  : "Revision created, but it is not compatible; see its issues.",
              );
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          <Field id="rev-profile" label="Execution profile">
            <select
              id="rev-profile"
              value={executionProfileId}
              onChange={(e) => setExecutionProfileId(e.target.value)}
            >
              {executionProfiles.map((p) => (
                <option key={p.metadata.id} value={p.metadata.id}>
                  {documentTitle(p)}
                </option>
              ))}
            </select>
          </Field>
          <dl className="kv">
            <dt>Model artifact</dt>
            <dd>
              {artifact ? (
                documentTitle(artifact)
              ) : (
                <span className="error">Not registered in this project</span>
              )}
            </dd>
            <dt>Robot profile</dt>
            <dd>
              {robot ? (
                documentTitle(robot)
              ) : (
                <span className="error">Not registered in this project</span>
              )}
            </dd>
          </dl>
          <Field
            id="rev-notes"
            label="Notes"
            hint="What changed, for the people reviewing the release."
          >
            <textarea
              id="rev-notes"
              value={notes}
              maxLength={4000}
              style={{ minHeight: 80, fontFamily: "inherit" }}
              onChange={(e) => setNotes(e.target.value)}
            />
          </Field>
          {create.isError ? (
            <ErrorState error={create.error} title="Could not create the revision" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!executionProfileId || missing || create.isPending}
          >
            Create revision
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function RobotPicker({
  deployment,
  robots,
  value,
  onChange,
}: {
  deployment: Schemas["Deployment"];
  robots: Schemas["Robot"][];
  value: string[];
  onChange: (ids: string[]) => void;
}) {
  return (
    <fieldset className="field">
      <legend>Robots</legend>
      <div className="checks">
        {deployment.spec.robot_ids.map((id) => (
          <label key={id}>
            <input
              type="checkbox"
              checked={value.includes(id)}
              onChange={(e) =>
                onChange(e.target.checked ? [...value, id] : value.filter((x) => x !== id))
              }
            />
            {robots.find((r) => r.metadata.id === id)?.spec.display_name ?? shortId(id)}
            {deployment.spec.rollout.canary_robot_id === id ? " (canary)" : ""}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function RequestActivation({
  deployment,
  prepared,
  robots,
  projectId,
  onClose,
  onOperation,
}: {
  deployment: Schemas["Deployment"];
  prepared: Revision[];
  robots: Schemas["Robot"][];
  projectId: string;
  onClose: () => void;
  onOperation: (id: string) => void;
}) {
  const toasts = useToasts();
  const [revisionId, setRevisionId] = useState(prepared[0]?.metadata.id ?? "");
  const defaults =
    deployment.spec.rollout.strategy === "single_robot_canary" &&
    deployment.spec.rollout.canary_robot_id
      ? [deployment.spec.rollout.canary_robot_id]
      : deployment.spec.robot_ids;
  const [robotIds, setRobotIds] = useState<string[]>(defaults);
  const [note, setNote] = useState("");
  const request = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/deployments/{deployment_id}/activation-requests", {
          params: { path: { deployment_id: deployment.metadata.id }, header: idempotent() },
          body: { revision_id: revisionId, robot_ids: robotIds, note },
        }),
      ),
    [["p", projectId]],
  );
  return (
    <Dialog
      open
      onClose={onClose}
      title="Request activation"
      description="The site agent receives the request and asks a person at the site to approve it. Robots switch to the revision at their next task boundary; nothing moves from here."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          request.mutate(undefined, {
            onSuccess: (operation) => {
              toasts.info("Requested. Waiting for approval at the site.");
              onOperation(operation.metadata.id);
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          <Field id="act-revision" label="Prepared revision">
            <select
              id="act-revision"
              value={revisionId}
              onChange={(e) => setRevisionId(e.target.value)}
            >
              {prepared.map((r) => (
                <option key={r.metadata.id} value={r.metadata.id}>
                  {r.metadata.id}
                </option>
              ))}
            </select>
          </Field>
          <RobotPicker
            deployment={deployment}
            robots={robots}
            value={robotIds}
            onChange={setRobotIds}
          />
          <Field id="act-note" label="Note for the site operator">
            <input
              id="act-note"
              type="text"
              maxLength={2000}
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </Field>
          {request.isError ? (
            <ErrorState error={request.error} title="The request was refused" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!revisionId || robotIds.length === 0 || request.isPending}
          >
            Request activation
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function Rollback({
  deployment,
  revisions,
  projectId,
  onClose,
  onOperation,
}: {
  deployment: Schemas["Deployment"];
  revisions: Revision[];
  projectId: string;
  onClose: () => void;
  onOperation: (id: string) => void;
}) {
  const toasts = useToasts();
  const active = new Set(Object.values(deployment.status.active_revision_ids ?? {}));
  const candidates = revisions.filter((r) => !active.has(r.metadata.id));
  const [target, setTarget] = useState(candidates[0]?.metadata.id ?? "");
  const [note, setNote] = useState("");
  const rollback = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/deployments/{deployment_id}/rollbacks", {
          params: { path: { deployment_id: deployment.metadata.id }, header: idempotent() },
          body: { to_revision_id: target, note },
        }),
      ),
    [["p", projectId]],
  );
  return (
    <Dialog
      open
      onClose={onClose}
      title="Roll back"
      description="Return the robots to an earlier prepared revision. Like an activation, it is approved at the site and takes effect at each robot's next task boundary."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          rollback.mutate(undefined, {
            onSuccess: (operation) => {
              toasts.info("Rollback requested. Waiting for approval at the site.");
              onOperation(operation.metadata.id);
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          {candidates.length === 0 ? (
            <Callout tone="info">No earlier prepared revision to roll back to.</Callout>
          ) : (
            <Field id="rb-target" label="Roll back to">
              <select id="rb-target" value={target} onChange={(e) => setTarget(e.target.value)}>
                {candidates.map((r) => (
                  <option key={r.metadata.id} value={r.metadata.id}>
                    {r.metadata.id}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field id="rb-note" label="Reason">
            <input
              id="rb-note"
              type="text"
              maxLength={2000}
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </Field>
          {rollback.isError ? (
            <ErrorState error={rollback.error} title="The rollback was refused" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={!target || rollback.isPending}>
            Request rollback
          </button>
        </div>
      </form>
    </Dialog>
  );
}
