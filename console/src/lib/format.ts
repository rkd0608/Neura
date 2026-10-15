// Formatting helpers. Ages and countdowns are durations computed from the server's own
// `data_age_seconds` plus local elapsed time: the console never compares its clock with a site's.

export function plural(n: number, word: string, many = `${word}s`): string {
  return `${n} ${n === 1 ? word : many}`;
}

export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return plural(s, "second");
  const m = Math.floor(s / 60);
  if (m < 60) return plural(m, "minute");
  const h = Math.floor(m / 60);
  if (h < 48) return `${plural(h, "hour")}${m % 60 ? ` ${plural(m % 60, "minute")}` : ""}`;
  return plural(Math.floor(h / 24), "day");
}

export function shortDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d`;
}

export function ago(seconds: number): string {
  return seconds < 5 ? "just now" : `${shortDuration(seconds)} ago`;
}

export function absolute(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Seconds since an ISO timestamp, by this browser's clock: used only for the console's own
 *  records (created_at, audit), never for site-reported state. */
export function secondsSince(iso: string, now: number): number {
  return Math.max(0, (now - new Date(iso).getTime()) / 1000);
}

export function shortId(id: string): string {
  const [prefix, rest] = id.split("_", 2);
  return rest ? `${prefix}_…${rest.slice(-6)}` : id;
}

export function shortDigest(digest: string): string {
  return digest.startsWith("sha256:") ? `sha256:${digest.slice(7, 19)}` : digest;
}

export function slug(text: string): string {
  return text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63)
    .replace(/-+$/g, "");
}
