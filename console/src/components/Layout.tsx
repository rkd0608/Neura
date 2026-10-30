// The console's frame: the sidebar (organization and project switchers, navigation, docs, theme,
// account), the top bar (where you are, the live connection, search) and the page.
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate, useParams } from "react-router";

import { useProjectEvents, type LiveState } from "../api/events";
import { useFaults, useMe, useProjects } from "../api/queries";
import { useAuth } from "../auth/AuthProvider";
import { DOCS_URL } from "../lib/config";
import { usePermissions, useScope } from "../lib/context";
import { setTheme, useTheme, type Theme } from "../lib/theme";
import { CommandPalette } from "./CommandPalette";
import {
  BookIcon,
  CheckIcon,
  ChevronsIcon,
  ExternalIcon,
  FolderIcon,
  LogOutIcon,
  Mark,
  MonitorIcon,
  MoonIcon,
  PlusIcon,
  SearchIcon,
  SunIcon,
} from "./Icons";
import { MORE, NAV, sectionOf } from "./nav";
import { MenuButton } from "./ui";

function initials(text: string): string {
  const words = text.trim().split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "N") + (words[1]?.[0] ?? "")).toUpperCase();
}

function useShortcut(open: () => void) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        open();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
}

export function Layout() {
  const { orgId, projectId, base } = useScope();
  const faults = useFaults(projectId);
  const live = useProjectEvents(projectId);
  const latched = (faults.data ?? []).filter((f) => f.latched && !f.cleared_at).length;
  const [palette, setPalette] = useState(false);
  const openPalette = useCallback(() => setPalette(true), []);
  const closePalette = useCallback(() => setPalette(false), []);
  useShortcut(openPalette);

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar" aria-label="Primary">
        <NavLink to={base} className="brand" end>
          <Mark />
          Neura
        </NavLink>
        <div className="switcher">
          <OrgSwitcher orgId={orgId} />
          <ProjectSwitcher orgId={orgId} projectId={projectId} />
        </div>
        <nav aria-label="Project" style={{ display: "grid", gap: 2 }}>
          {NAV.map((section) => (
            <div className="nav-section" key={section.heading}>
              <span className="nav-heading">{section.heading}</span>
              {section.links.map((link) => (
                <NavLink
                  key={link.to + String(link.org)}
                  to={link.org ? `/o/${orgId}/${link.to}` : link.to ? `${base}/${link.to}` : base}
                  end={link.end}
                  className="nav-link"
                >
                  <link.icon />
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
        </nav>
        <SidebarFooter />
      </aside>
      <div className="main">
        <Topbar live={live} onSearch={openPalette} />
        <main id="main" className="content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
      <CommandPalette open={palette} onClose={closePalette} orgId={orgId} projectId={projectId} />
    </div>
  );
}

function OrgSwitcher({ orgId }: { orgId: string }) {
  const me = useMe();
  const navigate = useNavigate();
  const orgs = me.data?.organizations ?? [];
  const org = orgs.find((o) => o.id === orgId);
  const name = org?.display_name ?? "Organization";
  return (
    <MenuButton
      className="switcher-button"
      button={
        <>
          <span className="avatar" aria-hidden="true">
            {initials(name)}
          </span>
          <span className="switcher-text">
            <span className="switcher-label">Organization</span>
            <span className="truncate">{name}</span>
          </span>
          <ChevronsIcon className="chevron" />
        </>
      }
    >
      {(close) => (
        <>
          <div className="menu-label">Organizations</div>
          {orgs.map((o) => (
            <button
              key={o.id}
              type="button"
              className="menu-item"
              role="menuitemradio"
              aria-checked={o.id === orgId}
              onClick={() => {
                close();
                void navigate(`/o/${o.id}/projects`);
              }}
            >
              <span className="avatar" aria-hidden="true">
                {initials(o.display_name)}
              </span>
              <span className="truncate">{o.display_name}</span>
              {o.id === orgId ? <CheckIcon style={{ marginLeft: "auto" }} /> : null}
            </button>
          ))}
          <div className="menu-sep" />
          <Link className="menu-item" to={`/o/${orgId}/settings`} onClick={close}>
            Organization settings
          </Link>
        </>
      )}
    </MenuButton>
  );
}

function ProjectSwitcher({ orgId, projectId }: { orgId: string; projectId: string }) {
  const projects = useProjects(orgId);
  const can = usePermissions(orgId);
  const navigate = useNavigate();
  const project = projects.data?.find((p) => p.metadata.id === projectId);
  const name = project?.spec.display_name ?? (projectId ? "Project" : "Choose a project");
  return (
    <MenuButton
      className="switcher-button"
      button={
        <>
          <span className="avatar round" aria-hidden="true">
            <FolderIcon width={12} height={12} />
          </span>
          <span className="switcher-text">
            <span className="switcher-label">Project</span>
            <span className="truncate">{name}</span>
          </span>
          <ChevronsIcon className="chevron" />
        </>
      }
    >
      {(close) => (
        <>
          <div className="menu-label">Projects</div>
          {(projects.data ?? []).map((p) => (
            <button
              key={p.metadata.id}
              type="button"
              className="menu-item"
              role="menuitemradio"
              aria-checked={p.metadata.id === projectId}
              onClick={() => {
                close();
                void navigate(`/o/${orgId}/p/${p.metadata.id}`);
              }}
            >
              <span className="truncate">{p.spec.display_name}</span>
              {p.metadata.id === projectId ? <CheckIcon style={{ marginLeft: "auto" }} /> : null}
            </button>
          ))}
          <div className="menu-sep" />
          <Link className="menu-item" to={`/o/${orgId}/projects`} onClick={close}>
            {can.admin ? (
              <>
                <PlusIcon /> New project
              </>
            ) : (
              <>
                <FolderIcon /> All projects
              </>
            )}
          </Link>
        </>
      )}
    </MenuButton>
  );
}

function SidebarFooter() {
  const auth = useAuth();
  const me = useMe();
  const theme = useTheme();
  const who =
    me.data?.kind === "api_token" ? "API token" : (me.data?.subject.split("|").pop() ?? "Account");
  const themes: { id: Theme; label: string; icon: ReactNode }[] = [
    { id: "dark", label: "Dark", icon: <MoonIcon /> },
    { id: "light", label: "Light", icon: <SunIcon /> },
    { id: "system", label: "System", icon: <MonitorIcon /> },
  ];
  return (
    <div className="sidebar-footer">
      <a className="nav-link" href={DOCS_URL} target="_blank" rel="noreferrer">
        <BookIcon />
        Documentation
        <ExternalIcon style={{ marginLeft: "auto", width: 13, height: 13 }} />
      </a>
      <MenuButton
        className="switcher-button"
        placement="above"
        button={
          <>
            <span className="avatar round" aria-hidden="true">
              {initials(who)}
            </span>
            <span className="switcher-text">
              <span className="truncate small">{who}</span>
              <span className="visually-hidden">: account and theme</span>
            </span>
            <ChevronsIcon className="chevron" />
          </>
        }
      >
        {(close) => (
          <>
            <div className="menu-label" title={me.data?.subject}>
              Signed in as {who}
            </div>
            <div className="menu-sep" />
            <div className="menu-label">Theme</div>
            {themes.map((t) => (
              <button
                key={t.id}
                type="button"
                className="menu-item"
                role="menuitemradio"
                aria-checked={theme === t.id}
                onClick={() => setTheme(t.id)}
              >
                {t.icon}
                {t.label}
                {theme === t.id ? <CheckIcon style={{ marginLeft: "auto" }} /> : null}
              </button>
            ))}
            <div className="menu-sep" />
            <button
              type="button"
              className="menu-item"
              onClick={() => {
                close();
                void auth.signOut();
              }}
            >
              <LogOutIcon />
              Sign out
            </button>
          </>
        )}
      </MenuButton>
    </div>
  );
}

function Topbar({ live, onSearch }: { live?: LiveState; onSearch?: () => void }) {
  const { orgId, projectId } = useParams();
  const location = useLocation();
  const projects = useProjects(orgId ?? "");
  const project = projects.data?.find((p) => p.metadata.id === projectId);
  const base = projectId ? `/o/${orgId}/p/${projectId}` : "";
  const rest =
    base && location.pathname.startsWith(base) ? location.pathname.slice(base.length + 1) : "";
  const section = projectId
    ? (sectionOf(rest) ?? (rest ? undefined : { label: "Fleet" }))
    : undefined;
  const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.userAgent);
  return (
    <header className="topbar">
      <div className="topbar-left">
        <nav className="crumbs" aria-label="You are here">
          {project ? (
            <>
              <Link to={base}>{project.spec.display_name}</Link>
              {section ? (
                <>
                  <span className="sep" aria-hidden="true">
                    /
                  </span>
                  <span aria-current="page">{section.label}</span>
                </>
              ) : null}
            </>
          ) : null}
        </nav>
      </div>
      <div className="topbar-right">
        {onSearch ? (
          <button type="button" className="search-trigger" onClick={onSearch}>
            <SearchIcon width={15} height={15} />
            Search
            <kbd>{mac ? "⌘" : "Ctrl"} K</kbd>
          </button>
        ) : null}
        {live ? (
          <span
            role="status"
            aria-live="polite"
            className={`live${live === "live" ? "" : " reconnecting"}`}
            title="Changes appear as they happen: the console follows the project's event stream."
          >
            <span className="pulse" aria-hidden="true" />
            {live === "live" ? "Live" : live === "connecting" ? "Connecting" : "Reconnecting"}
          </span>
        ) : null}
      </div>
    </header>
  );
}

/** Pages outside a project (projects, organization settings) keep the sidebar, without the
 *  project's sections. */
export function OrgLayout() {
  const { orgId = "" } = useParams();
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar" aria-label="Primary">
        <Link to="/" className="brand">
          <Mark />
          Neura
        </Link>
        <div className="switcher">
          <OrgSwitcher orgId={orgId} />
        </div>
        <nav aria-label="Organization" style={{ display: "grid", gap: 2 }}>
          <div className="nav-section">
            <span className="nav-heading">Organization</span>
            {[...MORE, ...NAV.flatMap((s) => s.links)]
              .filter((l) => l.org)
              .map((link) => (
                <NavLink key={link.to} to={`/o/${orgId}/${link.to}`} className="nav-link">
                  <link.icon />
                  {link.label}
                </NavLink>
              ))}
          </div>
        </nav>
        <SidebarFooter />
      </aside>
      <div className="main">
        <Topbar />
        <main id="main" className="content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
