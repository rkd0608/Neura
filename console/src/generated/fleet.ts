// The fleet's words: what robots and rollouts are called, in the console and from
// `neura` (design §19.4, §29).
// GENERATED FILE: do not edit. Source: catalog/fleet.yaml.
// Regenerate with `make gen`.

export type Tone = "ok" | "warn" | "danger" | "info" | "muted";
export type RobotState = "running" | "ready" | "preparing" | "waiting" | "needs setup" | "attention" | "missing" | "offline" | "unknown";
export type RolloutState = "checking" | "preparing" | "shadow_testing" | "awaiting_approval" | "switching" | "live" | "stopped" | "cancelled";
export type StepName = "check" | "prepare" | "shadow" | "approve" | "switch";
export type StepState = "pending" | "running" | "passed" | "skipped" | "failed";
export type RolloutKind = "deploy" | "try" | "rollback";

/** The fields of a robot's status its state is decided from. */
export interface RobotFacts {
  readonly phase?: string | null;
  readonly discovered?: boolean | null;
  readonly present?: boolean | null;
  readonly policy_version_id?: string | null;
}

export const ROBOT_TONES: Readonly<Record<RobotState, Tone>> = {
  "running": "ok",
  "ready": "ok",
  "preparing": "info",
  "waiting": "warn",
  "needs setup": "warn",
  "attention": "danger",
  "missing": "danger",
  "offline": "muted",
  "unknown": "muted",
};

/** What a robot is doing, in one word: the word `neura robots` prints for it. */
export function robotState(robot: { readonly status: RobotFacts }): RobotState {
  const status = robot.status;
  const phase = status.phase ?? "Unknown";
  if (status.discovered && status.present === false) return "missing";
  if (phase === "Offline") return "offline";
  if (phase === "NeedsAttention" && status.discovered) return "needs setup";
  if (phase === "NeedsAttention") return "attention";
  if (phase === "Waiting") return "waiting";
  if (status.policy_version_id) return "running";
  if (phase === "Running") return "running";
  if (phase === "Ready") return "ready";
  if (phase === "Preparing") return "preparing";
  return "unknown";
}

export const TERMINAL: ReadonlySet<string> = new Set([
  "live",
  "stopped",
  "cancelled",
]);

export const ROLLOUT_LABELS: Readonly<Record<RolloutState, string>> = {
  "checking": "checking",
  "preparing": "preparing",
  "shadow_testing": "shadow testing",
  "awaiting_approval": "awaiting approval",
  "switching": "switching",
  "live": "live",
  "stopped": "stopped",
  "cancelled": "cancelled",
};

export const ROLLOUT_TONES: Readonly<Record<RolloutState, Tone>> = {
  "checking": "info",
  "preparing": "info",
  "shadow_testing": "info",
  "awaiting_approval": "warn",
  "switching": "info",
  "live": "ok",
  "stopped": "danger",
  "cancelled": "muted",
};

export const STEP_LABELS: Readonly<Record<StepName, string>> = {
  "check": "Fits the robots",
  "prepare": "At the site",
  "shadow": "Shadow test",
  "approve": "Approval",
  "switch": "Switch",
};

export const STEP_TONES: Readonly<Record<StepState, Tone>> = {
  "pending": "muted",
  "running": "info",
  "passed": "ok",
  "skipped": "muted",
  "failed": "danger",
};

export const STEP_MARKS: Readonly<Record<StepState, string>> = {
  "pending": " ",
  "running": "◐",
  "passed": "✓",
  "skipped": "–",
  "failed": "✗",
};

export const ROLLOUT_KINDS: Readonly<Record<RolloutKind, string>> = {
  "deploy": "Deploy",
  "try": "Try",
  "rollback": "Rollback",
};
