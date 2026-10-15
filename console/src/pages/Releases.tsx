import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { api, unwrap, idempotent } from "../api/client";
import { useApiMutation, useDeployments, useRobots, useSites } from "../api/queries";
import { Dialog } from "../components/Dialog";
import { Card, Empty, ErrorState, Field, Loading, PageHeader, Status } from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { slug } from "../lib/format";

export function Releases() {
  const { orgId, projectId, base } = useScope();
  const deployments = useDeployments(projectId);
  const sites = useSites(projectId);
  const can = usePermissions(orgId);
  const [open, setOpen] = useState(false);

  return (
    <>
      <PageHeader
        title="Releases"
        description="A release puts a model on a set of robots at one site. Each change is a new revision: prepared and verified at the site, then activated with approval by a person at the site."
        actions={
          can.deploy ? (
            <button type="button" className="button primary" onClick={() => setOpen(true)}>
              New release
            </button>
          ) : null
        }
      />
      <Card flush>
        {deployments.isPending ? (
          <Loading />
        ) : deployments.isError ? (
          <ErrorState error={deployments.error} />
        ) : deployments.data.length === 0 ? (
          <Empty title="No releases yet">
            <p>Create a release for the robots at a site; then add a revision with your model.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Releases</caption>
              <thead>
                <tr>
                  <th scope="col">Release</th>
                  <th scope="col">Site</th>
                  <th scope="col">Phase</th>
                  <th scope="col">Rollout</th>
                  <th scope="col" className="num">
                    Robots active
                  </th>
                </tr>
              </thead>
              <tbody>
                {deployments.data.map((d) => {
                  const active = Object.values(d.status.active_revision_ids ?? {}).filter(
                    Boolean,
                  ).length;
                  return (
                    <tr key={d.metadata.id}>
                      <td>
                        <Link to={`${base}/releases/${d.metadata.id}`}>{d.spec.display_name}</Link>
                      </td>
                      <td>
                        {sites.data?.find((s) => s.metadata.id === d.spec.site_id)?.spec
                          .display_name ?? "—"}
                      </td>
                      <td>
                        <Status phase={d.status.phase} />
                      </td>
                      <td>
                        {d.spec.rollout.strategy === "single_robot_canary"
                          ? "Canary first"
                          : "All at task boundary"}
                      </td>
                      <td className="num">
                        {active} / {d.spec.robot_ids.length}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <NewRelease open={open} onClose={() => setOpen(false)} projectId={projectId} base={base} />
    </>
  );
}

function NewRelease({
  open,
  onClose,
  projectId,
  base,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  base: string;
}) {
  const navigate = useNavigate();
  const sites = useSites(projectId);
  const robots = useRobots(projectId);
  const [name, setName] = useState("");
  const [siteId, setSiteId] = useState("");
  const [picked, setPicked] = useState<string[] | null>(null);
  const [strategy, setStrategy] = useState<"all_at_task_boundary" | "single_robot_canary">(
    "all_at_task_boundary",
  );
  const [canary, setCanary] = useState("");
  const site = siteId || sites.data?.[0]?.metadata.id || "";
  const atSite = (robots.data ?? []).filter((r) => r.spec.site_id === site);
  const chosen = picked ?? atSite.map((r) => r.metadata.id);
  const canaryId = canary || chosen[0] || "";
  const create = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/projects/{project_id}/deployments", {
          params: { path: { project_id: projectId }, header: idempotent() },
          body: {
            spec: {
              name: slug(name) || "release",
              display_name: name.trim(),
              site_id: site,
              robot_ids: chosen,
              rollout:
                strategy === "single_robot_canary"
                  ? { strategy, canary_robot_id: canaryId }
                  : { strategy },
            },
          },
        }),
      ),
    [["p", projectId, "deployments"]],
  );

  return (
    <Dialog open={open} onClose={onClose} title="New release">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(undefined, {
            onSuccess: (d) => {
              onClose();
              void navigate(`${base}/releases/${d.metadata.id}`);
            },
          });
        }}
      >
        <div className="dialog-body">
          <Field id="release-name" label="Name">
            <input
              id="release-name"
              type="text"
              required
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field id="release-site" label="Site">
            <select
              id="release-site"
              value={site}
              onChange={(e) => {
                setSiteId(e.target.value);
                setPicked(null);
              }}
            >
              {(sites.data ?? []).map((s) => (
                <option key={s.metadata.id} value={s.metadata.id}>
                  {s.spec.display_name}
                </option>
              ))}
            </select>
          </Field>
          <fieldset className="field">
            <legend>Robots</legend>
            {atSite.length === 0 ? (
              <span className="hint">No robots at this site yet.</span>
            ) : (
              <div className="checks">
                {atSite.map((r) => (
                  <label key={r.metadata.id}>
                    <input
                      type="checkbox"
                      checked={chosen.includes(r.metadata.id)}
                      onChange={(e) =>
                        setPicked(
                          e.target.checked
                            ? [...chosen, r.metadata.id]
                            : chosen.filter((id) => id !== r.metadata.id),
                        )
                      }
                    />
                    {r.spec.display_name}
                  </label>
                ))}
              </div>
            )}
          </fieldset>
          <fieldset className="field">
            <legend>Rollout</legend>
            <div className="checks">
              <label>
                <input
                  type="radio"
                  name="strategy"
                  checked={strategy === "all_at_task_boundary"}
                  onChange={() => setStrategy("all_at_task_boundary")}
                />
                All robots, each at its next task boundary
              </label>
              <label>
                <input
                  type="radio"
                  name="strategy"
                  checked={strategy === "single_robot_canary"}
                  onChange={() => setStrategy("single_robot_canary")}
                />
                One canary robot first
              </label>
            </div>
          </fieldset>
          {strategy === "single_robot_canary" ? (
            <Field id="release-canary" label="Canary robot">
              <select
                id="release-canary"
                value={canaryId}
                onChange={(e) => setCanary(e.target.value)}
              >
                {chosen.map((id) => (
                  <option key={id} value={id}>
                    {atSite.find((r) => r.metadata.id === id)?.spec.display_name ?? id}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
          {create.isError ? (
            <ErrorState error={create.error} title="Could not create the release" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!name.trim() || chosen.length === 0 || create.isPending}
          >
            Create release
          </button>
        </div>
      </form>
    </Dialog>
  );
}
