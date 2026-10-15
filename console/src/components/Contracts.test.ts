import { describe, expect, it } from "vitest";

import { parseDocument } from "./Contracts";

describe("parseDocument", () => {
  it("accepts YAML and JSON documents of the expected kind", () => {
    const yaml =
      "api_version: neura.dev/v1alpha1\nkind: RobotProfile\nmetadata: {name: arm}\nspec: {}\n";
    expect(parseDocument(yaml, "RobotProfile").kind).toBe("RobotProfile");
    expect(parseDocument(JSON.stringify({ kind: "ModelArtifact" }), "ModelArtifact").kind).toBe(
      "ModelArtifact",
    );
  });

  it("refuses the wrong kind, non-objects and broken syntax with a clear message", () => {
    expect(() => parseDocument("kind: RobotProfile", "ExecutionProfile")).toThrow(
      "This is a robot profile, not an execution profile.",
    );
    expect(() => parseDocument("- a\n- b", "RobotProfile")).toThrow(
      "must be a YAML or JSON object",
    );
    expect(() => parseDocument("kind: [unclosed", "RobotProfile")).toThrow(
      "Not valid YAML or JSON",
    );
  });
});
