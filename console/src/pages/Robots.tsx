import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, ifMatch, unwrap, type Schemas, idempotent } from "../api/client";
import {
  useApiMutation,
  useDeployments,
  useFaults,
  useRobot,
  useRobotProfiles,
  useRobots,
  useSessions,
  useSites,
} from "../api/queries";
import { Dialog } from "../components/Dialog";
import { useToasts } from "../components/Toasts";
import {
  Card,
  DataAge,
  Empty,
  ErrorState,
  Field,
  KeyValue,
  Loading,
  PageHeader,
  Reason,
  Status,
  Time,
} from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { shortId, slug } from "../lib/format";

function profileName(profile: Schemas["RobotProfile"] | undefined): string {
  const meta = profile?.document.metadata as { name?: string; version?: string } | undefined;
  return meta?.name ? `${meta.name}${meta.version ? ` ${meta.version}` : ""}` : "—";
}

export function Robots() {
  const { orgId, projectId, base } = useScope();
  const robots = useRobots(projectId);
  const sites = useSites(projectId);
  const profiles = useRobotProfiles(projectId);
  const can = usePermissions(orgId);
  const [open, setOpen] = useState(false);
  const siteName = (id: string) =>
    sites.data?.find((s) => s.metadata.id === id)?.spec.display_name ?? shortId(id);
  const connected = (id: string) => sites.data?.find((s) => s.metadata.id === id)?.status.connected;

  return (
    <>
      <PageHeader
        title="Robots"
        description="Each robot belongs to a site and has a robot profile. States are what the site last observed."
        actions={
          can.admin ? (
            <button type="button" className="button primary" onClick={() => setOpen(true)}>
              Add robots
            </button>
          ) : null
        }
      />
      <Card flush>
        {robots.isPending ? (
          <Loading />
        ) : robots.isError ? (
          <ErrorState error={robots.error} />
        ) : robots.data.length === 0 ? (
          <Empty title="No robots yet">
            <p>Register a robot profile under Models first, then add your robots.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Robots</caption>
              <thead>
                <tr>
                  <th scope="col">Robot</th>
                  <th scope="col">Site</th>
                  <th scope="col">State</th>
                  <th scope="col">Active revision</th>
                  <th scope="col">Last report</th>
                </tr>
              </thead>
              <tbody>
                {robots.data.map((robot) => {
                  const stale = connected(robot.spec.site_id) === false;
                  return (
                    <tr key={robot.metadata.id}>
                      <td>
                        <Link to={`${base}/robots/${robot.metadata.id}`}>
                          {robot.spec.display_name}
                        </Link>
                        <div className="muted small mono">{robot.spec.name}</div>
                      </td>
                      <td>{siteName(robot.spec.site_id)}</td>
                      <td>
                        <Status phase={robot.status.phase} stale={stale} />
                        {robot.status.reason ? (
                          <div className="small">
                            <Reason code={robot.status.reason} />
                          </div>
                        ) : null}
                      </td>
                      <td>
                        {robot.status.active_revision_id ? (
                          <code>{shortId(robot.status.active_revision_id)}</code>
                        ) : (
                          <span className="muted">None</span>
                        )}
                      </td>
                      <td>
                        <DataAge
                          ageSeconds={robot.status.data_age_seconds}
                          fetchedAt={robots.dataUpdatedAt}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <AddRobots
        open={open}
        onClose={() => setOpen(false)}
        projectId={projectId}
        sites={sites.data ?? []}
        profiles={profiles.data ?? []}
        base={base}
      />
    </>
  );
}

function AddRobots({
  open,
  onClose,
  projectId,
  sites,
  profiles,
  base,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  sites: Schemas["Site"][];
  profiles: Schemas["RobotProfile"][];
  base: string;
}) {
  const toasts = useToasts();
  const [name, setName] = useState("");
  const [count, setCount] = useState(1);
  const [siteId, setSiteId] = useState("");
  const [profileId, setProfileId] = useState("");
  const site = siteId || sites[0]?.metadata.id || "";
  const profile = profileId || profiles[0]?.metadata.id || "";
  const create = useApiMutation(async () => {
    const base = slug(name) || "robot";
    const created = [];
    for (let i = 0; i < count; i++) {
      const suffix = count > 1 ? `-${i}` : "";
      created.push(
        unwrap(
          await api.POST("/v1/projects/{project_id}/robots", {
            params: { path: { project_id: projectId }, header: idempotent() },
            body: {
              spec: {
                name: `${base}${suffix}`,
                display_name: count > 1 ? `${name.trim()} ${i}` : name.trim(),
                site_id: site,
                robot_profile_id: profile,
              },
            },
          }),
        ),
      );
    }
    return created;
  }, [["p", projectId]]);
  const missing =
    sites.length === 0
      ? "Add a site first."
      : profiles.length === 0
        ? "Register a robot profile under Models first."
        : null;
  return (
    <Dialog open={open} onClose={onClose} title="Add robots">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(undefined, {
            onSuccess: (robots) => {
              toasts.ok(`Added ${robots.length === 1 ? "1 robot" : `${robots.length} robots`}.`);
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          {missing ? (
            <p className="callout warn">
              {missing}{" "}
              <Link to={sites.length === 0 ? `${base}/sites` : `${base}/models`}>Go there</Link>
            </p>
          ) : null}
          <Field
            id="robot-name"
            label="Name"
            hint="Robots at a site authenticate to the gateway with this name."
          >
            <input
              id="robot-name"
              type="text"
              required
              maxLength={190}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field
            id="robot-count"
            label="How many"
            hint={count > 1 ? `Named ${slug(name) || "robot"}-0 … -${count - 1}` : undefined}
          >
            <input
              id="robot-count"
              type="number"
              min={1}
              max={64}
              value={count}
              onChange={(event) => setCount(Math.max(1, Math.min(64, Number(event.target.value))))}
            />
          </Field>
          <Field id="robot-site" label="Site">
            <select
              id="robot-site"
              value={site}
              onChange={(event) => setSiteId(event.target.value)}
            >
              {sites.map((s) => (
                <option key={s.metadata.id} value={s.metadata.id}>
                  {s.spec.display_name}
                </option>
              ))}
            </select>
          </Field>
          <Field id="robot-profile" label="Robot profile">
            <select
              id="robot-profile"
              value={profile}
              onChange={(event) => setProfileId(event.target.value)}
            >
              {profiles.map((p) => (
                <option key={p.metadata.id} value={p.metadata.id}>
                  {profileName(p)}
                </option>
              ))}
            </select>
          </Field>
          {create.isError ? (
            <ErrorState error={create.error} title="Could not add the robots" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!name.trim() || !site || !profile || create.isPending}
          >
            {create.isPending ? "Adding…" : "Add"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

export function RobotPage() {
  const { orgId, projectId, base } = useScope();
  const { robotId = "" } = useParams();
  const navigate = useNavigate();
  const toasts = useToasts();
  const robot = useRobot(projectId, robotId);
  const sites = useSites(projectId);
  const profiles = useRobotProfiles(projectId);
  const sessions = useSessions(projectId);
  const faults = useFaults(projectId);
  const deployments = useDeployments(projectId);
  const can = usePermissions(orgId);
  const [confirming, setConfirming] = useState(false);
  const remove = useApiMutation(
    async (version: string) => {
      await api.DELETE("/v1/robots/{robot_id}", {
        params: { path: { robot_id: robotId }, header: ifMatch(version) },
      });
    },
    [["p", projectId, "robots"]],
  );

  if (robot.isPending) return <Loading />;
  if (robot.isError) return <ErrorState error={robot.error} />;
  const r = robot.data;
  const site = sites.data?.find((s) => s.metadata.id === r.spec.site_id);
  const profile = profiles.data?.find((p) => p.metadata.id === r.spec.robot_profile_id);
  const mine = (sessions.data ?? []).filter((s) => s.robot_id === robotId);
  const myFaults = (faults.data ?? []).filter((f) => f.robot_id === robotId);
  const inDeployments = (deployments.data ?? []).filter((d) => d.spec.robot_ids.includes(robotId));
  const stale = site ? !site.status.connected : false;

  return (
    <>
      <PageHeader
        breadcrumbs={<Link to={`${base}/robots`}>Robots</Link>}
        title={r.spec.display_name}
        description={
          <span className="row">
            <Status phase={r.status.phase} stale={stale} />
            <DataAge ageSeconds={r.status.data_age_seconds} fetchedAt={robot.dataUpdatedAt} />
          </span>
        }
        actions={
          can.admin ? (
            <button type="button" className="button danger" onClick={() => setConfirming(true)}>
              Remove robot
            </button>
          ) : null
        }
      />
      <div className="grid cols-2">
        <Card title="Robot">
          <KeyValue
            items={[
              ["Name", <code key="n">{r.spec.name}</code>],
              [
                "Site",
                site ? (
                  <Link to={`${base}/sites/${site.metadata.id}`}>{site.spec.display_name}</Link>
                ) : (
                  "—"
                ),
              ],
              ["Robot profile", profileName(profile)],
              [
                "Releases",
                inDeployments.length
                  ? inDeployments.map((d) => (
                      <Link
                        key={d.metadata.id}
                        to={`${base}/releases/${d.metadata.id}`}
                        style={{ marginRight: 8 }}
                      >
                        {d.spec.display_name}
                      </Link>
                    ))
                  : "None",
              ],
            ]}
          />
        </Card>
        <Card title="Observed">
          <KeyValue
            items={[
              ["State", <Status key="s" phase={r.status.phase} stale={stale} />],
              [
                "Reason",
                r.status.reason ? <Reason code={r.status.reason} detail={r.status.message} /> : "—",
              ],
              [
                "Active revision",
                r.status.active_revision_id ? <code>{r.status.active_revision_id}</code> : "None",
              ],
              [
                "Lease holder",
                r.status.lease_holder_session_id ? (
                  <code>{r.status.lease_holder_session_id}</code>
                ) : (
                  "None"
                ),
              ],
              ["Task epoch", r.status.task_epoch ?? "—"],
            ]}
          />
        </Card>
      </div>
      <Card
        title="Sessions"
        flush
        description="Each session is one lease on the gateway; a new one starts when the robot reconnects or switches revision."
      >
        {mine.length === 0 ? (
          <p className="card-body muted">No sessions reported.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Session</th>
                  <th scope="col">State</th>
                  <th scope="col">Revision</th>
                  <th scope="col" className="num">
                    Lease generation
                  </th>
                  <th scope="col" className="num">
                    Buffer
                  </th>
                </tr>
              </thead>
              <tbody>
                {mine.map((s) => (
                  <tr key={s.metadata.id}>
                    <td>
                      <code>{shortId(s.metadata.id)}</code>
                    </td>
                    <td>
                      <Status phase={s.state} />
                    </td>
                    <td>
                      <code>{shortId(s.revision_id)}</code>
                    </td>
                    <td className="num">{s.lease_generation}</td>
                    <td className="num">{s.metrics?.buffer_actions ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Card title="Faults" flush>
        {myFaults.length === 0 ? (
          <p className="card-body muted">No faults reported.</p>
        ) : (
          <FaultTable faults={myFaults} />
        )}
      </Card>
      <Dialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Remove ${r.spec.display_name}?`}
        description="The robot leaves every release; the site stops serving it from its next desired state."
      >
        <div className="dialog-body">
          {remove.isError ? (
            <ErrorState error={remove.error} title="Could not remove the robot" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={() => setConfirming(false)}>
            Cancel
          </button>
          <button
            type="button"
            className="button danger"
            disabled={remove.isPending}
            onClick={() =>
              remove.mutate(r.metadata.version, {
                onSuccess: () => {
                  toasts.ok("Robot removed.");
                  void navigate(`${base}/robots`);
                },
              })
            }
          >
            Remove
          </button>
        </div>
      </Dialog>
    </>
  );
}

export function FaultTable({ faults }: { faults: Schemas["Fault"][] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Fault</th>
            <th scope="col">State</th>
            <th scope="col">Local response</th>
            <th scope="col">When</th>
          </tr>
        </thead>
        <tbody>
          {faults.map((f) => (
            <tr key={f.metadata.id}>
              <td>
                <Reason code={f.reason} detail={f.message} />
              </td>
              <td>
                {f.cleared_at ? (
                  <Status phase="Ready" />
                ) : f.latched ? (
                  <Status phase="Faulted" />
                ) : (
                  <Status phase="Waiting" />
                )}
                <div className="muted small">
                  {f.cleared_at
                    ? "Cleared"
                    : f.latched
                      ? "Latched until reset at the site"
                      : "Recovering"}
                </div>
              </td>
              <td>{f.local_response ?? <span className="muted">—</span>}</td>
              <td>
                <Time iso={f.occurred_at} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
