import { NavLink, Outlet, useNavigate } from "react-router";

import { useProjectEvents } from "../api/events";
import { useFaults, useMe, useProjects } from "../api/queries";
import { useAuth } from "../auth/AuthProvider";
import { useScope } from "../lib/context";
import { Badge } from "./ui";

const SECTIONS: { heading: string; links: { to: string; label: string; end?: boolean }[] }[] = [
  { heading: "Overview", links: [{ to: "", label: "Home", end: true }] },
  {
    heading: "Fleet",
    links: [
      { to: "sites", label: "Sites" },
      { to: "robots", label: "Robots" },
      { to: "sessions", label: "Live sessions" },
      { to: "faults", label: "Faults" },
    ],
  },
  {
    heading: "Deploy",
    links: [
      { to: "models", label: "Models" },
      { to: "execution-profiles", label: "Execution profiles" },
      { to: "capacity", label: "Capacity" },
      { to: "releases", label: "Releases" },
    ],
  },
  { heading: "Govern", links: [{ to: "audit", label: "Audit log" }] },
];

export function Layout() {
  const { orgId, projectId, base } = useScope();
  const auth = useAuth();
  const me = useMe();
  const projects = useProjects(orgId);
  const faults = useFaults(projectId);
  const live = useProjectEvents(projectId);
  const navigate = useNavigate();
  const latched = (faults.data ?? []).filter((f) => f.latched && !f.cleared_at).length;
  const org = me.data?.organizations.find((o) => o.id === orgId);

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar" aria-label="Primary">
        <NavLink to={base} className="brand">
          <img src="/favicon.svg" alt="" />
          Neura
        </NavLink>
        <nav aria-label="Project" className="stack" style={{ gap: 14 }}>
          {SECTIONS.map((section) => (
            <div className="nav-section" key={section.heading}>
              <span className="nav-heading">{section.heading}</span>
              {section.links.map((link) => (
                <NavLink
                  key={link.to}
                  to={link.to ? `${base}/${link.to}` : base}
                  end={link.end}
                  className="nav-link"
                >
                  {link.label}
                  {link.to === "faults" && latched > 0 ? (
                    <span className="nav-count" aria-label={`${latched} latched`}>
                      {latched}
                    </span>
                  ) : null}
                </NavLink>
              ))}
            </div>
          ))}
          <div className="nav-section">
            <span className="nav-heading">Organization</span>
            <NavLink to={`/o/${orgId}/projects`} className="nav-link" end>
              Projects
            </NavLink>
            <NavLink to={`/o/${orgId}/settings`} className="nav-link">
              Settings
            </NavLink>
          </div>
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="topbar-left">
            <span className="secondary">{org?.display_name ?? "Organization"}</span>
            <span className="muted" aria-hidden="true">
              /
            </span>
            <label className="visually-hidden" htmlFor="project-switcher">
              Project
            </label>
            <select
              id="project-switcher"
              value={projectId}
              style={{ width: "auto", minWidth: 180 }}
              onChange={(event) => void navigate(`/o/${orgId}/p/${event.target.value}`)}
            >
              {(projects.data ?? []).map((p) => (
                <option key={p.metadata.id} value={p.metadata.id}>
                  {p.spec.display_name}
                </option>
              ))}
            </select>
          </div>
          <div className="topbar-right">
            <span role="status" aria-live="polite">
              {live === "live" ? (
                <Badge tone="ok">Live</Badge>
              ) : (
                <Badge tone="warn">{live === "connecting" ? "Connecting" : "Reconnecting"}</Badge>
              )}
            </span>
            <span className="muted small" title={me.data?.subject}>
              {me.data?.kind === "api_token"
                ? "API token"
                : (me.data?.subject.split("|").pop() ?? "")}
            </span>
            <button type="button" className="button small" onClick={() => void auth.signOut()}>
              Sign out
            </button>
          </div>
        </header>
        <main id="main" className="content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

/** Pages outside a project (organization settings, project list) share a simpler frame. */
export function OrgLayout() {
  const auth = useAuth();
  const me = useMe();
  return (
    <div className="main" style={{ minHeight: "100%" }}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <div className="topbar-left">
          <NavLink to="/" className="brand">
            <img src="/favicon.svg" alt="" />
            Neura
          </NavLink>
        </div>
        <div className="topbar-right">
          <span className="muted small">{me.data?.kind === "api_token" ? "API token" : ""}</span>
          <button type="button" className="button small" onClick={() => void auth.signOut()}>
            Sign out
          </button>
        </div>
      </header>
      <main id="main" className="content" tabIndex={-1} style={{ margin: "0 auto" }}>
        <Outlet />
      </main>
    </div>
  );
}
