import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { fakeEnrollment, fakeFleet, rollouts } from "../test/fleet";
import { me, paged, renderAt } from "../test/render";
import { Fleet } from "./Fleet";

const AT = { path: "/o/:orgId/p/:projectId", url: "/o/org_1/p/prj_1" };
const ADMIN = me(["admin", "deploy", "read"]);

describe("Fleet", () => {
  it("lists every rollout waiting for approval, however many rollouts came after it", async () => {
    fakeFleet(rollouts(60, 1));
    renderAt(<Fleet />, { ...AT, principal: ADMIN });
    expect(await screen.findByText("pick v1 is waiting")).toBeVisible();
  });

  it("shows each robot's state and what it runs, and no time it cannot vouch for", async () => {
    // A robot's updated_at moves with every report its site sends: it is not when its state began.
    const robot = {
      metadata: { id: "rbt_1", version: "9", updated_at: "2026-10-30T09:59:58Z" },
      spec: { name: "arm-1", display_name: "Arm 1", site_id: "site_1" },
      status: {
        phase: "Running",
        policy_version_id: "pv_1",
        policy_name: "pick",
        policy_version: 3,
      },
    };
    fakeFleet([], { "/v1/projects/{project_id}/robots": ({ query }) => paged([robot], query) });
    renderAt(<Fleet />, { ...AT, principal: ADMIN });
    const table = await screen.findByRole("table", { name: "Robots at lab-line" });
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(headers).toEqual(["Robot", "State", "Running"]);
    expect(within(table).getByRole("cell", { name: "pick v3" })).toBeVisible();
  });

  it("offers fresh commands each time a server is added: a used one is never shown again", async () => {
    fakeFleet([]);
    fakeEnrollment();
    renderAt(<Fleet />, { ...AT, principal: ADMIN });
    await userEvent.click(await screen.findByRole("button", { name: "Add a server" }));
    let dialog = screen.getByRole("dialog", { name: "Add a server to lab-line" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Generate the commands" }));
    expect(await within(dialog).findAllByText(/--token nen_1$/)).toHaveLength(2);
    await userEvent.click(within(dialog).getByRole("button", { name: "Done" }));

    await userEvent.click(screen.getByRole("button", { name: "Add a server" }));
    dialog = screen.getByRole("dialog", { name: "Add a server to lab-line" });
    expect(within(dialog).getByRole("button", { name: "Generate the commands" })).toBeVisible();
    expect(within(dialog).queryAllByText(/--token nen_1$/)).toHaveLength(0);
  });
});
