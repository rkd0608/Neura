// The console's state vocabulary (design §19.4): every observed phase has one label and one tone.
export type Tone = "ok" | "warn" | "danger" | "info" | "muted" | "accent";

const TONES: Record<string, Tone> = {
  // ConsolePhase
  Unknown: "muted",
  Preparing: "info",
  Ready: "ok",
  Running: "ok",
  Waiting: "warn",
  NeedsAttention: "danger",
  Offline: "muted",
  // Revisions, operations, activation requests, deployments, sessions
  Created: "muted",
  Prepared: "ok",
  Failed: "danger",
  Retired: "muted",
  Pending: "info",
  Succeeded: "ok",
  Cancelled: "muted",
  AwaitingLocalApproval: "warn",
  Approved: "info",
  Refused: "danger",
  Activated: "ok",
  Active: "ok",
  Activating: "info",
  RollingBack: "warn",
  Invited: "info",
  Revoked: "muted",
  Expired: "muted",
  Armed: "info",
  Faulted: "danger",
  Disconnected: "muted",
};

const LABELS: Record<string, string> = {
  NeedsAttention: "Needs attention",
  AwaitingLocalApproval: "Awaiting local approval",
};

export function tone(phase: string | null | undefined): Tone {
  return phase ? (TONES[phase] ?? "muted") : "muted";
}

export function label(phase: string | null | undefined): string {
  if (!phase) return "Unknown";
  return LABELS[phase] ?? phase;
}
