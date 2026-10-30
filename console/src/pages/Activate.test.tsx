import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { fakeGet, fakePost, me, renderAt } from "../test/render";
import { Activate } from "./Activate";

const WAITING = {
  user_code: "BCDF-2345",
  client_name: "neura CLI on eve@far-away",
  created_at: "2026-10-29T10:00:00Z",
  expires_at: "2026-10-29T10:10:00Z",
};

function activate(principal = me(["admin", "deploy", "read"])) {
  fakeGet({
    "/v1/login-codes/{user_code}": ({ path }) => {
      const code = (path.user_code ?? "").toUpperCase().replace("-", "");
      if (code !== "BCDF2345") throw new ApiError(404, { reason: "API_NOT_FOUND" });
      return WAITING;
    },
  });
  const approvals = fakePost({
    "/v1/login-approvals": () => ({
      client_name: WAITING.client_name,
      organization_id: "org_1",
      scopes: ["admin"],
    }),
  });
  renderAt(<Activate />, { path: "/activate", url: "/activate?code=BCDF-2345", principal });
  return approvals;
}

describe("Activate", () => {
  it("does not let a link alone approve a sign-in: the person types the code their terminal shows", async () => {
    activate();
    const code = await screen.findByLabelText("Code");
    expect(code).toHaveValue("");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    // The link's code is there to compare with the terminal, not to approve.
    expect(code).toHaveAccessibleDescription(/BCDF-2345/);
  });

  it("shows what waits for the code before it is approved, then approves it", async () => {
    const approvals = activate();
    await userEvent.type(await screen.findByLabelText("Code"), "bcdf2345");
    expect(await screen.findByText("neura CLI on eve@far-away")).toBeVisible();
    expect(screen.getByText(/admin access to Ada's lab, for 30 days/)).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/now has admin access/)).toBeVisible();
    expect(approvals).toHaveBeenCalledOnce();
  });

  it("says when nothing waits for a code, and cannot approve it", async () => {
    activate();
    await userEvent.type(await screen.findByLabelText("Code"), "BCDF-2346");
    expect(await screen.findByText("API_NOT_FOUND")).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("tells someone signed in with an API token that only a person approves", async () => {
    activate(me(["admin", "deploy", "read"], { kind: "api_token" }));
    expect(await screen.findByText(/Only a person can approve a sign-in/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});
