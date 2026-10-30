import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { Providers, fakeFetch, openStream, testClient } from "../test/render";
import { parseSse, useProjectEvents } from "./events";

describe("parseSse", () => {
  it("reads events, the starting position and keepalives, keeping partial input", () => {
    const text =
      "retry: 1000\nid: 41\n\n" +
      ": keepalive\n\n" +
      'id: 42\nevent: resource.updated\ndata: {"cursor":"42"}\n\n' +
      "id: 43\nda";
    const { messages, rest } = parseSse(text);
    expect(messages).toEqual([
      { id: "41", data: null },
      { id: "42", data: '{"cursor":"42"}' },
    ]);
    expect(rest).toBe("id: 43\nda");
  });

  it("joins multi-line data and accepts CRLF", () => {
    const { messages } = parseSse("data: a\r\ndata: b\r\n\r\n");
    expect(messages).toEqual([{ id: null, data: "a\nb" }]);
  });
});

describe("useProjectEvents", () => {
  it("starts again from now when its cursor has expired, and re-reads what is on screen", async () => {
    // The stream ends after its starting position (41); resuming from 41 finds it pruned (410).
    const resumedFrom: (string | undefined)[] = [];
    const fetch = fakeFetch((init) => {
      const cursor = (init?.headers as Record<string, string>)["Last-Event-ID"];
      resumedFrom.push(cursor);
      if (resumedFrom.length === 1) return new Response("retry: 1000\nid: 41\n\n");
      if (cursor === "41") {
        return Response.json(
          { status: 410, reason: "API_CURSOR_EXPIRED" },
          { status: 410, headers: { "Content-Type": "application/problem+json" } },
        );
      }
      return openStream("id: 97\n\n", init?.signal);
    });
    const client = testClient();
    const reread = vi.spyOn(client, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <Providers client={client}>{children}</Providers>
    );
    const { result, unmount } = renderHook(() => useProjectEvents("prj_1"), { wrapper });

    await waitFor(() => expect(resumedFrom).toHaveLength(3), { timeout: 3000 });
    expect(resumedFrom).toEqual([undefined, "41", undefined]);
    expect(reread).toHaveBeenCalledWith({ queryKey: ["p", "prj_1"] });
    await waitFor(() => expect(result.current).toBe("live"));
    unmount();
    expect(fetch).toHaveBeenCalledTimes(3);
  });
});
