import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { Fleet } from "../pages/Fleet";
import { Sites } from "../pages/Sites";
import { SITE, fakeFleet } from "../test/fleet";
import { fakePost, me, renderAt, type Request } from "../test/render";

/** Create a site named "Dock 2" from a page, and return the spec the console sent. */
async function createFrom(page: ReactNode, path: string): Promise<unknown> {
  fakeFleet([]);
  const sent: Request[] = [];
  fakePost({
    "/v1/projects/{project_id}/sites": (request) => {
      sent.push(request);
      return { ...SITE, metadata: { ...SITE.metadata, id: "site_2" } };
    },
  });
  renderAt(page, {
    path,
    url: path.replace(":orgId", "org_1").replace(":projectId", "prj_1"),
    principal: me(["admin", "deploy", "read"]),
  });
  await userEvent.click(await screen.findByRole("button", { name: "Add a site" }));
  const dialog = screen.getByRole("dialog");
  await userEvent.type(within(dialog).getByRole("textbox"), "Dock 2");
  await userEvent.click(within(dialog).getByRole("button", { name: /site$/ }));
  expect(sent).toHaveLength(1);
  return (sent[0]?.body as { spec: unknown }).spec;
}

const DEFAULTS = {
  name: "dock-2",
  display_name: "Dock 2",
  offline_authorization_hours: 72, // the CLI's default
  kind: "production",
};

describe("NewSite", () => {
  it("creates a site with the defaults of the CLI from Fleet", async () => {
    expect(await createFrom(<Fleet />, "/o/:orgId/p/:projectId")).toEqual(DEFAULTS);
  });

  it("creates a site with the same defaults from Sites", async () => {
    // The page then opens the new site's page with its server dialog (see the journey).
    expect(await createFrom(<Sites />, "/o/:orgId/p/:projectId/sites")).toEqual(DEFAULTS);
  });
});
