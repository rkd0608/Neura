import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { fakeEnrollment, fakeFleet, rollouts } from "../test/fleet";
import { me, renderAt } from "../test/render";
import { Fleet } from "./Fleet";

const AT = { path: "/o/:orgId/p/:projectId", url: "/o/org_1/p/prj_1" };
const ADMIN = me(["admin", "deploy", "read"]);

describe("Fleet", () => {
  it("lists every rollout waiting for approval, however many rollouts came after it", async () => {
    fakeFleet(rollouts(60, 1));
    renderAt(<Fleet />, { ...AT, principal: ADMIN });
    expect(await screen.findByText("pick v1 is waiting")).toBeVisible();
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
