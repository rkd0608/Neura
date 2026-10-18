// The console's only way to the backend: the public management API (design §19.1, §20.1).
// Every call carries the signed-in credential; every POST an idempotency key; failures become
// ApiError with the reason code from the shared catalog.
import createClient, { type Middleware } from "openapi-fetch";

import { REASON_CODES } from "../generated/reasonCodes";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

export interface Problem {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  reason?: string;
  retryable?: boolean;
  errors?: { path?: string; message?: string }[];
}

export class ApiError extends Error {
  readonly status: number;
  readonly reason: string;
  readonly problem: Problem;

  constructor(status: number, problem: Problem) {
    const reason = problem.reason ?? "UNKNOWN";
    super(problem.detail ?? problem.title ?? REASON_CODES[reason]?.message ?? `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.reason = reason;
    this.problem = problem;
  }

  /** The catalog's plain-language explanation of the reason code. */
  get explanation(): string {
    return REASON_CODES[this.reason]?.message ?? this.message;
  }
}

type TokenSource = () => string | null;
let tokenSource: TokenSource = () => null;
let onUnauthenticated: () => void = () => undefined;

export function configureAuth(source: TokenSource, unauthenticated: () => void): void {
  tokenSource = source;
  onUnauthenticated = unauthenticated;
}

export function currentToken(): string | null {
  return tokenSource();
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID().replaceAll("-", "");
}

const auth: Middleware = {
  onRequest({ request }) {
    const token = tokenSource();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    if (request.method === "POST" && !request.headers.has("Idempotency-Key")) {
      request.headers.set("Idempotency-Key", newIdempotencyKey());
    }
    // Every update in the API is a JSON merge patch (RFC 7396), and says so.
    if (request.method === "PATCH") {
      request.headers.set("Content-Type", "application/merge-patch+json");
    }
    return request;
  },
  async onResponse({ response }) {
    if (response.ok) return response;
    let problem: Problem = {};
    try {
      problem = (await response.clone().json()) as Problem;
    } catch {
      problem = { detail: response.statusText };
    }
    if (response.status === 401) onUnauthenticated();
    throw new ApiError(response.status, problem);
  },
};

export const api = createClient<paths>({ baseUrl: "" });
api.use(auth);

/** The data of an openapi-fetch result; errors were already thrown by the middleware. */
export function unwrap<T>(result: { data?: T; error?: unknown }): T {
  if (result.data === undefined) throw new ApiError(0, { detail: "empty response" });
  return result.data;
}

/** A fresh Idempotency-Key for one user action (design §20.4): retrying that action's request
 *  cannot apply it twice. */
export function idempotent(): { "Idempotency-Key": string } {
  return { "Idempotency-Key": newIdempotencyKey() };
}

/** If-Match for a resource version (design §20.4). */
/** The version to send with If-Match when updating a resource's spec, read just before the
 *  update: its latest, if its spec is still the one the edit started from. A site's status
 *  reports move a resource's version without touching what a person edits, and are no conflict;
 *  a spec someone else changed is, and the update is refused. */
export async function specVersion(
  latest: { metadata: { version: string }; spec: unknown },
  editedFrom: unknown,
): Promise<string> {
  if (JSON.stringify(latest.spec) !== JSON.stringify(editedFrom)) {
    throw new ApiError(412, {
      reason: "API_PRECONDITION_FAILED",
      detail: "Someone else changed it since you opened it. Close this and try again.",
    });
  }
  return Promise.resolve(latest.metadata.version);
}

export function ifMatch(version: string): { "If-Match": string } {
  return { "If-Match": `"${version}"` };
}
