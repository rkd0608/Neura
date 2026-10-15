// Tables shown on several pages: sessions, faults and audit records.
import { Link } from "react-router";

import type { Schemas } from "../api/client";
import { useRobots } from "../api/queries";
import { useScope } from "../lib/context";
import { shortId } from "../lib/format";
import { ActivityIcon, AlertIcon, PulseIcon } from "./Icons";
import { Badge, DataAge, Empty, Reason, Sparkline, Status, Time, useLiveSeries } from "./ui";

type Session = Schemas["Session"];

export function SessionTable({
  sessions,
  fetchedAt,
  caption = "Sessions",
}: {
  sessions: Session[];
  fetchedAt: number;
  caption?: string;
}) {
  const { projectId, base } = useScope();
  const robots = useRobots(projectId);
  if (sessions.length === 0) {
    return (
      <Empty title="No sessions" icon={<PulseIcon />}>
        <p>Sessions appear when robots connect to their site's gateway.</p>
      </Empty>
    );
  }
  return (
    <div className="table-wrap">
      <table>
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Robot</th>
            <th scope="col">State</th>
            <th scope="col">Revision</th>
            <th scope="col" className="num">
              Buffer (actions)
            </th>
            <th scope="col">Buffer trend</th>
            <th scope="col" className="num">
              Chunks
            </th>
            <th scope="col" className="num">
              RTT p99
            </th>
            <th scope="col" className="num">
              Task epoch
            </th>
            <th scope="col">Last report</th>
          </tr>
        </thead>
        <tbody>
          {sessions.map((s) => (
            <SessionRow
              key={s.metadata.id}
              session={s}
              fetchedAt={fetchedAt}
              robot={robots.data?.find((r) => r.metadata.id === s.robot_id)?.spec.display_name}
              base={base}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SessionRow({
  session: s,
  fetchedAt,
  robot,
  base,
}: {
  session: Session;
  fetchedAt: number;
  robot: string | undefined;
  base: string;
}) {
  const live = s.state !== "Disconnected";
  const series = useLiveSeries(
    `session:${s.metadata.id}:buffer`,
    live ? (s.metrics?.buffer_actions ?? null) : undefined,
    fetchedAt,
  );
  const values = series.map((p) => p[1]).filter((v): v is number => v !== null);
  return (
    <tr>
      <td>
        <Link to={`${base}/robots/${s.robot_id}`}>{robot ?? shortId(s.robot_id)}</Link>
      </td>
      <td>
        <Status phase={s.state} />
        {s.reason ? (
          <div className="small">
            <Reason code={s.reason} />
          </div>
        ) : null}
      </td>
      <td>
        <span className="tag">{shortId(s.revision_id)}</span>
      </td>
      <td className="num">{s.metrics?.buffer_actions ?? "—"}</td>
      <td>
        <Sparkline values={values} label={`Buffer of ${robot ?? s.robot_id}`} />
      </td>
      <td className="num">{s.metrics?.chunks ?? "—"}</td>
      <td className="num">
        {s.metrics?.rtt_p99_ms !== undefined ? `${s.metrics.rtt_p99_ms.toFixed(1)} ms` : "—"}
      </td>
      <td className="num">{s.task_epoch}</td>
      <td>
        <DataAge ageSeconds={s.data_age_seconds} fetchedAt={fetchedAt} staleAfter={30} />
      </td>
    </tr>
  );
}

export function FaultTable({
  faults,
  showRobot = false,
}: {
  faults: Schemas["Fault"][];
  showRobot?: boolean;
}) {
  const { projectId, base } = useScope();
  const robots = useRobots(projectId);
  if (faults.length === 0) {
    return (
      <Empty title="No faults" icon={<AlertIcon />}>
        <p>Faults appear here when a site reports one, with the response the robot took locally.</p>
      </Empty>
    );
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Fault</th>
            {showRobot ? <th scope="col">Robot</th> : null}
            <th scope="col">State</th>
            <th scope="col">Local response</th>
            <th scope="col">When</th>
          </tr>
        </thead>
        <tbody>
          {faults.map((f) => (
            <tr key={f.metadata.id}>
              <td>
                <Reason code={f.reason} detail={f.message} />
              </td>
              {showRobot ? (
                <td>
                  <Link to={`${base}/robots/${f.robot_id}`}>
                    {robots.data?.find((r) => r.metadata.id === f.robot_id)?.spec.display_name ??
                      shortId(f.robot_id)}
                  </Link>
                </td>
              ) : null}
              <td>
                {f.cleared_at ? (
                  <Status phase="Ready" />
                ) : f.latched ? (
                  <Status phase="Faulted" />
                ) : (
                  <Status phase="Waiting" />
                )}
                <div className="muted small">
                  {f.cleared_at
                    ? "Cleared"
                    : f.latched
                      ? "Latched until reset at the site"
                      : "Recovering"}
                </div>
              </td>
              <td>{f.local_response ?? <span className="muted">—</span>}</td>
              <td>
                <Time iso={f.occurred_at} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function AuditTable({ records }: { records: Schemas["AuditRecord"][] }) {
  if (records.length === 0) {
    return (
      <Empty title="No audit records" icon={<ActivityIcon />}>
        <p>Every change and every instruction sent to a site is recorded here.</p>
      </Empty>
    );
  }
  return (
    <div className="table-wrap">
      <table>
        <caption className="visually-hidden">Audit records</caption>
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">Actor</th>
            <th scope="col">Action</th>
            <th scope="col">Target</th>
            <th scope="col">Result</th>
          </tr>
        </thead>
        <tbody>
          {records.map((r) => (
            <tr key={r.metadata.id}>
              <td className="small" style={{ minWidth: 110 }}>
                <Time iso={r.occurred_at} />
              </td>
              <td className="small">
                {r.actor}
                <div className="muted">{r.actor_kind?.replace("_", " ")}</div>
              </td>
              <td>
                <code>{r.action}</code>
              </td>
              <td>
                <span className="tag">{shortId(r.target)}</span>
              </td>
              <td>
                <Badge tone={r.result === "succeeded" ? "ok" : "danger"}>{r.result}</Badge>
                {r.reason ? (
                  <div className="small">
                    <Reason code={r.reason} />
                  </div>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
