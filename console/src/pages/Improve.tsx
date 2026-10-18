// A deployment's improvement loop (design §28): the episodes its robots recorded (counts only;
// episodes stay at the site), fine-tuning on them at the site, evaluations of a candidate against
// what the robots run, and the release gates that hold a candidate back until it has passed.
import { useState } from "react";

import { api, idempotent, ifMatch, unwrap, type Schemas } from "../api/client";
import { useApiMutation, useEvaluations, useTrainingJobs } from "../api/queries";
import { Dialog } from "../components/Dialog";
import { CpuIcon, GaugeIcon, PlusIcon } from "../components/Icons";
import { useToasts } from "../components/Toasts";
import {
  Badge,
  Callout,
  Card,
  Empty,
  ErrorState,
  Field,
  KeyValue,
  Loading,
  Reason,
  Status,
  Time,
} from "../components/ui";
import type { Tone } from "../lib/phases";
import { plural, shortDigest, shortId } from "../lib/format";

type Deployment = Schemas["Deployment"];
type Revision = Schemas["Revision"];
type Evaluation = Schemas["Evaluation"];
type TrainingJob = Schemas["TrainingJob"];
type Verdict = Schemas["EvaluationVerdict"];
type Outcome = "success" | "failure" | "unknown";

const VERDICT: Record<Verdict, { tone: Tone; label: string }> = {
  superior: { tone: "ok", label: "Better" },
  non_inferior: { tone: "ok", label: "No worse" },
  inconclusive: { tone: "warn", label: "Inconclusive" },
  inferior: { tone: "danger", label: "Worse" },
};

const LIVE = ["Created", "Preparing", "Prepared"];

// The API's defaults, sent explicitly so a request says exactly what the site will run.
const RECIPE = { seed: 1000, num_workers: 2, normalization: "base", log_every: 50 } as const;
const REPLAY = {
  kind: "replay",
  stride: 5,
  max_episodes: 200,
  relative: 0.25,
  absolute: 0.01,
  batch_size: 8,
} as const;
const SIMULATION = {
  kind: "simulation",
  scene: "aloha_transfer_cube",
  first_seed: 1000,
  max_steps: 400,
} as const;
const SHADOW = { kind: "shadow", min_samples: 200, every_nth: 1, tolerance: 0.05 } as const;

type SuiteKind = "replay" | "simulation" | "shadow";

export function ImprovePanel({
  deployment,
  revisions,
  projectId,
  canDeploy,
  onOperation,
}: {
  deployment: Deployment;
  revisions: Revision[];
  projectId: string;
  canDeploy: boolean;
  onOperation: (id: string) => void;
}) {
  const deploymentId = deployment.metadata.id;
  const evaluations = useEvaluations(projectId, deploymentId);
  const jobs = useTrainingJobs(projectId, deploymentId);
  const [dialog, setDialog] = useState<"train" | "evaluate" | null>(null);
  const capture = deployment.status.capture ?? { episodes: 0, outcomes: {} };
  const mode = deployment.spec.capture?.mode ?? "off";
  const active = deployment.status.active_revision_ids ?? {};
  const running = new Set(Object.values(active).filter(Boolean));
  // What the site still has: live revisions, and any a robot runs.
  const available = revisions.filter(
    (r) => LIVE.includes(r.status.phase) || running.has(r.metadata.id),
  );
  const trainedFrom = new Map(
    (jobs.data ?? [])
      .filter((j) => j.status.candidate_revision_id)
      .map((j) => [j.status.candidate_revision_id as string, j]),
  );

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="grid main-side">
        <Card
          title="Recorded episodes"
          description="Recorded by this deployment's robots and kept at the site. Only counts reach Neura."
        >
          <KeyValue
            items={[
              [
                "Recording",
                <span key="mode">
                  {mode === "off" ? "Off" : mode === "all" ? "Every task" : "Failures only"}
                  <span className="muted small"> (the site may allow less)</span>
                </span>,
              ],
              ["Episodes at the site", capture.episodes],
              [
                "By outcome",
                <span key="outcomes" className="row" style={{ gap: 6 }}>
                  {(["success", "failure", "unknown"] as Outcome[]).map((o) => (
                    <Badge
                      key={o}
                      tone={o === "success" ? "ok" : o === "failure" ? "danger" : "muted"}
                    >
                      {capture.outcomes[o] ?? 0} {o}
                    </Badge>
                  ))}
                </span>,
              ],
            ]}
          />
        </Card>
        <ReleaseGates deployment={deployment} projectId={projectId} canDeploy={canDeploy} />
      </div>

      <Card
        title="Fine-tuning"
        description="A revision's model trained further at the site on episodes its robots recorded. The weights never leave the site; the result is a signed artifact with its lineage, and a candidate revision."
        actions={
          canDeploy ? (
            <button
              type="button"
              className="button small"
              disabled={available.length === 0}
              onClick={() => setDialog("train")}
            >
              <PlusIcon /> Fine-tune
            </button>
          ) : null
        }
        flush
      >
        {jobs.isPending ? (
          <Loading />
        ) : jobs.isError ? (
          <ErrorState error={jobs.error} />
        ) : jobs.data.length === 0 ? (
          <Empty title="No fine-tuning yet" icon={<CpuIcon />}>
            <p>Record episodes, then fine-tune the running model on the successful ones.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Started</th>
                  <th scope="col">From</th>
                  <th scope="col">New model</th>
                  <th scope="col">Trained on</th>
                  <th scope="col">Loss</th>
                  <th scope="col">State</th>
                  <th scope="col">Candidate</th>
                </tr>
              </thead>
              <tbody>
                {[...jobs.data].reverse().map((job) => (
                  <TrainingRow key={job.metadata.id} job={job} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card
        title="Evaluations"
        description="A candidate against the revision it would replace, run at the site: in simulation on the same scenes, or on episodes its robots recorded."
        actions={
          canDeploy ? (
            <button
              type="button"
              className="button small"
              disabled={available.length === 0}
              onClick={() => setDialog("evaluate")}
            >
              <PlusIcon /> Evaluate
            </button>
          ) : null
        }
        flush
      >
        {evaluations.isPending ? (
          <Loading />
        ) : evaluations.isError ? (
          <ErrorState error={evaluations.error} />
        ) : evaluations.data.length === 0 ? (
          <Empty title="No evaluations yet" icon={<GaugeIcon />}>
            <p>Evaluate a candidate before it replaces what the robots run.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Started</th>
                  <th scope="col">Suite</th>
                  <th scope="col">Candidate</th>
                  <th scope="col">Against</th>
                  <th scope="col">Result</th>
                  <th scope="col">Verdict</th>
                  <th scope="col">State</th>
                </tr>
              </thead>
              <tbody>
                {[...evaluations.data].reverse().map((evaluation) => (
                  <EvaluationRow
                    key={evaluation.metadata.id}
                    evaluation={evaluation}
                    fineTuned={trainedFrom.has(evaluation.spec.candidate_revision_id)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {dialog === "train" ? (
        <FineTune
          deployment={deployment}
          revisions={available}
          projectId={projectId}
          onClose={() => setDialog(null)}
          onOperation={onOperation}
        />
      ) : null}
      {dialog === "evaluate" ? (
        <Evaluate
          deployment={deployment}
          revisions={available}
          projectId={projectId}
          onClose={() => setDialog(null)}
          onOperation={onOperation}
        />
      ) : null}
    </div>
  );
}

export function TrainingRow({ job }: { job: TrainingJob }) {
  const s = job.status;
  const metrics = (s.metrics ?? {}) as Record<string, unknown>;
  const first = typeof metrics.loss_first === "number" ? metrics.loss_first : null;
  const last = typeof metrics.loss_last === "number" ? metrics.loss_last : null;
  const steps = typeof metrics.steps === "number" ? metrics.steps : job.spec.recipe?.steps;
  return (
    <tr>
      <td>
        <Time iso={job.metadata.created_at} />
      </td>
      <td>
        <span className="tag">{shortId(job.spec.base_revision_id)}</span>
      </td>
      <td>
        <code>
          {job.spec.artifact_name} {job.spec.artifact_version}
        </code>
      </td>
      <td>
        {s.dataset_episodes != null ? (
          <span title={s.dataset_digest ?? undefined}>
            {plural(s.dataset_episodes, "episode")}
            {s.dataset_digest ? (
              <span className="muted small"> · {shortDigest(s.dataset_digest)}</span>
            ) : null}
          </span>
        ) : (
          <span className="muted">{episodeFilter(job.spec.episodes)}</span>
        )}
      </td>
      <td>
        {first !== null && last !== null ? (
          <span>
            {first.toFixed(3)} → {last.toFixed(3)}
            <span className="muted small"> · {steps} steps</span>
          </span>
        ) : (
          <span className="muted">{steps ? `${steps} steps` : "—"}</span>
        )}
      </td>
      <td>
        <Status phase={s.phase} />
        {s.phase === "Running" && s.message ? <div className="muted small">{s.message}</div> : null}
        {s.reason ? <Reason code={s.reason} detail={s.message} /> : null}
      </td>
      <td>
        {s.candidate_revision_id ? (
          <span className="tag">{shortId(s.candidate_revision_id)}</span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
    </tr>
  );
}

function episodeFilter(filter: Schemas["EpisodeFilter"] | undefined): string {
  const outcomes = filter?.outcomes?.length ? filter.outcomes.join(" or ") : "every";
  return `${outcomes} episode${outcomes === "every" ? "" : "s"}`;
}

export function EvaluationRow({
  evaluation,
  fineTuned,
}: {
  evaluation: Evaluation;
  fineTuned: boolean;
}) {
  const s = evaluation.status;
  const suite = evaluation.spec.suite;
  const verdict = s.verdict ? VERDICT[s.verdict] : null;
  return (
    <tr>
      <td>
        <Time iso={evaluation.metadata.created_at} />
      </td>
      <td>
        {suite.kind === "simulation" ? (
          <span>
            Simulation <span className="muted small">· {suite.episodes} scenes</span>
          </span>
        ) : suite.kind === "shadow" ? (
          <span>
            Shadow <span className="muted small">· live observations</span>
          </span>
        ) : (
          <span>
            Replay <span className="muted small">· recorded episodes</span>
          </span>
        )}
      </td>
      <td>
        <span className="tag">{shortId(evaluation.spec.candidate_revision_id)}</span>
        {fineTuned ? (
          <span style={{ marginLeft: 6 }}>
            <Badge tone="accent">Fine-tuned</Badge>
          </span>
        ) : null}
      </td>
      <td>
        {evaluation.spec.baseline_revision_id ? (
          <span className="tag">{shortId(evaluation.spec.baseline_revision_id)}</span>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
      <td>{summary(evaluation)}</td>
      <td>
        {verdict ? (
          <Badge tone={verdict.tone}>{verdict.label}</Badge>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
      <td>
        <Status phase={s.phase} />
        {s.reason ? <Reason code={s.reason} detail={s.message} /> : null}
      </td>
    </tr>
  );
}

function rate(value: unknown): string {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "—";
}

/** The numbers behind a verdict: success rates in simulation, the error on successful episodes
 *  in replay (as a share of each action's range). */
function summary(evaluation: Evaluation) {
  const report = evaluation.status.report as Record<string, Record<string, unknown>> | null;
  if (!report) return <span className="muted">—</span>;
  const episodes = (report as Record<string, unknown>).episodes;
  if (evaluation.spec.suite.kind === "shadow") {
    const mean = report.divergence?.mean;
    const samples = (report as Record<string, unknown>).samples;
    return (
      <span title="Mean distance from the running model's actions, as a share of each command's range">
        {typeof mean === "number" ? `${(mean * 100).toFixed(1)}%` : "—"}
        <span className="muted small">
          {" "}
          apart · {typeof samples === "number" ? samples : 0} live observations
        </span>
      </span>
    );
  }
  if (evaluation.spec.suite.kind === "simulation") {
    return (
      <span>
        {rate(report.candidate?.success_rate)} vs {rate(report.baseline?.success_rate)}
        <span className="muted small"> success</span>
      </span>
    );
  }
  const error = (side: Record<string, unknown> | undefined) => {
    const success = side?.success as { mean?: number } | undefined;
    return typeof success?.mean === "number" ? `${(success.mean * 100).toFixed(1)}%` : "—";
  };
  return (
    <span title="Mean error on successful episodes, as a share of each action's range">
      {error(report.candidate)} vs {error(report.baseline)}
      <span className="muted small">
        {" "}
        error · {typeof episodes === "number" ? episodes : 0} episodes
      </span>
    </span>
  );
}

function ReleaseGates({
  deployment,
  projectId,
  canDeploy,
}: {
  deployment: Deployment;
  projectId: string;
  canDeploy: boolean;
}) {
  const toasts = useToasts();
  const required = deployment.spec.release_gates?.evaluations ?? [];
  const save = useApiMutation(
    async (evaluations: SuiteKind[]) =>
      unwrap(
        await api.PATCH("/v1/deployments/{deployment_id}", {
          params: {
            path: { deployment_id: deployment.metadata.id },
            header: ifMatch(deployment.metadata.version),
          },
          body: { spec: { release_gates: { evaluations } } },
        }),
      ),
    [["p", projectId]],
  );
  const toggle = (kind: SuiteKind) => {
    const next = required.includes(kind) ? required.filter((k) => k !== kind) : [...required, kind];
    save.mutate(next, { onSuccess: () => toasts.info("Release gates saved.") });
  };
  return (
    <Card
      title="Release gates"
      description="Evidence a revision needs before robots switch to it from the one they run. Rollbacks and first activations need none."
    >
      <div className="stack" style={{ gap: 8 }}>
        {(
          [
            ["simulation", "A simulation evaluation, no worse than the running revision"],
            ["replay", "A replay on recorded episodes, no worse than the running revision"],
            ["shadow", "A shadow on live observations, close to the running revision's actions"],
          ] as const
        ).map(([kind, text]) => (
          <label key={kind} className="row" style={{ gap: 8 }}>
            <input
              type="checkbox"
              checked={required.includes(kind)}
              disabled={!canDeploy || save.isPending}
              onChange={() => toggle(kind)}
            />
            <span>{text}</span>
          </label>
        ))}
        {save.isError ? <ErrorState error={save.error} title="Not saved" /> : null}
      </div>
    </Card>
  );
}

function revisionLabel(r: Revision, running: Set<string>): string {
  const state = running.has(r.metadata.id) ? "running" : r.status.phase.toLowerCase();
  return `${r.metadata.id} (${state})`;
}

function FineTune({
  deployment,
  revisions,
  projectId,
  onClose,
  onOperation,
}: {
  deployment: Deployment;
  revisions: Revision[];
  projectId: string;
  onClose: () => void;
  onOperation: (id: string) => void;
}) {
  const toasts = useToasts();
  const running = new Set(
    Object.values(deployment.status.active_revision_ids ?? {}).filter(Boolean) as string[],
  );
  const preferred = revisions.find((r) => running.has(r.metadata.id)) ?? revisions[0];
  const [base, setBase] = useState(preferred?.metadata.id ?? "");
  const [outcomes, setOutcomes] = useState<Outcome[]>(["success"]);
  const [steps, setSteps] = useState(2000);
  const [batchSize, setBatchSize] = useState(8);
  const [version, setVersion] = useState("");
  const train = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/deployments/{deployment_id}/training-jobs", {
          params: { path: { deployment_id: deployment.metadata.id }, header: idempotent() },
          body: {
            spec: {
              base_revision_id: base,
              episodes: { outcomes, complete_only: true },
              recipe: { ...RECIPE, steps, batch_size: batchSize },
              ...(version ? { artifact_version: version } : {}),
            },
          },
        }),
      ),
    [["p", projectId]],
  );
  const recorded = deployment.status.capture?.episodes ?? 0;
  return (
    <Dialog
      open
      onClose={onClose}
      title="Fine-tune at the site"
      description="The site trains on the episodes you choose, on a GPU that serves no robot, and signs the result. It becomes a candidate revision: prepare and evaluate it before activating."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          train.mutate(undefined, {
            onSuccess: (operation) => {
              toasts.info("Training started at the site.");
              onOperation(operation.metadata.id);
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          {recorded === 0 ? (
            <Callout tone="warn">
              The site reports no recorded episodes for this deployment yet.
            </Callout>
          ) : null}
          <Field id="ft-base" label="Start from">
            <select id="ft-base" value={base} onChange={(e) => setBase(e.target.value)}>
              {revisions.map((r) => (
                <option key={r.metadata.id} value={r.metadata.id}>
                  {revisionLabel(r, running)}
                </option>
              ))}
            </select>
          </Field>
          <fieldset className="field">
            <legend>Train on episodes that ended in</legend>
            <div className="row" style={{ gap: 12 }}>
              {(["success", "failure", "unknown"] as Outcome[]).map((o) => (
                <label key={o} className="row" style={{ gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={outcomes.includes(o)}
                    onChange={() =>
                      setOutcomes(
                        outcomes.includes(o) ? outcomes.filter((x) => x !== o) : [...outcomes, o],
                      )
                    }
                  />
                  {o}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="row" style={{ gap: 12 }}>
            <Field id="ft-steps" label="Steps">
              <input
                id="ft-steps"
                type="number"
                min={1}
                max={10_000_000}
                value={steps}
                onChange={(e) => setSteps(Number(e.target.value))}
              />
            </Field>
            <Field id="ft-batch" label="Batch size">
              <input
                id="ft-batch"
                type="number"
                min={1}
                max={4096}
                value={batchSize}
                onChange={(e) => setBatchSize(Number(e.target.value))}
              />
            </Field>
          </div>
          <Field
            id="ft-version"
            label="New model version"
            hint="Optional. By default, the base's version followed by -ft and the job."
          >
            <input
              id="ft-version"
              type="text"
              pattern="[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
            />
          </Field>
          {train.isError ? <ErrorState error={train.error} title="Not started" /> : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!base || outcomes.length === 0 || steps < 1 || train.isPending}
          >
            Start training
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function Evaluate({
  deployment,
  revisions,
  projectId,
  onClose,
  onOperation,
}: {
  deployment: Deployment;
  revisions: Revision[];
  projectId: string;
  onClose: () => void;
  onOperation: (id: string) => void;
}) {
  const toasts = useToasts();
  const running = new Set(
    Object.values(deployment.status.active_revision_ids ?? {}).filter(Boolean) as string[],
  );
  const candidates = [...revisions].reverse();
  const [candidate, setCandidate] = useState(
    candidates.find((r) => !running.has(r.metadata.id))?.metadata.id ??
      candidates[0]?.metadata.id ??
      "",
  );
  const [baseline, setBaseline] = useState("");
  const [kind, setKind] = useState<SuiteKind>("replay");
  const [minutes, setMinutes] = useState(10);
  const [scenes, setScenes] = useState(50);
  const [margin, setMargin] = useState(0.1);
  const evaluate = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/deployments/{deployment_id}/evaluations", {
          params: { path: { deployment_id: deployment.metadata.id }, header: idempotent() },
          body: {
            spec: {
              candidate_revision_id: candidate,
              baseline_revision_id: baseline || null,
              suite:
                kind === "replay"
                  ? { ...REPLAY }
                  : kind === "shadow"
                    ? { ...SHADOW, duration_s: minutes * 60 }
                    : { ...SIMULATION, episodes: scenes, margin },
            },
          },
        }),
      ),
    [["p", projectId]],
  );
  return (
    <Dialog
      open
      onClose={onClose}
      title="Evaluate a candidate"
      description="Runs at the site, never on a GPU that serves robots. The episodes and scenes stay there; the verdict and its numbers come back."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          evaluate.mutate(undefined, {
            onSuccess: (operation) => {
              toasts.info("Evaluation started at the site.");
              onOperation(operation.metadata.id);
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          <Field id="ev-candidate" label="Candidate">
            <select
              id="ev-candidate"
              value={candidate}
              onChange={(e) => setCandidate(e.target.value)}
            >
              {candidates.map((r) => (
                <option key={r.metadata.id} value={r.metadata.id}>
                  {revisionLabel(r, running)}
                </option>
              ))}
            </select>
          </Field>
          <Field id="ev-baseline" label="Against">
            <select id="ev-baseline" value={baseline} onChange={(e) => setBaseline(e.target.value)}>
              <option value="">What the robots run now</option>
              {candidates.map((r) => (
                <option key={r.metadata.id} value={r.metadata.id}>
                  {revisionLabel(r, running)}
                </option>
              ))}
            </select>
          </Field>
          <Field id="ev-kind" label="Suite">
            <select
              id="ev-kind"
              value={kind}
              onChange={(e) => setKind(e.target.value as SuiteKind)}
            >
              <option value="replay">Replay on recorded episodes</option>
              <option value="shadow">Shadow on live observations</option>
              <option value="simulation">Simulation (ALOHA transfer cube)</option>
            </select>
          </Field>
          {kind === "shadow" ? (
            <>
              <Field
                id="ev-minutes"
                label="Minutes"
                hint="How long the candidate shadows the robots running the revision it would replace."
              >
                <input
                  id="ev-minutes"
                  type="number"
                  min={1}
                  max={10_080}
                  value={minutes}
                  onChange={(e) => setMinutes(Number(e.target.value))}
                />
              </Field>
              <p className="muted small">
                The candidate sees what the robots see, in a worker of its own; its actions are
                compared with theirs and never sent to a robot. It skips observations rather than
                slow anything down.
              </p>
            </>
          ) : kind === "simulation" ? (
            <div className="row" style={{ gap: 12 }}>
              <Field id="ev-scenes" label="Scenes">
                <input
                  id="ev-scenes"
                  type="number"
                  min={1}
                  max={10_000}
                  value={scenes}
                  onChange={(e) => setScenes(Number(e.target.value))}
                />
              </Field>
              <Field
                id="ev-margin"
                label="Margin"
                hint="The largest drop in success rate still called no worse."
              >
                <input
                  id="ev-margin"
                  type="number"
                  min={0}
                  max={0.99}
                  step={0.01}
                  value={margin}
                  onChange={(e) => setMargin(Number(e.target.value))}
                />
              </Field>
            </div>
          ) : (
            <p className="muted small">
              Both models predict from what the robots saw, compared with what they then did. It
              catches a candidate that drifts from behavior that worked; it cannot show one is
              better.
            </p>
          )}
          {evaluate.isError ? <ErrorState error={evaluate.error} title="Not started" /> : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!candidate || evaluate.isPending}
          >
            Start evaluation
          </button>
        </div>
      </form>
    </Dialog>
  );
}
