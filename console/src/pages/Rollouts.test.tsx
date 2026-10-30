import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { fakeFleet, rollouts } from "../test/fleet";
import { me, renderAt } from "../test/render";
import { Rollouts } from "./Rollouts";

describe("Rollouts", () => {
  it("lists every rollout, not only the newest page", async () => {
    fakeFleet(rollouts(260));
    renderAt(<Rollouts />, {
      path: "/o/:orgId/p/:projectId/rollouts",
      url: "/o/org_1/p/prj_1/rollouts",
      principal: me(["read"]),
    });
    const table = await screen.findByRole("table", { name: "Rollouts" });
    expect(within(table).getAllByRole("row")).toHaveLength(260 + 1);
    expect(within(table).getByRole("link", { name: "Deploy: pick v1" })).toBeInTheDocument();
  });
});
