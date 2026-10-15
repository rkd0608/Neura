// The console's navigation: one place for the sidebar, the command palette and the top bar.
import type { ComponentType, SVGProps } from "react";

import {
  ActivityIcon,
  AlertIcon,
  BotIcon,
  BuildingIcon,
  CubeIcon,
  FolderIcon,
  GaugeIcon,
  GearIcon,
  HomeIcon,
  PulseIcon,
  RocketIcon,
} from "./Icons";

export interface NavLinkSpec {
  /** Path inside the project ("" is the project's home), or inside the organization (org). */
  to: string;
  label: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  end?: boolean;
  org?: boolean;
}

export const NAV: { heading: string; links: NavLinkSpec[] }[] = [
  {
    heading: "Project",
    links: [
      { to: "", label: "Overview", icon: HomeIcon, end: true },
      { to: "deployments", label: "Deployments", icon: RocketIcon },
    ],
  },
  {
    heading: "Fleet",
    links: [
      { to: "robots", label: "Robots", icon: BotIcon },
      { to: "sites", label: "Sites & servers", icon: BuildingIcon },
      { to: "sessions", label: "Live sessions", icon: PulseIcon },
      { to: "faults", label: "Faults", icon: AlertIcon },
    ],
  },
  {
    heading: "Build",
    links: [
      { to: "models", label: "Models & contracts", icon: CubeIcon },
      { to: "capacity", label: "Capacity", icon: GaugeIcon },
    ],
  },
  {
    heading: "Observe",
    links: [{ to: "activity", label: "Activity", icon: ActivityIcon }],
  },
  {
    heading: "Organization",
    links: [
      { to: "projects", label: "Projects", icon: FolderIcon, org: true },
      { to: "settings", label: "Settings", icon: GearIcon, org: true },
    ],
  },
];

/** The section a path inside the project belongs to, for the top bar. */
export function sectionOf(path: string): NavLinkSpec | undefined {
  const first = path.split("/")[0] ?? "";
  const alias: Record<string, string> = { "execution-profiles": "models", audit: "activity" };
  const key = alias[first] ?? first;
  return NAV.flatMap((s) => s.links).find((l) => !l.org && l.to === key);
}
