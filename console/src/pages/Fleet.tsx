// Fleet: where the robots are and what each one runs (design §29). The console's home.
import { useState } from "react";
import { Link } from "react-router";

import type { Schemas } from "../api/client";
import { useRobots, useRollouts, useSites } from "../api/queries";
import { BotIcon, PlusIcon, ServerIcon } from "../components/Icons";
import { ConnectServer, NewSite } from "../components/SiteDialogs";
import { Badge, Callout, Card, ErrorState, Loading, PageHeader, Time } from "../components/ui";
import { usePermissions, useScope } from "../lib/context";
import { ROBOT_TONE, robotState, running } from "../lib/fleet";

type Site = Schemas["Site"];
type Robot = Schemas["Robot"];

export function Fleet() {
  const { orgId, projectId, base } = useScope();
  const sites = useSites(projectId);
  const robots = useRobots(projectId);
  // The server filters: the banner sees every waiting rollout, however many came after it.
  const waiting = useRollouts(projectId, { state: "awaiting_approval" });
  const can = usePermissions(orgId, projectId);
  const [adding, setAdding] = useState(false);

  if (sites.isPending || robots.isPending) return <Loading />;
  if (sites.isError) return <ErrorState error={sites.error} />;
  if (robots.isError) return <ErrorState error={robots.error} />;
  const siteName = (id: string) => sites.data.find((s) => s.metadata.id === id)?.spec.name ?? id;

  return (
    <>
      <PageHeader
        title="Fleet"
        icon={<BotIcon />}
        description="Where your robots are, and what each one runs."
        actions={
          can.admin && sites.data.length > 0 ? (
            <button type="button" className="button" onClick={() => setAdding(true)}>
              <PlusIcon /> Add a site
            </button>
          ) : null
        }
      />
      {(waiting.data ?? []).map((rollout) => (
        <Callout key={rollout.metadata.id} tone="warn" title={`${rollout.status.name} is waiting`}>
          <p>
            A person at {siteName(rollout.status.site_id)} approves it: on the site&apos;s server,
            run <code>neura approve</code>. No one can approve it from here.{" "}
            <Link to={`${base}/rollouts/${rollout.metadata.id}`}>See the rollout</Link>
          </p>
        </Callout>
      ))}
      {sites.data.length === 0 ? (
        <FirstSite admin={can.admin} />
      ) : (
        <div className="stack" style={{ gap: 20 }}>
          {sites.data.map((site) => (
            <SiteSection
              key={site.metadata.id}
              site={site}
              robots={robots.data.filter((r) => r.spec.site_id === site.metadata.id)}
              admin={can.admin}
            />
          ))}
        </div>
      )}
      <NewSite open={adding} onClose={() => setAdding(false)} />
    </>
  );
}

function connection(site: Site): { tone: "ok" | "warn" | "muted"; text: string } {
  if (site.status.connected) return { tone: "ok", text: "Connected" };
  if (site.status.agent_version) return { tone: "warn", text: "Offline" };
  return { tone: "muted", text: "No server yet" };
}

function SiteSection({ site, robots, admin }: { site: Site; robots: Robot[]; admin: boolean }) {
  const { base } = useScope();
  const [connecting, setConnecting] = useState(false);
  const state = connection(site);
  const bench = site.spec.kind === "bench";
  return (
    <Card
      title={
        <span className="row" style={{ gap: 8 }}>
          <Link to={`${base}/sites/${site.metadata.id}`}>{site.spec.name}</Link>
          <Badge tone={bench ? "info" : "accent"}>{bench ? "Bench" : "Production"}</Badge>
          <Badge tone={state.tone}>{state.text}</Badge>
        </span>
      }
      description={
        bench
          ? "A bench approves its own changes: for trying things, never near people."
          : "A person at the site approves every change."
      }
      actions={
        admin ? (
          <button type="button" className="button small" onClick={() => setConnecting(true)}>
            <ServerIcon /> Add a server
          </button>
        ) : null
      }
      flush={robots.length > 0}
    >
      {robots.length === 0 ? (
        <p className="secondary">
          {site.status.agent_version
            ? "Its server has not found a robot yet. Plug an arm in, or list the robots in its robots file."
            : "Add a server: robots appear here as it finds them."}
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">Robots at {site.spec.name}</caption>
            <thead>
              <tr>
                <th scope="col">Robot</th>
                <th scope="col">State</th>
                <th scope="col">Running</th>
                <th scope="col">Since</th>
              </tr>
            </thead>
            <tbody>
              {robots.map((robot) => {
                const state = robotState(robot);
                const what = running(robot);
                return (
                  <tr key={robot.metadata.id}>
                    <td>
                      <Link to={`${base}/robots/${robot.metadata.id}`}>{robot.spec.name}</Link>
                      {state === "needs setup" && robot.status.message ? (
                        <div className="muted small">{robot.status.message}</div>
                      ) : null}
                    </td>
                    <td>
                      <Badge tone={ROBOT_TONE[state]}>{state}</Badge>
                    </td>
                    <td>{what ?? <span className="muted">Nothing yet</span>}</td>
                    <td>
                      <Time iso={robot.metadata.updated_at} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <ConnectServer site={site} open={connecting} onClose={() => setConnecting(false)} />
    </Card>
  );
}

/** The first run: a workspace without a site. */
function FirstSite({ admin }: { admin: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <Card title="Connect your first robot">
      <ol className="stack" style={{ paddingLeft: 18, margin: 0, gap: 14 }}>
        <li>
          <strong>Create a site</strong>
          <p className="secondary small">
            A bench to try things on, or the production site where your robots work.
          </p>
          {admin ? (
            <button type="button" className="button primary" onClick={() => setOpen(true)}>
              <PlusIcon /> Create a site
            </button>
          ) : (
            <p className="muted small">An admin of this workspace creates sites.</p>
          )}
        </li>
        <li>
          <strong>Join the computer next to the robot</strong>
          <p className="secondary small">
            It finds the arms plugged into it, or simulates one. With Python 3.13:
          </p>
          <pre className="code">
            {'pip install "neura[site]"\nneura login\nneura site join SITE --simulate 1'}
          </pre>
        </li>
        <li>
          <strong>Push a policy and try it</strong>
          <pre className="code">
            {"neura policy push ./checkpoint --name pick\nneura try pick --on sim-1"}
          </pre>
        </li>
      </ol>
      <NewSite open={open} onClose={() => setOpen(false)} />
    </Card>
  );
}
