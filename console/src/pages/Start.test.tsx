import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { fakeGet, me, paged, renderAt } from "../test/render";
import { Projects } from "./Start";

function projects(principal: ReturnType<typeof me>) {
  fakeGet({ "/v1/organizations/{organization_id}/projects": ({ query }) => paged([], query) });
  renderAt(<Projects />, { path: "/o/:orgId/projects", url: "/o/org_1/projects", principal });
}

describe("Projects", () => {
  it("offers a new project to an admin of the organization", async () => {
    projects(me(["admin", "deploy", "read"]));
    expect(await screen.findByRole("button", { name: "Create project" })).toBeInTheDocument();
  });

  it("does not offer what the server refuses: only admins create projects", async () => {
    projects(me(["deploy", "read"]));
    expect(await screen.findByText(/An admin of this organization creates projects/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Create project" })).not.toBeInTheDocument();
  });
});
