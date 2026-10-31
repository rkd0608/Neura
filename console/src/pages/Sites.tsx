// Sites & servers: the project's sites, their servers and connection state; a new site opens on
// its page, ready to connect its server (design §19.3).
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { useRobots, useSites } from "../api/queries";
import { BuildingIcon, PlusIcon } from "../components/Icons";
import { NewSite } from "../components/SiteDialogs";
import { DataAge, Empty, ErrorState, Loading, PageHeader, Status } from "../components/ui";
import { usePermissions, useScope } from "../lib/context";

export function Sites() {
  const { orgId, projectId, base } = useScope();
  const sites = useSites(projectId);
  const robots = useRobots(projectId);
  const can = usePermissions(orgId, projectId);
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
      <NewSite
        open={open}
        onClose={() => setOpen(false)}
        onCreated={(site) => void navigate(`${base}/sites/${site.metadata.id}?connect=1`)}
      />
    </>
  );
}
