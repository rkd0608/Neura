import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { fakeFleet, rollouts } from "../test/fleet";
import { me, renderAt } from "../test/render";
import { Fleet } from "./Fleet";

const AT = { path: "/o/:orgId/p/:projectId", url: "/o/org_1/p/prj_1" };

describe("Fleet", () => {
  it("lists every rollout waiting for approval, however many rollouts came after it", async () => {
    fakeFleet(rollouts(60, 1));
    renderAt(<Fleet />, { ...AT, principal: me(["admin", "deploy", "read"]) });
    expect(await screen.findByText("pick v1 is waiting")).toBeVisible();
  });
});
