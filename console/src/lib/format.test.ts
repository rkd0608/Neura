import { describe, expect, it } from "vitest";

import { ago, duration, shortDigest, shortId, slug } from "./format";
import { label, tone } from "./phases";

describe("format", () => {
  it("writes durations people read", () => {
    expect(duration(1)).toBe("1 second");
    expect(duration(125)).toBe("2 minutes");
    expect(duration(3 * 3600 + 120)).toBe("3 hours 2 minutes");
    expect(duration(3 * 86400)).toBe("3 days");
    expect(ago(2)).toBe("just now");
    expect(ago(90)).toBe("1m ago");
  });

  it("makes API names from display names", () => {
    expect(slug("Warehouse 3 — Line B")).toBe("warehouse-3-line-b");
    expect(slug("  --  ")).toBe("");
    expect(slug("x".repeat(80))).toHaveLength(63);
  });

  it("shortens identifiers and digests", () => {
    expect(shortId("rbt_01J0000000000000000000ABCD")).toBe("rbt_…00ABCD");
    expect(shortDigest(`sha256:${"a".repeat(64)}`)).toBe(`sha256:${"a".repeat(12)}`);
  });
});

describe("state vocabulary", () => {
  it("has one label and tone per phase (design §19.4)", () => {
    expect(label("NeedsAttention")).toBe("Needs attention");
    expect(tone("NeedsAttention")).toBe("danger");
    expect(label("AwaitingLocalApproval")).toBe("Awaiting local approval");
    expect(tone("Running")).toBe("ok");
    expect(tone("Offline")).toBe("muted");
    expect(tone("SomethingNew")).toBe("muted");
    expect(label(null)).toBe("Unknown");
  });
});
