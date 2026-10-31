// A project's fleet for the console's unit tests: a site, its robots and rollouts, answered by
// the fake API the way the server answers (newest first, filtered by state, paged).
import { fakeGet, fakePost, paged, type Query } from "./render";

export const SITE = {
  metadata: { id: "site_1", version: "1", created_at: "2026-10-01T00:00:00Z" },
  spec: {
    name: "lab-line",
    display_name: "Lab line",
    kind: "production",
    offline_authorization_hours: 72,
  },
  status: { phase: "Ready", connected: true, agent_version: "0.9.0", data_age_seconds: 1 },
};

/** `count` rollouts, newest first; `waiting` of them (the oldest) wait for approval at the site. */
export function rollouts(count: number, waiting = 0) {
  return Array.from({ length: count }, (_, i) => {
    const n = count - i;
    return {
      metadata: { id: `rlt_${String(n).padStart(3, "0")}`, created_at: "2026-10-01T00:00:00Z" },
      spec: { kind: "deploy", robot_ids: [] },
      status: {
        name: `pick v${n}`,
        site_id: SITE.metadata.id,
        state: i >= count - waiting ? "awaiting_approval" : "live",
        steps: [],
      },
    };
  });
}

/** Answer the fleet's lists: the site, no robots, and these rollouts. */
export function fakeFleet(
  all: ReturnType<typeof rollouts>,
  extra: Parameters<typeof fakeGet>[0] = {},
) {
  return fakeGet({
    "/v1/projects/{project_id}/sites": ({ query }) => paged([SITE], query),
    "/v1/sites/{site_id}": () => SITE,
    "/v1/sites/{site_id}/servers": ({ query }) => paged([], query),
    "/v1/projects/{project_id}/robots": ({ query }) => paged([], query),
    "/v1/projects/{project_id}/rollouts": ({ query }: { query: Query }) =>
      paged(
        all.filter((r) => !query.state || r.status.state === query.state),
        query,
      ),
    ...extra,
  });
}

/** Enrollment tokens: each one new (nen_1, nen_2, ...), as the server issues single-use tokens. */
export function fakeEnrollment(extra: Parameters<typeof fakePost>[0] = {}) {
  let issued = 0;
  return fakePost({
    "/v1/sites/{site_id}/enrollment-tokens": () => {
      issued += 1;
      const flags = `--control-plane 127.0.0.1:8081 --token nen_${issued}`;
      return {
        id: `enr_${issued}`,
        site_id: SITE.metadata.id,
        expires_at: "2026-10-30T12:00:00Z",
        join_command: `neura site join ${SITE.spec.name} ${flags}`,
        install_command: `neura-agent enroll ${flags}`,
      };
    },
    ...extra,
  });
}
