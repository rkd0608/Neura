import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { AuthProvider } from "../auth/AuthProvider";
import { fakeFleet, rollouts } from "../test/fleet";
import { fakeFetch, me, paged, renderAt, type Query } from "../test/render";
import { CommandPalette } from "./CommandPalette";

const POLICY = {
  metadata: { id: "pol_1", version: "1", created_at: "2026-10-01T00:00:00Z" },
  spec: { name: "pick" },
  status: { versions: 2, latest_version: 2 },
};

function Palette() {
  const [open, setOpen] = useState(false);
  return (
    <AuthProvider>
      <button type="button" onClick={() => setOpen(true)}>
        Search
      </button>
      <CommandPalette open={open} onClose={() => setOpen(false)} orgId="org_1" projectId="prj_1" />
    </AuthProvider>
  );
}

function render() {
  fakeFetch();
  const empty = ({ query }: { query: Query }) => paged([], query);
  const get = fakeFleet(rollouts(2), {
    "/v1/projects/{project_id}/deployments": empty,
    "/v1/projects/{project_id}/model-artifacts": empty,
    "/v1/projects/{project_id}/robot-profiles": empty,
    "/v1/projects/{project_id}/execution-profiles": empty,
    "/v1/projects/{project_id}/policies": ({ query }) => paged([POLICY], query),
  });
  renderAt(<Palette />, { path: "/", url: "/", principal: me(["read"]) });
  return get;
}

describe("CommandPalette", () => {
  it("loads nothing while it is closed", async () => {
    const get = render();
    expect(await screen.findByRole("button", { name: "Search" })).toBeVisible();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(get).not.toHaveBeenCalled();
  });

  it("finds the project's policies and rollouts once it opens", async () => {
    const get = render();
    await userEvent.click(await screen.findByRole("button", { name: "Search" }));
    await userEvent.type(screen.getByRole("combobox"), "pick");
    expect(await screen.findByRole("option", { name: /^pick/ })).toBeVisible();
    expect(screen.getByRole("option", { name: /^Deploy: pick v2/ })).toBeVisible();
    await waitFor(() => expect(get).toHaveBeenCalled());
  });
});
