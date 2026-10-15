// Models: registered model artifacts and robot profiles, and which combinations are compatible
// (with the reasons when they are not), checked by the same code the site runs (design §4.4).
import { useQueries } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { api, idempotent, unwrap, type Schemas } from "../api/client";
import {
  useApiMutation,
  useArtifacts,
  useExecutionProfiles,
  useRobotProfiles,
} from "../api/queries";
import {
  KIND_LABELS,
  RegisterContract,
  documentMeta,
  documentTitle,
  parseSignatures,
  type Kind,
  type Registered,
  type Signature,
} from "../components/Contracts";
import { Dialog } from "../components/Dialog";
import { useToasts } from "../components/Toasts";
import { BotIcon, CubeIcon, PlusIcon, SlidersIcon } from "../components/Icons";
import {
  Badge,
  Card,
  Empty,
  ErrorState,
  Field,
  KeyValue,
  Loading,
  PageHeader,
  Reason,
  TabPanel,
  Tabs,
  Time,
  useTab,
  type TabSpec,
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
  const tabs: TabSpec[] = [
    { id: "artifacts", label: "Model artifacts", count: artifacts.data?.length ?? null },
    { id: "robot-profiles", label: "Robot profiles", count: robots.data?.length ?? null },
    { id: "execution-profiles", label: "Execution profiles", count: profiles.data?.length ?? null },
    { id: "compatibility", label: "Compatibility" },
  ];
  const [tab, setTab] = useTab(tabs);
  const primary: Record<string, Kind> = {
    artifacts: "ModelArtifact",
    "robot-profiles": "RobotProfile",
    "execution-profiles": "ExecutionProfile",
    compatibility: "ExecutionProfile",
  };

  return (
    <>
      <PageHeader
        title="Models & contracts"
        icon={<CubeIcon />}
        description="Model artifacts and robot profiles are immutable contract documents identified by their content digest. An execution profile binds one of each and says how to run them. Compatibility is checked by the same code the site runs."
        actions={
          can.deploy ? (
            <>
              {(["ModelArtifact", "RobotProfile", "ExecutionProfile"] as Kind[])
                .filter((k) => k !== primary[tab])
                .map((k) => (
                  <button
                    key={k}
                    type="button"
                    className="button"
                    onClick={() => setRegistering(k)}
                  >
                    Register {KIND_LABELS[k]}
                  </button>
                ))}
              <button
                type="button"
                className="button primary"
                onClick={() => setRegistering(primary[tab] ?? "ModelArtifact")}
              >
                <PlusIcon /> Register {KIND_LABELS[primary[tab] ?? "ModelArtifact"]}
              </button>
            </>
          ) : null
        }
      />
      <Tabs label="Contracts" tabs={tabs} value={tab} onChange={setTab} />
      {tab === "artifacts" ? (
        <TabPanel label="Model artifacts">
          <Card title="Model artifacts" flush>
            {artifacts.isPending ? (
              <Loading />
            ) : artifacts.isError ? (
              <ErrorState error={artifacts.error} />
            ) : artifacts.data.length === 0 ? (
              <Empty title="No model artifacts yet" icon={<CubeIcon />}>
                <p>
                  Register the manifest of your trained policy: weights and image digests, inputs
                  and outputs.
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
                      <th scope="col">Publisher</th>
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
                            <div className="muted small">
                              every {ms(s.outputs?.control_period_us)}
                            </div>
                          </td>
                          <td>
                            code {license(s.licenses?.code)}
                            <div className="muted small">
                              weights {license(s.licenses?.weights)}
                            </div>
                          </td>
                          <td>
                            <Signed signatures={a.signatures} />
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
        </TabPanel>
      ) : null}
      {tab === "robot-profiles" ? (
        <TabPanel label="Robot profiles">
          <Card title="Robot profiles" flush>
            {robots.isPending ? (
              <Loading />
            ) : robots.isError ? (
              <ErrorState error={robots.error} />
            ) : robots.data.length === 0 ? (
              <Empty title="No robot profiles yet">
                <p>
                  A robot profile describes the robot's command interface, state features and
                  cameras.
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
        </TabPanel>
      ) : null}
      {tab === "execution-profiles" ? (
        <TabPanel label="Execution profiles">
          <ExecutionProfilesTable />
        </TabPanel>
      ) : null}
      {tab === "compatibility" ? (
        <TabPanel label="Compatibility">
          <Compatibility
            projectId={projectId}
            base={base}
            artifacts={artifacts.data ?? []}
            robots={robots.data ?? []}
            profiles={profiles.data ?? []}
          />
        </TabPanel>
      ) : null}
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

/** Whether publishers signed the artifact. Sites check the keys against the ones they trust. */
function Signed({ signatures }: { signatures: Signature[] }) {
  if (signatures.length === 0) return <Badge tone="warn">Unsigned</Badge>;
  return (
    <>
      <Badge tone="ok">Signed</Badge>
      {signatures.map((sig) => (
        <div className="muted small" key={sig.key_id}>
          <code>{sig.key_id}</code>
        </div>
      ))}
    </>
  );
}

function Signatures({ artifact, projectId }: { artifact: Artifact; projectId: string }) {
  const { orgId } = useScope();
  const can = usePermissions(orgId);
  const toasts = useToasts();
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const add = useApiMutation(
    async (signature: Signature) =>
      unwrap(
        await api.POST("/v1/model-artifacts/{model_artifact_id}/signatures", {
          params: { path: { model_artifact_id: artifact.metadata.id }, header: idempotent() },
          body: signature,
        }),
      ),
    [
      ["p", projectId, "contract", artifact.metadata.id],
      ["p", projectId, "model-artifacts"],
    ],
  );
  return (
    <Card
      title="Publisher signatures"
      description="A site runs this artifact only if a publisher key its operator trusts signed its digest (neura-agent trust-publisher). Signing never changes the document or its digest."
      actions={
        can.deploy ? (
          <button type="button" className="button" onClick={() => setAdding(true)}>
            Add signature
          </button>
        ) : null
      }
    >
      {artifact.signatures.length === 0 ? (
        <p>
          <Badge tone="warn">Unsigned</Badge> Sites that require signatures will refuse to prepare
          it. Sign it with <code>neura-contracts sign</code> and add the signature here.
        </p>
      ) : (
        <KeyValue
          items={artifact.signatures.map((sig) => [
            <code key="k">{sig.key_id}</code>,
            <code key="p" className="small">
              {sig.public_key}
            </code>,
          ])}
        />
      )}
      <Dialog
        open={adding}
        onClose={() => setAdding(false)}
        title="Add a publisher signature"
        description={
          <>
            Paste the JSON that <code>neura-contracts sign</code> printed for this artifact. It is
            verified against the digest <code>{shortDigest(artifact.digest)}</code>.
          </>
        }
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            let signatures: Signature[];
            try {
              signatures = parseSignatures(text);
              if (signatures.length !== 1) throw new Error("Paste one signature at a time.");
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
              return;
            }
            add.mutate(signatures[0] as Signature, {
              onSuccess: () => {
                toasts.ok("Signature added.");
                setText("");
                setAdding(false);
              },
            });
          }}
        >
          <div className="dialog-body">
            <Field id="signature-text" label="Signature" error={error}>
              <textarea
                id="signature-text"
                value={text}
                spellCheck={false}
                rows={6}
                aria-invalid={error ? true : undefined}
                onChange={(event) => {
                  setText(event.target.value);
                  setError(null);
                }}
              />
            </Field>
            {add.isError ? (
              <ErrorState error={add.error} title="The signature was not accepted" />
            ) : null}
          </div>
          <div className="dialog-footer form-actions">
            <button type="button" className="button" onClick={() => setAdding(false)}>
              Cancel
            </button>
            <button
              type="submit"
              className="button primary"
              disabled={!text.trim() || add.isPending}
            >
              {add.isPending ? "Adding…" : "Add signature"}
            </button>
          </div>
        </form>
      </Dialog>
    </Card>
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

function ExecutionProfilesTable() {
  const { projectId, base } = useScope();
  const profiles = useExecutionProfiles(projectId);
  const artifacts = useArtifacts(projectId);
  const robots = useRobotProfiles(projectId);
  const byDigest = (digest: string | undefined) =>
    [...(artifacts.data ?? []), ...(robots.data ?? [])].find((d) => d.digest === digest);

  return (
    <>
      <Card
        title="Execution profiles"
        description="How a model runs on a robot: timing, chunking, how chunks merge, freshness limits and what the robot does if it starves."
        flush
      >
        {profiles.isPending ? (
          <Loading />
        ) : profiles.isError ? (
          <ErrorState error={profiles.error} />
        ) : profiles.data.length === 0 ? (
          <Empty title="No execution profiles yet" icon={<SlidersIcon />} />
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
  const back =
    k === "ExecutionProfile"
      ? `${base}/models?tab=execution-profiles`
      : k === "RobotProfile"
        ? `${base}/models?tab=robot-profiles`
        : `${base}/models`;
  if (doc.isPending) return <Loading />;
  if (doc.isError) return <ErrorState error={doc.error} />;
  const meta = documentMeta(doc.data);
  return (
    <>
      <PageHeader
        breadcrumbs={<Link to={back}>Models & contracts</Link>}
        icon={
          k === "ExecutionProfile" ? (
            <SlidersIcon />
          ) : k === "RobotProfile" ? (
            <BotIcon />
          ) : (
            <CubeIcon />
          )
        }
        title={documentTitle(doc.data)}
        description={meta.description}
        meta={
          <>
            <Badge tone="accent">{KIND_LABELS[k]}</Badge>
            <span className="tag">{shortDigest(doc.data.digest)}</span>
          </>
        }
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
      {k === "ModelArtifact" && "signatures" in doc.data ? (
        <Signatures artifact={doc.data} projectId={projectId} />
      ) : null}
      <Card
        title="Document"
        description="Exactly as registered. Changing it means registering a new document with a new digest."
      >
        <pre className="code">{JSON.stringify(doc.data.document, null, 2)}</pre>
      </Card>
    </>
  );
}
