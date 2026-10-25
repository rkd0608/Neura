// Deployments: every deployment in the project, filtered and searched, and the dialog that
// creates one (design §19.3).
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { api, unwrap, idempotent } from "../api/client";
import { useApiMutation, useDeployments, useRobots, useSites } from "../api/queries";
import { Dialog } from "../components/Dialog";
import { PlusIcon, RocketIcon } from "../components/Icons";
import {
  Card,
  Empty,
  ErrorState,
  Field,
  Loading,
  Meter,
  PageHeader,
  SearchInput,
  Segmented,
  Status,
} from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { ago, secondsSince, slug } from "../lib/format";
import { useNow } from "../lib/useNow";

export function Releases() {
  const { orgId, projectId, base } = useScope();
  const deployments = useDeployments(projectId);
  const sites = useSites(projectId);
  const can = usePermissions(orgId);
  const navigate = useNavigate();
  const [search, setSearch] = useSearchParams();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const open = search.has("new");
  const setOpen = (value: boolean) =>
    setSearch(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set("new", "1");
        else next.delete("new");
        return next;
      },
      { replace: true },
    );
  const now = useNow(30_000);

  const list = (deployments.data ?? []).filter((d) => {
    const site = sites.data?.find((s) => s.metadata.id === d.spec.site_id);
    const text =
      `${d.spec.display_name} ${d.spec.name} ${site?.spec.display_name ?? ""}`.toLowerCase();
    if (query && !text.includes(query.toLowerCase())) return false;
    return FILTERS[filter](d.status.phase);
  });

  return (
    <>
      <PageHeader
        title="Deployments"
        icon={<RocketIcon />}
        description="A deployment puts a model on a set of robots at one site. Every change is a new immutable revision: prepared and verified at the site, then activated with approval by a person at the site."
        actions={
          can.deploy ? (
            <button type="button" className="button primary" onClick={() => setOpen(true)}>
              <PlusIcon /> New deployment
            </button>
          ) : null
        }
      />
      <div className="toolbar">
        <SearchInput
          value={query}
          onChange={setQuery}
          placeholder="Filter by name or site"
          label="Filter deployments"
        />
        <Segmented
          label="Show"
          value={filter}
          onChange={setFilter}
          options={[
            { id: "all", label: "All" },
            { id: "active", label: "Active" },
            { id: "progress", label: "In progress" },
            { id: "attention", label: "Needs attention" },
          ]}
        />
        <span className="grow" />
        <span className="muted small">
          {list.length} of {deployments.data?.length ?? 0}
        </span>
      </div>
      <Card flush>
        {deployments.isPending ? (
          <Loading />
        ) : deployments.isError ? (
          <ErrorState error={deployments.error} />
        ) : deployments.data.length === 0 ? (
          <Empty
            title="No deployments yet"
            icon={<RocketIcon />}
            actions={
              can.deploy ? (
                <button
                  type="button"
                  className="button primary small"
                  onClick={() => setOpen(true)}
                >
                  New deployment
                </button>
              ) : null
            }
          >
            <p>
              Create a deployment for the robots at a site; then add a revision with your model.
            </p>
          </Empty>
        ) : list.length === 0 ? (
          <Empty title="Nothing matches">
            <p>No deployment matches this filter.</p>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <caption className="visually-hidden">Deployments</caption>
              <thead>
                <tr>
                  <th scope="col">Deployment</th>
                  <th scope="col">Status</th>
                  <th scope="col">Site</th>
                  <th scope="col">On the active revision</th>
                  <th scope="col">Rollout</th>
                  <th scope="col" className="num">
                    Created
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.map((d) => {
                  const active = Object.values(d.status.active_revision_ids ?? {}).filter(
                    Boolean,
                  ).length;
                  const site = sites.data?.find((s) => s.metadata.id === d.spec.site_id);
                  const to = `${base}/deployments/${d.metadata.id}`;
                  return (
                    <tr
                      key={d.metadata.id}
                      className="clickable"
                      onClick={(event) => {
                        if ((event.target as HTMLElement).closest("a")) return;
                        void navigate(to);
                      }}
                    >
                      <td>
                        <Link to={to}>{d.spec.display_name}</Link>
                        <span className="sub mono">{d.spec.name}</span>
                      </td>
                      <td>
                        <Status
                          phase={d.status.phase}
                          stale={site ? !site.status.connected : false}
                        />
                      </td>
                      <td>
                        {site ? (
                          <Link to={`${base}/sites/${site.metadata.id}`}>
                            {site.spec.display_name}
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td style={{ minWidth: 170 }}>
                        <div className="spread small" style={{ marginBottom: 4 }}>
                          <span className="secondary">robots</span>
                          <span className="num">
                            {active} / {d.spec.robot_ids.length}
                          </span>
                        </div>
                        <Meter
                          value={active}
                          max={d.spec.robot_ids.length}
                          tone="ok"
                          label={`${active} of ${d.spec.robot_ids.length} robots on the active revision`}
                        />
                      </td>
                      <td className="secondary">
                        {d.spec.rollout.strategy === "single_robot_canary"
                          ? "Canary first"
                          : "All at task boundary"}
                      </td>
                      <td className="num muted small" title={d.metadata.created_at}>
                        {ago(secondsSince(d.metadata.created_at, now))}
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

type Filter = "all" | "active" | "progress" | "attention";
const FILTERS: Record<Filter, (phase: string) => boolean> = {
  all: () => true,
  active: (phase) => phase === "Active",
  progress: (phase) => ["Preparing", "Activating", "RollingBack", "Pending"].includes(phase),
  attention: (phase) => ["NeedsAttention", "Failed"].includes(phase),
};

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
              name: slug(name) || "deployment",
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
    <Dialog
      open={open}
      onClose={onClose}
      title="New deployment"
      description="Choose the site and its robots. You add the model as a revision next; nothing runs until it is prepared at the site and approved there."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(undefined, {
            onSuccess: (d) => {
              onClose();
              void navigate(`${base}/deployments/${d.metadata.id}`);
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
            <ErrorState error={create.error} title="Could not create the deployment" />
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
            Create deployment
          </button>
        </div>
      </form>
    </Dialog>
  );
}
