// The live activity feed: the project's change events as they arrive, in words, linked to what
// changed. Events say what changed, not its new state (design §20.6): the linked page shows that.
import { Link } from "react-router";

import type { ChangeEvent } from "../api/events";
import { useScope } from "../lib/context";
import { ago, secondsSince, shortId } from "../lib/format";
import { useNow } from "../lib/useNow";
import type { Tone } from "../lib/phases";
import { Empty } from "./ui";
import { ActivityIcon } from "./Icons";

const KINDS: Record<string, { noun: string; path?: string }> = {
  dep: { noun: "Deployment", path: "deployments" },
  rbt: { noun: "Robot", path: "robots" },
  site: { noun: "Site", path: "sites" },
  srv: { noun: "Server" },
  rev: { noun: "Revision" },
  op: { noun: "Operation" },
  flt: { noun: "Fault", path: "faults" },
  ses: { noun: "Session", path: "sessions" },
  art: { noun: "Model artifact", path: "models/ModelArtifact" },
  rpf: { noun: "Robot profile", path: "models/RobotProfile" },
  exp: { noun: "Execution profile", path: "models/ExecutionProfile" },
  actr: { noun: "Activation request" },
  rbk: { noun: "Rollback" },
  prj: { noun: "Project" },
};

const VERBS: Record<ChangeEvent["type"], { text: string; tone: Tone }> = {
  "resource.created": { text: "created", tone: "info" },
  "resource.updated": { text: "updated", tone: "muted" },
  "resource.deleted": { text: "deleted", tone: "muted" },
  "operation.updated": { text: "progressed", tone: "info" },
  "fault.raised": { text: "raised", tone: "danger" },
  "fault.cleared": { text: "cleared", tone: "ok" },
};

export function describe(event: ChangeEvent): {
  noun: string;
  verb: string;
  tone: Tone;
  path?: string;
} {
  const prefix = event.resource.split("_")[0] ?? "";
  const kind = KINDS[prefix] ?? { noun: "Resource" };
  const verb = VERBS[event.type];
  const path =
    kind.path === "faults" || kind.path === "sessions"
      ? kind.path
      : kind.path
        ? `${kind.path}/${event.resource}`
        : undefined;
  return { noun: kind.noun, verb: verb.text, tone: verb.tone, path };
}

export function ActivityFeed({
  events,
  compact = false,
}: {
  events: ChangeEvent[];
  compact?: boolean;
}) {
  const { base } = useScope();
  const now = useNow(5000);
  if (events.length === 0) {
    return (
      <div style={{ padding: 16 }}>
        <Empty title="Quiet so far" icon={<ActivityIcon />}>
          <p>
            Changes appear here as they happen: deployments, robots, faults and operations.
            {compact ? null : " The audit log keeps the full history."}
          </p>
        </Empty>
      </div>
    );
  }
  // Repeated updates of one resource (a robot's state, a session's metrics) collapse into one
  // line with a count, so creations, faults and operations stand out.
  const grouped: { event: ChangeEvent; count: number }[] = [];
  for (const event of events) {
    const previous = grouped.find(
      (g) => g.event.resource === event.resource && g.event.type === event.type,
    );
    if (previous) previous.count += 1;
    else grouped.push({ event, count: 1 });
  }
  return (
    <ul className="feed" aria-label="Recent changes">
      {(compact ? grouped.slice(0, 12) : grouped).map(({ event, count }) => {
        const d = describe(event);
        return (
          <li key={event.cursor}>
            <span className={`dot ${d.tone}`} aria-hidden="true" />
            <span className="small">
              {d.noun}{" "}
              {d.path ? (
                <Link to={`${base}/${d.path}`}>
                  <code>{shortId(event.resource)}</code>
                </Link>
              ) : (
                <code>{shortId(event.resource)}</code>
              )}{" "}
              <span className="secondary">{d.verb}</span>
              {count > 1 ? <span className="muted"> ×{count}</span> : null}
            </span>
            <time className="when" dateTime={event.occurred_at} title={event.occurred_at}>
              {ago(secondsSince(event.occurred_at, now))}
            </time>
          </li>
        );
      })}
    </ul>
  );
}
