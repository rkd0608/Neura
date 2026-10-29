import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { Providers, me, testClient } from "../test/render";
import { usePermissions } from "./context";

function permissions(principal: Schemas["CurrentPrincipal"], projectId?: string) {
  const client = testClient(principal);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <Providers client={client}>{children}</Providers>
  );
  return renderHook(() => usePermissions("org_1", projectId), { wrapper }).result.current;
}

const NONE = { read: false, deploy: false, admin: false };
const ALL = { read: true, deploy: true, admin: true };

describe("usePermissions", () => {
  it("is what the organization grants, in it and in each of its projects", () => {
    const admin = me(["admin", "deploy", "read"]);
    expect(permissions(admin)).toEqual(ALL);
    expect(permissions(admin, "prj_1")).toEqual(ALL);
  });

  it("lets a token limited to some projects only read the organization, as the server does", () => {
    const token = me(["admin", "deploy", "read"], { kind: "api_token", project_ids: ["prj_1"] });
    expect(permissions(token)).toEqual({ read: true, deploy: false, admin: false });
    expect(permissions(token, "prj_1")).toEqual(ALL);
    expect(permissions(token, "prj_2")).toEqual(NONE);
  });

  it("grants nothing in an organization the principal is not a member of", () => {
    expect(permissions(me(["read"], { organizations: [] }))).toEqual(NONE);
  });
});
