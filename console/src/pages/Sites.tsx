// Sites & servers: the project's sites, their servers and connection state, and the dialog that
// adds a site (design §19.3).
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { api, unwrap, idempotent } from "../api/client";
import { useApiMutation, useRobots, useSites } from "../api/queries";
import { Dialog } from "../components/Dialog";
import { BuildingIcon, PlusIcon } from "../components/Icons";
import { Field } from "../components/Field";
import { DataAge, Empty, ErrorState, Loading, PageHeader, Status } from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { slug } from "../lib/format";

export function Sites() {
  const { orgId, projectId, base } = useScope();
  const sites = useSites(projectId);
  const robots = useRobots(projectId);
  const can = usePermissions(orgId);
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);

  return (
    <>
      <PageHeader
        title="Sites & servers"
        icon={<BuildingIcon />}
        description="A site is a place where robots and the GPU servers that serve them share a local network. Each site keeps running on its committed configuration when the cloud is unreachable, for as long as its offline allowance lasts."
        actions={
          can.admin ? (
            <button type="button" className="button primary" onClick={() => setOpen(true)}>
              <PlusIcon /> Add a site
            </button>
          ) : null
        }
      />
      {sites.isPending ? (
        <Loading />
      ) : sites.isError ? (
        <ErrorState error={sites.error} />
      ) : sites.data.length === 0 ? (
        <Empty
          title="No sites yet"
          icon={<BuildingIcon />}
          actions={
            can.admin ? (
              <button type="button" className="button primary small" onClick={() => setOpen(true)}>
                Add a site
              </button>
            ) : null
          }
        >
          <p>Add the site where your robots work, then connect its GPU server with one command.</p>
        </Empty>
      ) : (
        <div className="grid cols-3">
          {sites.data.map((site) => {
            const here = (robots.data ?? []).filter((r) => r.spec.site_id === site.metadata.id);
            const running = here.filter((r) => r.status.phase === "Running").length;
            const to = `${base}/sites/${site.metadata.id}`;
            return (
              // The whole card is a pointer target; the site name link inside is the keyboard one.
              // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
              <div
                key={site.metadata.id}
                className="card interactive"
                style={{ cursor: "pointer" }}
                onClick={(event) => {
                  if ((event.target as HTMLElement).closest("a")) return;
                  void navigate(to);
                }}
              >
                <div className="card-body stack" style={{ gap: 12 }}>
                  <div className="spread">
                    <div className="row" style={{ gap: 10 }}>
                      <span className="icon-tile">
                        <BuildingIcon />
                      </span>
                      <div>
                        <Link to={to} style={{ color: "var(--text)", fontWeight: 600 }}>
                          {site.spec.display_name}
                        </Link>
                        <div className="muted small mono">{site.spec.name}</div>
                      </div>
                    </div>
                    <Status phase={site.status.phase} />
                  </div>
                  <dl className="kv small" style={{ gridTemplateColumns: "auto 1fr" }}>
                    <dt>Agent</dt>
                    <dd>
                      {site.status.agent_version ?? <span className="muted">Not enrolled</span>}
                    </dd>
                    <dt>Robots</dt>
                    <dd>
                      {running} running of {here.length}
                    </dd>
                    <dt>Offline allowance</dt>
                    <dd>{site.spec.offline_authorization_hours} h</dd>
                    <dt>Last report</dt>
                    <dd>
                      <DataAge
                        ageSeconds={site.status.data_age_seconds}
                        fetchedAt={sites.dataUpdatedAt}
                        never="Never connected"
                      />
                    </dd>
                  </dl>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <NewSiteDialog open={open} onClose={() => setOpen(false)} projectId={projectId} base={base} />
    </>
  );
}

function NewSiteDialog({
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
  const [name, setName] = useState("");
  const [hours, setHours] = useState(24);
  const create = useApiMutation(
    async () =>
      unwrap(
        await api.POST("/v1/projects/{project_id}/sites", {
          params: { path: { project_id: projectId }, header: idempotent() },
          body: {
            spec: {
              name: slug(name) || "site",
              display_name: name.trim(),
              offline_authorization_hours: hours,
              kind: "production",
            },
          },
        }),
      ),
    [["p", projectId, "sites"]],
  );
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Add a site"
      description="You will connect its GPU server next."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(undefined, {
            onSuccess: (site) => {
              onClose();
              void navigate(`${base}/sites/${site.metadata.id}?connect=1`);
            },
          });
        }}
      >
        <div className="dialog-body">
          <Field id="site-name" label="Site name" hint="For example, the warehouse or cell name.">
            <input
              type="text"
              required
              maxLength={200}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field
            id="site-hours"
            label="Offline allowance (hours)"
            hint="How long the site keeps operating on its committed configuration without reaching Neura. After that it stops serving until it reconnects."
          >
            <input
              type="number"
              min={1}
              max={8760}
              required
              value={hours}
              onChange={(event) => setHours(Number(event.target.value))}
            />
          </Field>
          {create.isError ? (
            <ErrorState error={create.error} title="Could not add the site" />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!name.trim() || create.isPending}
          >
            {create.isPending ? "Adding…" : "Add site"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
