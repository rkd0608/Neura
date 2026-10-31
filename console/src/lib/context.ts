// Where the console is (the organization and project in the URL) and what the signed-in
// principal may do there.
import { useParams } from "react-router";

import { useMe } from "../api/queries";

export interface ProjectScope {
  orgId: string;
  projectId: string;
  /** Path prefix for links inside the project. */
  base: string;
}

export function useScope(): ProjectScope {
  const { orgId = "", projectId = "" } = useParams();
  return { orgId, projectId, base: `/o/${orgId}/p/${projectId}` };
}

export interface Permissions {
  read: boolean;
  deploy: boolean;
  admin: boolean;
}

/** What the signed-in principal may do in an organization, or in one of its projects: the rule
 *  of the server's Principal.permissions. A token limited to some projects may only read the
 *  organization itself (members, tokens, new projects), and has nothing in other projects. The
 *  server enforces this on every request; the console only avoids offering what it would refuse. */
export function usePermissions(orgId: string, projectId?: string): Permissions {
  const me = useMe();
  let grants = me.data?.organizations.find((o) => o.id === orgId)?.permissions ?? [];
  const limited = me.data?.project_ids;
  if (limited) {
    if (projectId === undefined) grants = grants.filter((p) => p === "read");
    else if (!limited.includes(projectId)) grants = [];
  }
  return {
    read: grants.includes("read"),
    deploy: grants.includes("deploy"),
    admin: grants.includes("admin"),
  };
}
