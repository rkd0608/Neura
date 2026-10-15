import { describe, expect, it } from "vitest";

import { parseSse } from "./events";

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
