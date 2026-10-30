import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AuthProvider } from "../auth/AuthProvider";
import { fakeFleet } from "../test/fleet";
import { asked, fakeFetch, me, paged, renderAt } from "../test/render";
import { Layout } from "./Layout";

describe("Layout", () => {
  it("loads only what its frame shows: not the faults, nor the closed palette's lists", async () => {
    fakeFetch();
    const get = fakeFleet([], {
      "/v1/organizations/{organization_id}/projects": ({ query }) => paged([], query),
    });
    renderAt(
      <AuthProvider>
        <Layout />
      </AuthProvider>,
      { path: "/o/:orgId/p/:projectId", url: "/o/org_1/p/prj_1", principal: me(["read"]) },
    );
    expect(await screen.findByRole("navigation", { name: "Project" })).toBeVisible();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(asked(get)).toEqual(["/v1/organizations/{organization_id}/projects"]);
  });
});
