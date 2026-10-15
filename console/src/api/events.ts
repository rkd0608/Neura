// Live updates (design §20.6): the project's change events, read with fetch so the request can
// carry the Authorization header (EventSource cannot). Events say what changed; the console
// re-reads by invalidating the cached queries of that project. The stream resumes from the last
// id the server sent, including the starting position it sends first, so nothing is missed
// across reconnects.
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, useSyncExternalStore } from "react";

import { currentToken, type Schemas } from "./client";

export type ChangeEvent = Schemas["Event"];

// The events this console received, per project, newest last: the live activity feed. Bounded,
// and only what arrived while the console was open; the audit log is the durable record.
const FEED_LIMIT = 300;
const feeds = new Map<string, ChangeEvent[]>();
const feedListeners = new Set<() => void>();
const EMPTY: ChangeEvent[] = [];

export function recordEvent(projectId: string, data: string): void {
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return;
  }
  if (!isEvent(parsed)) return;
  const event = parsed;
  const feed = [...(feeds.get(projectId) ?? []), event].slice(-FEED_LIMIT);
  feeds.set(projectId, feed);
  feedListeners.forEach((l) => l());
}

function isEvent(value: unknown): value is ChangeEvent {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.type === "string" &&
    typeof v.resource === "string" &&
    typeof v.cursor === "string" &&
    typeof v.occurred_at === "string"
  );
}

export function useEventFeed(projectId: string): ChangeEvent[] {
  return useSyncExternalStore(
    (listener) => {
      feedListeners.add(listener);
      return () => feedListeners.delete(listener);
    },
    () => feeds.get(projectId) ?? EMPTY,
    () => EMPTY,
  );
}

export type LiveState = "connecting" | "live" | "reconnecting";

export interface StreamMessage {
  id: string | null;
  data: string | null;
}

/** Parse server-sent-event text. Returns complete messages and the unparsed remainder. */
export function parseSse(buffer: string): { messages: StreamMessage[]; rest: string } {
  const messages: StreamMessage[] = [];
  const blocks = buffer.split(/\r?\n\r?\n/);
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let id: string | null = null;
    const data: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "id") id = value;
      else if (field === "data") data.push(value);
    }
    if (id !== null || data.length)
      messages.push({ id, data: data.length ? data.join("\n") : null });
  }
  return { messages, rest };
}

export function useProjectEvents(projectId: string | undefined): LiveState {
  const queryClient = useQueryClient();
  const [state, setState] = useState<LiveState>("connecting");

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    // A function, so the check is not narrowed away: aborting happens from the cleanup.
    const aborted = () => controller.signal.aborted;
    let lastId: string | null = null;
    let timer: number | undefined;
    const refresh = () => {
      window.clearTimeout(timer);
      // Coalesce bursts (a rollout emits many events) into one refresh.
      timer = window.setTimeout(() => {
        void queryClient.invalidateQueries({ queryKey: ["p", projectId] });
      }, 250);
    };

    void (async () => {
      let backoff = 1000;
      while (!aborted()) {
        try {
          const headers: Record<string, string> = { Accept: "text/event-stream" };
          const token = currentToken();
          if (token) headers.Authorization = `Bearer ${token}`;
          if (lastId) headers["Last-Event-ID"] = lastId;
          const response = await fetch(`/v1/projects/${encodeURIComponent(projectId)}/events`, {
            headers,
            signal: controller.signal,
          });
          if (!response.ok || !response.body) throw new Error(`events: HTTP ${response.status}`);
          setState("live");
          backoff = 1000;
          const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
          let buffer = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            const parsed = parseSse(buffer + value);
            buffer = parsed.rest;
            for (const message of parsed.messages) {
              if (message.id !== null) lastId = message.id;
              if (message.data !== null) {
                recordEvent(projectId, message.data);
                refresh();
              }
            }
          }
        } catch {
          if (aborted()) return;
          setState("reconnecting");
          await new Promise((resolve) => window.setTimeout(resolve, backoff));
          backoff = Math.min(backoff * 2, 15000);
        }
      }
    })();

    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [projectId, queryClient]);

  return state;
}
