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

/** What the signed-in principal may do in an organization. The server enforces this on every
 *  request; the console only avoids offering actions that would be refused. */
export function usePermissions(orgId: string): Permissions {
  const me = useMe();
  const grants = me.data?.organizations.find((o) => o.id === orgId)?.permissions ?? [];
  return {
    read: grants.includes("read"),
    deploy: grants.includes("deploy"),
    admin: grants.includes("admin"),
  };
}
