// Models: registered model artifacts and robot profiles, and which combinations are compatible
// (with the reasons when they are not), checked by the same code the site runs (design §4.4).
import { useQueries } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { useArtifacts, useExecutionProfiles, useRobotProfiles } from "../api/queries";
import {
  KIND_LABELS,
  RegisterContract,
  documentMeta,
  documentTitle,
  type Kind,
  type Registered,
} from "../components/Contracts";
import {
  Badge,
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
import { shortDigest } from "../lib/format";

type Artifact = Schemas["ModelArtifact"];
type RobotProfile = Schemas["RobotProfile"];
type ExecutionProfile = Schemas["ExecutionProfile"];

interface ArtifactSpec {
  adapter?: { name?: string; version?: string };
  licenses?: {
    code?: { spdx_id?: string; name?: string };
    weights?: { spdx_id?: string; name?: string };
  };
  inputs?: { kind: string; name: string }[];
  outputs?: { chunk_length?: number; control_period_us?: number; features?: unknown[] };
}

interface ProfileSpec {
  binding?: { model_artifact?: string; robot_profile?: string };
  control_period_us?: number;
  chunk?: { actions_per_chunk?: number; refill_threshold_actions?: number };
  merge?: { mode?: string };
  freshness?: {
    max_observation_age_at_dispatch_us?: number;
    max_observation_age_at_acceptance_us?: number;
  };
  starvation?: { response?: string; grace_us?: number };
  runtime?: { device?: string; precision?: string };
}

// Contract specs are validated server-side; the console reads the fields it displays.
// eslint-disable-next-line @typescript-eslint/no-unnecessary-type-parameters -- a typed view
const spec = <T,>(doc: Registered): T => doc.document.spec as T;
const ms = (us: number | undefined) =>
  us === undefined ? "—" : `${(us / 1000).toFixed(us % 1000 ? 1 : 0)} ms`;

function license(ref: { spdx_id?: string; name?: string } | undefined): string {
  return ref?.spdx_id ?? ref?.name ?? "—";
}

export function Models() {
  const { orgId, projectId, base } = useScope();
  const artifacts = useArtifacts(projectId);
  const robots = useRobotProfiles(projectId);
  const profiles = useExecutionProfiles(projectId);
  const can = usePermissions(orgId);
  const [registering, setRegistering] = useState<Kind | null>(null);

  return (
    <>
      <PageHeader
        title="Models"
        description="Model artifacts and robot profiles are immutable contract documents identified by their content digest. An execution profile binds one of each and says how to run them."
        actions={
          can.deploy ? (
            <>
              <button
                type="button"
                className="button"
                onClick={() => setRegistering("RobotProfile")}
              >
                Register robot profile
              </button>
              <button
                type="button"
                className="button primary"
                onClick={() => setRegistering("ModelArtifact")}
              >
                Register model artifact
              </button>
            </>
          ) : null
        }
      />
      <Card title="Model artifacts" flush>
        {artifacts.isPending ? (
          <Loading />
        ) : artifacts.isError ? (
          <ErrorState error={artifacts.error} />
        ) : artifacts.data.length === 0 ? (
          <Empty title="No model artifacts yet">
            <p>
              Register the manifest of your trained policy: weights and image digests, inputs and
              outputs.
            </p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Artifact</th>
                  <th scope="col">Adapter</th>
                  <th scope="col">Outputs</th>
                  <th scope="col">Licenses</th>
                  <th scope="col">Digest</th>
                </tr>
              </thead>
              <tbody>
                {artifacts.data.map((a) => {
                  const s = spec<ArtifactSpec>(a);
                  return (
                    <tr key={a.metadata.id}>
                      <td>
                        <Link to={`${base}/models/ModelArtifact/${a.metadata.id}`}>
                          {documentTitle(a)}
                        </Link>
                        <div className="muted small">{documentMeta(a).description}</div>
                      </td>
                      <td>
                        {s.adapter ? `${s.adapter.name ?? ""} ${s.adapter.version ?? ""}` : "—"}
                      </td>
                      <td>
                        {s.outputs?.chunk_length ?? "—"} actions ×{" "}
                        {s.outputs?.features?.length ?? "—"} features
                        <div className="muted small">every {ms(s.outputs?.control_period_us)}</div>
                      </td>
                      <td>
                        code {license(s.licenses?.code)}
                        <div className="muted small">weights {license(s.licenses?.weights)}</div>
                      </td>
                      <td>
                        <code>{shortDigest(a.digest)}</code>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Card title="Robot profiles" flush>
        {robots.isPending ? (
          <Loading />
        ) : robots.isError ? (
          <ErrorState error={robots.error} />
        ) : robots.data.length === 0 ? (
          <Empty title="No robot profiles yet">
            <p>
              A robot profile describes the robot's command interface, state features and cameras.
            </p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Robot profile</th>
                  <th scope="col">Control period</th>
                  <th scope="col">Digest</th>
                </tr>
              </thead>
              <tbody>
                {robots.data.map((r) => (
                  <tr key={r.metadata.id}>
                    <td>
                      <Link to={`${base}/models/RobotProfile/${r.metadata.id}`}>
                        {documentTitle(r)}
                      </Link>
                      <div className="muted small">{documentMeta(r).description}</div>
                    </td>
                    <td>{ms(spec<{ control_period_us?: number }>(r).control_period_us)}</td>
                    <td>
                      <code>{shortDigest(r.digest)}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Compatibility
        projectId={projectId}
        base={base}
        artifacts={artifacts.data ?? []}
        robots={robots.data ?? []}
        profiles={profiles.data ?? []}
      />
      {registering ? (
        <RegisterContract
          kind={registering}
          open
          onClose={() => setRegistering(null)}
          projectId={projectId}
        />
      ) : null}
    </>
  );
}

function Compatibility({
  projectId,
  base,
  artifacts,
  robots,
  profiles,
}: {
  projectId: string;
  base: string;
  artifacts: Artifact[];
  robots: RobotProfile[];
  profiles: ExecutionProfile[];
}) {
  const pairs = artifacts.flatMap((a) =>
    robots.map((r) => ({
      artifact: a,
      robot: r,
      profile: profiles.find((p) => {
        const binding = spec<ProfileSpec>(p).binding;
        return binding?.model_artifact === a.digest && binding.robot_profile === r.digest;
      }),
    })),
  );
  const checks = useQueries({
    queries: pairs.map(({ artifact, robot, profile }) => {
      const request: Schemas["CompatibilityCheckRequest"] | null = profile
        ? {
            model_artifact_id: artifact.metadata.id,
            robot_profile_id: robot.metadata.id,
            execution_profile_id: profile.metadata.id,
          }
        : null;
      return {
        queryKey: ["p", projectId, "compatibility", request],
        enabled: request !== null,
        staleTime: 60_000,
        queryFn: async () =>
          unwrap(
            await api.POST("/v1/projects/{project_id}/compatibility-checks", {
              params: { path: { project_id: projectId } },
              body: request as Schemas["CompatibilityCheckRequest"],
            }),
          ),
      };
    }),
  });
  const [selected, setSelected] = useState<number | null>(null);
  if (artifacts.length === 0 || robots.length === 0) return null;
  const detail = selected === null ? null : { pair: pairs[selected], check: checks[selected] };

  return (
    <Card
      title="Compatibility"
      description="Each model artifact against each robot profile, through the execution profile that binds them. Nothing is converted implicitly: every mismatch is listed with its reason."
      flush
    >
      <div className="table-wrap">
        <table>
          <caption className="visually-hidden">
            Compatibility of model artifacts and robot profiles
          </caption>
          <thead>
            <tr>
              <th scope="col">Model artifact</th>
              {robots.map((r) => (
                <th scope="col" key={r.metadata.id}>
                  {documentTitle(r)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {artifacts.map((a, ai) => (
              <tr key={a.metadata.id}>
                <th
                  scope="row"
                  style={{ background: "transparent", fontWeight: 500, color: "var(--text)" }}
                >
                  {documentTitle(a)}
                </th>
                {robots.map((r, ri) => {
                  const index = ai * robots.length + ri;
                  const pair = pairs[index];
                  const check = checks[index];
                  let cell;
                  if (!pair?.profile)
                    cell = <span className="muted">No execution profile binds these</span>;
                  else if (!check || check.isPending)
                    cell = <span className="muted">Checking…</span>;
                  else if (check.isError) cell = <Badge tone="danger">Check failed</Badge>;
                  else if (check.data.compatible) cell = <Badge tone="ok">Compatible</Badge>;
                  else cell = <Badge tone="danger">{check.data.issues.length} issues</Badge>;
                  return (
                    <td key={r.metadata.id}>
                      <div className="row">
                        {cell}
                        {pair?.profile ? (
                          <button
                            type="button"
                            className="button small ghost"
                            onClick={() => setSelected(index)}
                          >
                            Details
                            <span className="visually-hidden">
                              {" "}
                              for {documentTitle(a)} on {documentTitle(r)}
                            </span>
                          </button>
                        ) : null}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail?.pair?.profile ? (
        <div className="card-body stack" aria-live="polite">
          <h3>
            {documentTitle(detail.pair.artifact)} on {documentTitle(detail.pair.robot)} with{" "}
            <Link to={`${base}/models/ExecutionProfile/${detail.pair.profile.metadata.id}`}>
              {documentTitle(detail.pair.profile)}
            </Link>
          </h3>
          {detail.check?.data ? (
            detail.check.data.issues.length === 0 ? (
              <p>No issues: the site will accept this combination.</p>
            ) : (
              <ul className="stack" style={{ margin: 0, paddingLeft: 18 }}>
                {detail.check.data.issues.map((issue, i) => (
                  <li key={i}>
                    <Reason code={issue.code} detail={`${issue.path}: ${issue.message}`} />
                  </li>
                ))}
              </ul>
            )
          ) : detail.check?.isError ? (
            <ErrorState error={detail.check.error} />
          ) : (
            <Loading />
          )}
        </div>
      ) : null}
    </Card>
  );
}

export function ExecutionProfiles() {
  const { orgId, projectId, base } = useScope();
  const profiles = useExecutionProfiles(projectId);
  const artifacts = useArtifacts(projectId);
  const robots = useRobotProfiles(projectId);
  const can = usePermissions(orgId);
  const [registering, setRegistering] = useState(false);
  const byDigest = (digest: string | undefined) =>
    [...(artifacts.data ?? []), ...(robots.data ?? [])].find((d) => d.digest === digest);

  return (
    <>
      <PageHeader
        title="Execution profiles"
        description="How a model runs on a robot: timing, chunking, how chunks merge, freshness limits and what the robot does if it starves."
        actions={
          can.deploy ? (
            <button type="button" className="button primary" onClick={() => setRegistering(true)}>
              Register execution profile
            </button>
          ) : null
        }
      />
      <Card flush>
        {profiles.isPending ? (
          <Loading />
        ) : profiles.isError ? (
          <ErrorState error={profiles.error} />
        ) : profiles.data.length === 0 ? (
          <Empty title="No execution profiles yet" />
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Profile</th>
                  <th scope="col">Binds</th>
                  <th scope="col">Timing</th>
                  <th scope="col">Merge</th>
                  <th scope="col">Freshness</th>
                  <th scope="col">If starved</th>
                </tr>
              </thead>
              <tbody>
                {profiles.data.map((p) => {
                  const s = spec<ProfileSpec>(p);
                  return (
                    <tr key={p.metadata.id}>
                      <td>
                        <Link to={`${base}/models/ExecutionProfile/${p.metadata.id}`}>
                          {documentTitle(p)}
                        </Link>
                        <div className="muted small">{documentMeta(p).description}</div>
                      </td>
                      <td className="small">
                        {documentTitle(byDigest(s.binding?.model_artifact))}
                        <div className="muted">
                          on {documentTitle(byDigest(s.binding?.robot_profile))}
                        </div>
                      </td>
                      <td className="small">
                        every {ms(s.control_period_us)}
                        <div className="muted">
                          chunks of {s.chunk?.actions_per_chunk}, refill at{" "}
                          {s.chunk?.refill_threshold_actions}
                        </div>
                      </td>
                      <td>{s.merge?.mode ?? "—"}</td>
                      <td className="small">
                        {ms(s.freshness?.max_observation_age_at_dispatch_us)} at dispatch
                        <div className="muted">
                          {ms(s.freshness?.max_observation_age_at_acceptance_us)} at acceptance
                        </div>
                      </td>
                      <td className="small">
                        {s.starvation?.response?.replaceAll("_", " ") ?? "—"}
                        <div className="muted">after {ms(s.starvation?.grace_us)}</div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <RegisterContract
        kind="ExecutionProfile"
        open={registering}
        onClose={() => setRegistering(false)}
        projectId={projectId}
      />
    </>
  );
}

const DETAIL_PATHS = {
  ModelArtifact: "/v1/model-artifacts/{model_artifact_id}",
  RobotProfile: "/v1/robot-profiles/{robot_profile_id}",
  ExecutionProfile: "/v1/execution-profiles/{execution_profile_id}",
} as const;

export function ContractPage() {
  const { projectId, base } = useScope();
  const { kind = "ModelArtifact", contractId = "" } = useParams();
  const k = (kind in DETAIL_PATHS ? kind : "ModelArtifact") as Kind;
  const [doc] = useQueries({
    queries: [
      {
        queryKey: ["p", projectId, "contract", contractId],
        queryFn: async (): Promise<Registered> => {
          if (k === "ModelArtifact") {
            return unwrap(
              await api.GET(DETAIL_PATHS.ModelArtifact, {
                params: { path: { model_artifact_id: contractId } },
              }),
            );
          }
          if (k === "RobotProfile") {
            return unwrap(
              await api.GET(DETAIL_PATHS.RobotProfile, {
                params: { path: { robot_profile_id: contractId } },
              }),
            );
          }
          return unwrap(
            await api.GET(DETAIL_PATHS.ExecutionProfile, {
              params: { path: { execution_profile_id: contractId } },
            }),
          );
        },
      },
    ],
  });
  const back = k === "ExecutionProfile" ? `${base}/execution-profiles` : `${base}/models`;
  if (doc.isPending) return <Loading />;
  if (doc.isError) return <ErrorState error={doc.error} />;
  const meta = documentMeta(doc.data);
  return (
    <>
      <PageHeader
        breadcrumbs={
          <Link to={back}>{k === "ExecutionProfile" ? "Execution profiles" : "Models"}</Link>
        }
        title={documentTitle(doc.data)}
        description={meta.description}
      />
      <Card title={`About this ${KIND_LABELS[k]}`}>
        <KeyValue
          items={[
            ["Digest", <code key="d">{doc.data.digest}</code>],
            ["Identifier", <code key="i">{doc.data.metadata.id}</code>],
            ["Registered", <Time key="t" iso={doc.data.metadata.created_at} />],
            ["Registered by", doc.data.metadata.created_by ?? "—"],
          ]}
        />
      </Card>
      <Card
        title="Document"
        description="Exactly as registered. Changing it means registering a new document with a new digest."
      >
        <pre className="mono" style={{ margin: 0, overflowX: "auto" }}>
          {JSON.stringify(doc.data.document, null, 2)}
        </pre>
      </Card>
    </>
  );
}
