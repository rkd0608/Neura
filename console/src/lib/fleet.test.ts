import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { robotState, running } from "./fleet";

function robot(status: Partial<Schemas["Robot"]["status"]>): Schemas["Robot"] {
  return {
    metadata: {
      id: "rbt_1",
      version: "1",
      created_at: "2026-10-24T00:00:00Z",
      updated_at: "2026-10-24T00:00:00Z",
    },
    spec: { name: "arm-1", display_name: "Arm 1", site_id: "site_1", robot_profile_id: "rp_1" },
    status: { phase: "Ready", ...status },
  };
}

describe("robotState", () => {
  it("says what a robot does in the words the CLI prints", () => {
    expect(robotState(robot({}))).toBe("ready");
    expect(robotState(robot({ policy_version_id: "pv_1" }))).toBe("running");
    expect(robotState(robot({ phase: "Offline" }))).toBe("offline");
    expect(robotState(robot({ phase: "NeedsAttention", discovered: true }))).toBe("needs setup");
    expect(robotState(robot({ phase: "NeedsAttention" }))).toBe("attention");
    // Missing only when its server no longer reports it, not when it has nothing to run.
    expect(robotState(robot({ phase: "Offline", discovered: true, present: false }))).toBe(
      "missing",
    );
    expect(robotState(robot({ phase: "Offline", discovered: true, present: true }))).toBe(
      "offline",
    );
  });

  it("names what a robot runs as people say it", () => {
    expect(running(robot({ policy_name: "pick", policy_version: 3 }))).toBe("pick v3");
    expect(running(robot({}))).toBeNull();
  });
});
