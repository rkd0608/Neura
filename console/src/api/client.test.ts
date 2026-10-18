import { describe, expect, it } from "vitest";

import { ApiError, specVersion } from "./client";

describe("specVersion", () => {
  const spec = { display_name: "Warehouse 3", offline_authorization_hours: 24 };

  it("updates on the latest version when only the site's reports moved it", async () => {
    await expect(
      specVersion({ metadata: { version: "7" }, spec: { ...spec } }, spec),
    ).resolves.toBe("7");
  });

  it("refuses when someone else changed what is being edited", async () => {
    const changed = { metadata: { version: "8" }, spec: { ...spec, display_name: "Dock 1" } };
    const refused = specVersion(changed, spec);
    await expect(refused).rejects.toBeInstanceOf(ApiError);
    await expect(refused).rejects.toMatchObject({ status: 412, reason: "API_PRECONDITION_FAILED" });
  });
});
