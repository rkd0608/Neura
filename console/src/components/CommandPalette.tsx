// The command palette (⌘K / Ctrl+K): jump to any page or resource of the project, or run a
// console action. It loads the project's lists only while it is open.
import { useEffect, useId, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { useNavigate } from "react-router";

import {
  useArtifacts,
  useDeployments,
  useExecutionProfiles,
  usePolicies,
  useRobotProfiles,
  useRobots,
  useRollouts,
  useSites,
} from "../api/queries";
import { useAuth } from "../auth/AuthProvider";
import { KIND_LABELS, rolloutLabel } from "../lib/fleet";
import { documentTitle } from "./Contracts";
import { MORE, NAV } from "./nav";
import {
  BotIcon,
  BuildingIcon,
  CubeIcon,
  LogOutIcon,
  MoonIcon,
  RocketIcon,
  SearchIcon,
  SlidersIcon,
  SunIcon,
} from "./Icons";
import { setTheme, useTheme } from "../lib/theme";

interface Item {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: ReactNode;
  run: () => void;
}

export function CommandPalette({
  open,
  onClose,
  orgId,
  projectId,
}: {
  open: boolean;
  onClose: () => void;
  orgId: string;
  projectId: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      inputRef.current?.focus();
    } else if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="palette"
      aria-label="Search and commands"
      onClose={onClose}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      {open ? (
        <Search onClose={onClose} orgId={orgId} projectId={projectId} inputRef={inputRef} />
      ) : null}
    </dialog>
  );
}

/** What the palette shows, mounted only while it is open: the project's lists load then (the
 *  pages have cached most of them), and each opening starts with an empty search. */
function Search({
  onClose,
  orgId,
  projectId,
  inputRef,
}: {
  onClose: () => void;
  orgId: string;
  projectId: string;
  inputRef: RefObject<HTMLInputElement | null>;
}) {
  const navigate = useNavigate();
  const auth = useAuth();
  const theme = useTheme();
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const listId = useId();
  const policies = usePolicies(projectId);
  const rollouts = useRollouts(projectId);
  const deployments = useDeployments(projectId);
  const robots = useRobots(projectId);
  const sites = useSites(projectId);
  const artifacts = useArtifacts(projectId);
  const robotProfiles = useRobotProfiles(projectId);
  const executionProfiles = useExecutionProfiles(projectId);
  const base = `/o/${orgId}/p/${projectId}`;

  const items = useMemo<Item[]>(() => {
    const go = (to: string) => () => {
      onClose();
      void navigate(to);
    };
    const pages: Item[] = [
      ...NAV.flatMap((section) => section.links.map((link) => ({ link, hint: section.heading }))),
      ...MORE.map((link) => ({ link, hint: "More" })),
    ].map(({ link, hint }) => ({
      id: `page:${link.to}`,
      group: "Go to",
      label: link.label,
      hint,
      icon: <link.icon />,
      run: go(link.org ? `/o/${orgId}/${link.to}` : link.to ? `${base}/${link.to}` : base),
    }));
    const resources: Item[] = [
      ...(policies.data ?? []).map((p) => ({
        id: p.metadata.id,
        group: "Policies",
        label: p.spec.name,
        hint: p.status.latest_version ? `v${p.status.latest_version}` : "no version yet",
        icon: <CubeIcon />,
        run: go(`${base}/policies/${p.metadata.id}`),
      })),
      ...(rollouts.data ?? []).map((r) => ({
        id: r.metadata.id,
        group: "Rollouts",
        label: `${KIND_LABELS[r.spec.kind] ?? r.spec.kind}: ${r.status.name}`,
        hint: rolloutLabel(r.status.state),
        icon: <RocketIcon />,
        run: go(`${base}/rollouts/${r.metadata.id}`),
      })),
      ...(deployments.data ?? []).map((d) => ({
        id: d.metadata.id,
        group: "Deployments",
        label: d.spec.display_name,
        hint: d.status.phase,
        icon: <RocketIcon />,
        run: go(`${base}/deployments/${d.metadata.id}`),
      })),
      ...(robots.data ?? []).map((r) => ({
        id: r.metadata.id,
        group: "Robots",
        label: r.spec.display_name,
        hint: r.status.phase,
        icon: <BotIcon />,
        run: go(`${base}/robots/${r.metadata.id}`),
      })),
      ...(sites.data ?? []).map((s) => ({
        id: s.metadata.id,
        group: "Sites",
        label: s.spec.display_name,
        hint: s.status.connected ? "connected" : "offline",
        icon: <BuildingIcon />,
        run: go(`${base}/sites/${s.metadata.id}`),
      })),
      ...(artifacts.data ?? []).map((a) => ({
        id: a.metadata.id,
        group: "Models",
        label: documentTitle(a),
        hint: "model artifact",
        icon: <CubeIcon />,
        run: go(`${base}/models/ModelArtifact/${a.metadata.id}`),
      })),
      ...(robotProfiles.data ?? []).map((r) => ({
        id: r.metadata.id,
        group: "Models",
        label: documentTitle(r),
        hint: "robot profile",
        icon: <BotIcon />,
        run: go(`${base}/models/RobotProfile/${r.metadata.id}`),
      })),
      ...(executionProfiles.data ?? []).map((p) => ({
        id: p.metadata.id,
        group: "Models",
        label: documentTitle(p),
        hint: "execution profile",
        icon: <SlidersIcon />,
        run: go(`${base}/models/ExecutionProfile/${p.metadata.id}`),
      })),
    ];
    const actions: Item[] = [
      {
        id: "theme",
        group: "Actions",
        label: theme === "light" ? "Switch to dark theme" : "Switch to light theme",
        icon: theme === "light" ? <MoonIcon /> : <SunIcon />,
        run: () => {
          setTheme(theme === "light" ? "dark" : "light");
          onClose();
        },
      },
      {
        id: "sign-out",
        group: "Actions",
        label: "Sign out",
        icon: <LogOutIcon />,
        run: () => {
          onClose();
          void auth.signOut();
        },
      },
    ];
    return [...pages, ...resources, ...actions];
  }, [
    policies.data,
    rollouts.data,
    deployments.data,
    robots.data,
    sites.data,
    artifacts.data,
    robotProfiles.data,
    executionProfiles.data,
    base,
    orgId,
    navigate,
    onClose,
    theme,
    auth,
  ]);

  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = items
    .filter((item) => {
      const text = `${item.label} ${item.hint ?? ""} ${item.group}`.toLowerCase();
      return words.every((w) => text.includes(w));
    })
    .slice(0, 40);
  const selected = Math.min(index, Math.max(0, shown.length - 1));

  return (
    <>
      <div className="palette-input">
        <SearchIcon />
        <input
          ref={inputRef}
          type="text"
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-activedescendant={shown[selected] ? `${listId}-${selected}` : undefined}
          aria-label="Search pages, policies, rollouts, robots, sites, deployments and models"
          placeholder="Search pages, policies, rollouts, robots, sites…"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setIndex(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setIndex(Math.min(selected + 1, shown.length - 1));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setIndex(Math.max(selected - 1, 0));
            } else if (event.key === "Enter") {
              event.preventDefault();
              shown[selected]?.run();
            }
          }}
        />
      </div>
      <div className="palette-list" id={listId} role="listbox" aria-label="Results">
        {shown.length === 0 ? (
          <div className="palette-group" role="presentation">
            Nothing matches “{query}”
          </div>
        ) : null}
        {shown.map((item, i) => (
          <PaletteRow
            key={`${item.group}:${item.id}`}
            item={item}
            id={`${listId}-${i}`}
            heading={i === 0 || shown[i - 1]?.group !== item.group ? item.group : null}
            selected={i === selected}
            onHover={() => setIndex(i)}
          />
        ))}
      </div>
      <div className="palette-footer" aria-hidden="true">
        <span>
          <kbd>↑</kbd> <kbd>↓</kbd> to move
        </span>
        <span>
          <kbd>↵</kbd> to open
        </span>
        <span>
          <kbd>esc</kbd> to close
        </span>
      </div>
    </>
  );
}

function PaletteRow({
  item,
  id,
  heading,
  selected,
  onHover,
}: {
  item: Item;
  id: string;
  heading: string | null;
  selected: boolean;
  onHover: () => void;
}) {
  return (
    <>
      {heading ? (
        <div className="palette-group" role="presentation">
          {heading}
        </div>
      ) : null}
      {/* The combobox owns the keyboard (arrows and Enter, via aria-activedescendant); a click is
          the pointer's way to choose. */}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events */}
      <div
        id={id}
        role="option"
        tabIndex={-1}
        aria-selected={selected}
        className="palette-item"
        onMouseMove={onHover}
        onClick={item.run}
      >
        {item.icon}
        <span className="truncate">{item.label}</span>
        {item.hint ? <span className="hint">{item.hint}</span> : null}
      </div>
    </>
  );
}
