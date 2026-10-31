// Unit tests render a screen as the console does, against a fake of the API. The screen gets a
// query client and a router at an address; requests are answered from in-memory resources,
// paged as the server pages them, and recorded (vi.spyOn) so a test can say what was asked.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { vi } from "vitest";

import { api, ApiError, type Schemas } from "../api/client";
import { ToastProvider } from "../components/Toasts";

export type Query = Record<string, string | number | undefined>;
export interface Request {
  path: Record<string, string>;
  query: Query;
  body?: unknown;
}
type Handler = (request: Request) => unknown;
type Init = { params?: { path?: Record<string, string>; query?: Query }; body?: unknown };

function answer(method: string, handlers: Record<string, Handler>) {
  return async (template: string, init?: Init) => {
    const handler = handlers[template];
    if (!handler) throw new ApiError(404, { detail: `the test has no ${method} ${template}` });
    const request = {
      path: init?.params?.path ?? {},
      query: init?.params?.query ?? {},
      body: init?.body,
    };
    return Promise.resolve({ data: handler(request) });
  };
}

/** Answer api.GET by path template; a request with no handler fails with a 404. */
export function fakeGet(handlers: Record<string, Handler>) {
  return vi.spyOn(api, "GET").mockImplementation(answer("GET", handlers) as typeof api.GET);
}

/** The path templates a fake was asked for, in order. */
export function asked(fake: { mock: { calls: unknown[][] } }): string[] {
  return fake.mock.calls.map((call) => String(call[0]));
}

/** Answer api.POST by path template. */
export function fakePost(handlers: Record<string, Handler>) {
  return vi.spyOn(api, "POST").mockImplementation(answer("POST", handlers) as typeof api.POST);
}

/** A server-sent-event stream that sends `text`, then stays open until the request is aborted. */
export function openStream(text: string, signal?: AbortSignal | null): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text));
      signal?.addEventListener("abort", () => {
        controller.error(new DOMException("aborted", "AbortError"));
      });
    },
  });
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

/** Answer what the console fetches beside the API client: /v1/meta (how to sign in) and each
 *  project's event stream, which by default opens and sends nothing. */
export function fakeFetch(
  stream: (init: RequestInit | undefined) => Response = (init) => openStream("", init?.signal),
) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url.endsWith("/v1/meta")) {
      return Promise.resolve(Response.json({ authentication: {}, service_version: "test" }));
    }
    if (url.endsWith("/events")) return Promise.resolve(stream(init));
    return Promise.reject(new Error(`the test does not fetch ${url}`));
  });
}

/** One page of a list, as the API returns it: page_size items from the cursor (an offset here). */
export function paged<T>(
  items: T[],
  query: Query,
): { items: T[]; page: { next_cursor: string | null } } {
  const start = Number(query.cursor ?? 0);
  const size = Number(query.page_size ?? 50);
  const next = start + size < items.length ? String(start + size) : null;
  return { items: items.slice(start, start + size), page: { next_cursor: next } };
}

/** The signed-in principal: a member of org_1 with these permissions. */
export function me(
  permissions: ("read" | "deploy" | "admin")[],
  extra: Partial<Schemas["CurrentPrincipal"]> = {},
): Schemas["CurrentPrincipal"] {
  return {
    subject: "https://sso.example|ada",
    kind: "user",
    organizations: [{ id: "org_1", display_name: "Ada's lab", permissions }],
    project_ids: null,
    ...extra,
  };
}

export function testClient(principal?: Schemas["CurrentPrincipal"]): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  if (principal) client.setQueryData(["me"], principal);
  return client;
}

export function Providers({ client, children }: { client: QueryClient; children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>{children}</ToastProvider>
    </QueryClientProvider>
  );
}

/** Render a screen at an address of the console (`path` is its route, `url` where it is). */
export function renderAt(
  element: ReactNode,
  { path, url, principal }: { path: string; url: string; principal?: Schemas["CurrentPrincipal"] },
) {
  const client = testClient(principal);
  render(
    <Providers client={client}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path={path} element={element} />
        </Routes>
      </MemoryRouter>
    </Providers>,
  );
  return client;
}
