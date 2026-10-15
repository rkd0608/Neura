// Small, accessible building blocks shared by every page.
import { useId, useState, type ReactNode } from "react";

import { ApiError } from "../api/client";
import { REASON_CODES } from "../generated/reasonCodes";
import { ago, absolute, duration } from "../lib/format";
import { label, tone, type Tone } from "../lib/phases";
import { useNow } from "../lib/useNow";

export function Badge({ tone: t, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`badge ${t}`}>{children}</span>;
}

/** An observed phase with its label; color is never the only signal. */
export function Status({
  phase,
  stale = false,
}: {
  phase: string | null | undefined;
  stale?: boolean;
}) {
  return (
    <span className={stale ? "stale" : undefined}>
      <Badge tone={stale ? "muted" : tone(phase)}>{label(phase)}</Badge>
    </span>
  );
}

/** A reason code with its plain-language explanation from the catalog (design §19.1). */
export function Reason({
  code,
  detail,
}: {
  code: string | null | undefined;
  detail?: string | null;
}) {
  if (!code) return detail ? <span className="secondary">{detail}</span> : null;
  const info = REASON_CODES[code];
  return (
    <span className="stack" style={{ gap: 2 }}>
      <span>{info?.message ?? code}</span>
      <span className="muted small">
        <code>{code}</code>
        {detail ? ` · ${detail}` : null}
      </span>
    </span>
  );
}

/**
 * How old site-reported data is. `ageSeconds` is the server's `data_age_seconds` at fetch time and
 * `fetchedAt` the local time of that fetch, so the age is a duration: no clocks are compared.
 */
export function DataAge({
  ageSeconds,
  fetchedAt,
  staleAfter = 60,
  never = "No data yet",
}: {
  ageSeconds: number | null | undefined;
  fetchedAt: number;
  staleAfter?: number;
  never?: string;
}) {
  const now = useNow(1000);
  if (ageSeconds === null || ageSeconds === undefined)
    return <span className="muted">{never}</span>;
  const age = ageSeconds + Math.max(0, now - fetchedAt) / 1000;
  const stale = age > staleAfter;
  return (
    <span className={stale ? "secondary" : "muted"} title={`${duration(age)} old`}>
      {stale ? `Last seen ${ago(age)}` : `Updated ${ago(age)}`}
    </span>
  );
}

export function Time({ iso }: { iso: string | null | undefined }) {
  if (!iso) return <span className="muted">—</span>;
  return <time dateTime={iso}>{absolute(iso)}</time>;
}

export function PageHeader({
  title,
  description,
  actions,
  breadcrumbs,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumbs?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        {breadcrumbs ? (
          <nav className="breadcrumbs" aria-label="Breadcrumbs">
            {breadcrumbs}
          </nav>
        ) : null}
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function Card({
  title,
  description,
  actions,
  children,
  flush = false,
  labelledBy,
}: {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  labelledBy?: string;
}) {
  const id = useId();
  const headingId = labelledBy ?? `${id}-title`;
  return (
    <section className="card" aria-labelledby={title ? headingId : undefined}>
      {title ? (
        <div className="card-header">
          <div>
            <h2 id={headingId}>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          {actions ? <div className="row">{actions}</div> : null}
        </div>
      ) : null}
      <div className={flush ? "card-body flush" : "card-body"}>{children}</div>
    </section>
  );
}

export function KeyValue({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v], i) => (
        <div key={i} style={{ display: "contents" }}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {children}
    </div>
  );
}

export function Loading({ label: text = "Loading" }: { label?: string }) {
  return (
    <div className="stack" aria-busy="true" aria-live="polite" style={{ padding: 18 }}>
      <span className="visually-hidden">{text}…</span>
      <div className="skeleton" style={{ width: "60%" }} />
      <div className="skeleton" style={{ width: "80%" }} />
      <div className="skeleton" style={{ width: "40%" }} />
    </div>
  );
}

/** An error from the API, explained with its reason code. */
export function ErrorState({
  error,
  title = "Something went wrong",
}: {
  error: unknown;
  title?: string;
}) {
  if (error instanceof ApiError) {
    const notFound = error.status === 404;
    return (
      <div className="callout danger" role="alert">
        <strong>{notFound ? "Not found, or you do not have access" : title}</strong>
        <span>{error.explanation}</span>
        {error.problem.detail && error.problem.detail !== error.explanation ? (
          <span className="small">{error.problem.detail}</span>
        ) : null}
        <span className="muted small">
          <code>{error.reason}</code>
          {error.problem.errors?.length
            ? ` · ${error.problem.errors.map((e) => `${e.path ?? ""} ${e.message ?? ""}`).join("; ")}`
            : null}
        </span>
      </div>
    );
  }
  return (
    <div className="callout danger" role="alert">
      <strong>{title}</strong>
      <span>{error instanceof Error ? error.message : String(error)}</span>
    </div>
  );
}

export function Callout({
  tone: t = "info",
  title,
  children,
}: {
  tone?: "info" | "warn" | "danger" | "ok";
  title?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={`callout ${t}`} role={t === "danger" ? "alert" : undefined}>
      {title ? <strong>{title}</strong> : null}
      {children}
    </div>
  );
}

export function CopyField({ value, label: text }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="copy">
      <pre aria-label={text}>{value}</pre>
      <button
        type="button"
        className="button"
        onClick={() => {
          void navigator.clipboard.writeText(value).then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 2000);
          });
        }}
      >
        {copied ? "Copied" : "Copy"}
        <span className="visually-hidden"> {text}</span>
      </button>
      <span className="visually-hidden" aria-live="polite">
        {copied ? `${text} copied` : ""}
      </span>
    </div>
  );
}

export function Field({
  label: text,
  hint,
  error,
  children,
  id,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  id: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label htmlFor={id}>{text}</label>
      {children}
      {hint ? (
        <span className="hint" id={`${id}-hint`}>
          {hint}
        </span>
      ) : null}
      {error ? (
        <span className="error" id={`${id}-error`} role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}

export function Stat({
  label: text,
  value,
  note,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
}) {
  const id = useId();
  return (
    <div className="card stat" role="group" aria-labelledby={id}>
      <div className="stat-label" id={id}>
        {text}
      </div>
      <div className="stat-value">{value}</div>
      {note ? <div className="stat-note">{note}</div> : null}
    </div>
  );
}
