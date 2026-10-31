// Small, accessible building blocks shared by every page.
import {
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router";

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
  icon,
  meta,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumbs?: ReactNode;
  icon?: ReactNode;
  /** A row of facts under the title (status, site, counts). */
  meta?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div style={{ minWidth: 0 }}>
        {breadcrumbs ? (
          <nav className="breadcrumbs" aria-label="Breadcrumbs">
            {breadcrumbs}
          </nav>
        ) : null}
        <div className="page-title">
          {icon ? <span className="icon-tile">{icon}</span> : null}
          <h1>{title}</h1>
        </div>
        {description ? <p>{description}</p> : null}
        {meta ? <div className="page-meta">{meta}</div> : null}
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

export function Empty({
  title,
  icon,
  actions,
  children,
}: {
  title: string;
  icon?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      {icon ? <span className="icon-tile">{icon}</span> : null}
      <strong>{title}</strong>
      {children}
      {actions ? <div className="row">{actions}</div> : null}
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

// --- tabs ---------------------------------------------------------------------------------------

export interface TabSpec {
  id: string;
  label: string;
  count?: number | null;
}

/** The selected tab, kept in the URL (?tab=) so every tab can be linked to and survives reload. */
export function useTab(
  tabs: TabSpec[],
  fallback = tabs[0]?.id ?? "",
): [string, (id: string) => void] {
  const [search, setSearch] = useSearchParams();
  const wanted = search.get("tab");
  const current = tabs.some((t) => t.id === wanted) ? (wanted as string) : fallback;
  const select = (id: string) =>
    setSearch(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (id === fallback) next.delete("tab");
        else next.set("tab", id);
        return next;
      },
      { replace: true },
    );
  return [current, select];
}

/** A tab list (WAI-ARIA tabs pattern: arrow keys move between tabs). */
export function Tabs({
  tabs,
  value,
  onChange,
  label: text,
}: {
  tabs: TabSpec[];
  value: string;
  onChange: (id: string) => void;
  label: string;
}) {
  const id = useId();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const delta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!delta) return;
    event.preventDefault();
    const next = (index + delta + tabs.length) % tabs.length;
    const tab = tabs[next];
    if (!tab) return;
    onChange(tab.id);
    refs.current[next]?.focus();
  };
  return (
    <div className="tabs" role="tablist" aria-label={text}>
      {tabs.map((tab, index) => (
        <button
          key={tab.id}
          ref={(el) => {
            refs.current[index] = el;
          }}
          type="button"
          role="tab"
          id={`${id}-${tab.id}`}
          className="tab"
          aria-selected={tab.id === value}
          tabIndex={tab.id === value ? 0 : -1}
          onClick={() => onChange(tab.id)}
          onKeyDown={(event) => onKey(event, index)}
        >
          {tab.label}
          {tab.count !== undefined && tab.count !== null ? (
            <span className="count">{tab.count}</span>
          ) : null}
        </button>
      ))}
    </div>
  );
}

export function TabPanel({ label: text, children }: { label: string; children: ReactNode }) {
  return (
    <div role="tabpanel" aria-label={text} className="stack" style={{ gap: 16 }}>
      {children}
    </div>
  );
}

// --- toolbar parts ------------------------------------------------------------------------------

export function SearchInput({
  value,
  onChange,
  placeholder,
  label: text,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  label: string;
}) {
  const id = useId();
  return (
    <div className="search">
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.75}
        aria-hidden="true"
      >
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-3.5-3.5" />
      </svg>
      <label htmlFor={id} className="visually-hidden">
        {text}
      </label>
      <input
        id={id}
        type="search"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label: text,
}: {
  options: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={text}>
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          aria-pressed={o.id === value}
          onClick={() => onChange(o.id)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Meter({
  value,
  max,
  tone: t,
  label: text,
}: {
  value: number;
  max: number;
  tone?: "ok" | "warn" | "danger";
  label: string;
}) {
  const share = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  return (
    <div
      className={`meter ${t ?? ""}`}
      role="meter"
      aria-label={text}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={value}
    >
      <span style={{ width: `${share * 100}%` }} />
    </div>
  );
}

// --- charts -------------------------------------------------------------------------------------

export type Point = [number, number | null]; // [epoch ms, value]

/** The rendered width of an element, kept current as it resizes. */
function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.round(entry.contentRect.width));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

/** A line chart over time, in SVG at its real pixel size (text never stretches). Gaps (null)
 *  break the line; fewer than two points say why there is no line yet. */
export function LineChart({
  points,
  height = 140,
  format = (v) => v.toFixed(1),
  color = "var(--chart-1)",
  label: text,
  empty = "No data in this window",
}: {
  points: Point[];
  height?: number;
  format?: (v: number) => string;
  color?: string;
  label: string;
  empty?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const [ref, measured] = useWidth<HTMLDivElement>();
  const id = useId();
  const values = points.map((p) => p[1]).filter((v): v is number => v !== null);
  const width = Math.max(measured, 120);
  const pad = { top: 10, right: 10, bottom: 22, left: 44 };
  let body: ReactNode;
  if (values.length === 0) {
    body = (
      <div className="chart-empty" role="img" aria-label={`${text}: ${empty}`} style={{ height }}>
        {empty}
      </div>
    );
  } else if (values.length === 1) {
    body = (
      <div
        className="chart-empty"
        role="img"
        aria-label={`${text}: ${format(values[0] ?? 0)}`}
        style={{ height }}
      >
        <span>
          <strong className="chart-value">{format(values[0] ?? 0)}</strong>
          <br />
          collecting the trend…
        </span>
      </div>
    );
  } else {
    const first = points[0]?.[0] ?? 0;
    const last = points.at(-1)?.[0] ?? first + 1;
    const lo = Math.min(0, ...values);
    const peak = Math.max(...values);
    const hi = peak > lo ? peak + (peak - lo) * 0.15 : lo + 1;
    const x = (t: number) =>
      pad.left + ((t - first) / Math.max(1, last - first)) * (width - pad.left - pad.right);
    const y = (v: number) => pad.top + (1 - (v - lo) / (hi - lo)) * (height - pad.top - pad.bottom);
    const segments: { d: string; start: number; end: number }[] = [];
    let current: { d: string; start: number; end: number } | null = null;
    for (const [t, v] of points) {
      if (v === null) {
        if (current) segments.push(current);
        current = null;
        continue;
      }
      const px = x(t);
      const step = `${px.toFixed(1)},${y(v).toFixed(1)}`;
      current = current
        ? { d: `${current.d}L${step}`, start: current.start, end: px }
        : { d: `M${step}`, start: px, end: px };
    }
    if (current) segments.push(current);
    const ticks = [lo, (lo + hi) / 2, hi];
    const hovered = hover !== null ? points[hover] : undefined;
    const time = (t: number) =>
      new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    body = (
      <svg
        className="chart"
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${text}: latest ${format(values.at(-1) ?? 0)}, peak ${format(peak)}`}
        onMouseMove={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          const at = event.clientX - box.left;
          let best = 0;
          let distance = Infinity;
          points.forEach(([t], i) => {
            const d = Math.abs(x(t) - at);
            if (d < distance) {
              distance = d;
              best = i;
            }
          });
          setHover(best);
        }}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id={`${id}-fill`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor={color} stopOpacity="0.26" />
            <stop offset="1" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        {ticks.map((v, i) => (
          <g key={i}>
            <line className="grid-line" x1={pad.left} x2={width - pad.right} y1={y(v)} y2={y(v)} />
            <text className="axis" x={pad.left - 8} y={y(v) + 3.5} textAnchor="end">
              {format(v)}
            </text>
          </g>
        ))}
        <text className="axis" x={pad.left} y={height - 5}>
          {time(first)}
        </text>
        <text className="axis" x={width - pad.right} y={height - 5} textAnchor="end">
          {time(last)}
        </text>
        {segments.map((seg, i) => (
          <g key={i}>
            <path
              d={`${seg.d}L${seg.end.toFixed(1)},${y(lo).toFixed(1)}L${seg.start.toFixed(1)},${y(lo).toFixed(1)}Z`}
              fill={`url(#${id}-fill)`}
            />
            <path d={seg.d} fill="none" stroke={color} strokeWidth={1.75} strokeLinejoin="round" />
          </g>
        ))}
        {hovered && hovered[1] !== null ? (
          <g>
            <line
              x1={x(hovered[0])}
              x2={x(hovered[0])}
              y1={pad.top}
              y2={height - pad.bottom}
              stroke="var(--border-strong)"
            />
            <circle cx={x(hovered[0])} cy={y(hovered[1])} r={3.5} fill={color} />
          </g>
        ) : null}
      </svg>
    );
  }
  const hovered = hover !== null ? points[hover] : undefined;
  return (
    <figure style={{ margin: 0 }}>
      <div ref={ref} style={{ width: "100%" }}>
        {body}
      </div>
      <figcaption className="muted tiny" style={{ minHeight: 18 }}>
        {hovered && hovered[1] !== null
          ? `${new Date(hovered[0]).toLocaleTimeString()} · ${format(hovered[1])}`
          : "\u00a0"}
      </figcaption>
    </figure>
  );
}

/** A small trend line, for stat tiles and table cells. */
export function Sparkline({
  values,
  width = 96,
  height = 24,
  color = "var(--chart-1)",
  label: text,
}: {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
  label: string;
}) {
  if (values.length < 2) return <span className="muted tiny">—</span>;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const d = values
    .map((v, i) => {
      const px = (i / (values.length - 1)) * (width - 2) + 1;
      const py = height - 2 - ((v - lo) / span) * (height - 4);
      return `${i ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`;
    })
    .join("");
  return (
    <svg
      className="spark"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${text}: from ${values[0]} to ${values.at(-1)}`}
    >
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} />
    </svg>
  );
}

// Live series: values sampled each time a polled query refreshes, kept for the browser session so
// a chart survives navigation. They show what this console observed since it opened, nothing more.
const SERIES = new Map<string, Point[]>();
const SERIES_LIMIT = 180;
const seriesListeners = new Set<() => void>();
const NO_POINTS: Point[] = [];

function sample(key: string, value: number | null, at: number): void {
  const series = SERIES.get(key) ?? NO_POINTS;
  if (series.at(-1)?.[0] === at) return;
  SERIES.set(key, [...series, [at, value] as Point].slice(-SERIES_LIMIT));
  seriesListeners.forEach((l) => l());
}

export function useLiveSeries(key: string, value: number | null | undefined, at: number): Point[] {
  useEffect(() => {
    if (value !== undefined && at) sample(key, value, at);
  }, [key, value, at]);
  return useSyncExternalStore(
    (listener) => {
      seriesListeners.add(listener);
      return () => seriesListeners.delete(listener);
    },
    () => SERIES.get(key) ?? NO_POINTS,
    () => NO_POINTS,
  );
}

// --- menus --------------------------------------------------------------------------------------

/** A disclosure menu: a button that opens a list of actions; Escape or a click outside closes it. */
export function MenuButton({
  label: text,
  button,
  className = "button ghost small",
  align = "left",
  placement = "below",
  children,
}: {
  /** The button's accessible name, when its visible content does not say what it opens. */
  label?: string;
  button: ReactNode;
  className?: string;
  align?: "left" | "right";
  placement?: "below" | "above";
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="popover-anchor" ref={ref}>
      <button
        type="button"
        className={className}
        aria-haspopup="true"
        aria-expanded={open}
        aria-label={text}
        onClick={() => setOpen((v) => !v)}
        style={{ width: className.includes("switcher-button") ? "100%" : undefined }}
      >
        {button}
      </button>
      {open ? (
        <div
          className="menu"
          style={{
            [align]: 0,
            ...(placement === "below"
              ? { top: "calc(100% + 6px)" }
              : { bottom: "calc(100% + 6px)" }),
          }}
        >
          {children(() => setOpen(false))}
        </div>
      ) : null}
    </div>
  );
}
