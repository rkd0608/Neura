// The fleet's vocabulary, shared by the console's pages and the `neura` command (design §29): what
// a robot's state is called, what a rollout's steps and states are called, and their tones.
import type { Schemas } from "../api/client";
import type { Tone } from "./phases";

type Robot = Schemas["Robot"];
type Rollout = Schemas["Rollout"];

const LIVE = new Set(["Ready", "Running", "Serving", "Healthy"]);

export type RobotState =
  "running" | "ready" | "needs setup" | "attention" | "missing" | "offline" | "unknown";

/** What a robot is doing, in one word: the same words `neura robots` prints. */
export function robotState(robot: Robot): RobotState {
  const status = robot.status;
  const phase = status.phase;
  if (status.discovered && status.present === false) return "missing";
  if (phase === "Offline") return "offline";
  if (phase === "NeedsAttention") return status.discovered ? "needs setup" : "attention";
  if (status.policy_version_id) return "running";
  if (LIVE.has(phase)) return "ready";
  return "unknown";
}

export const ROBOT_TONE: Record<RobotState, Tone> = {
  running: "ok",
  ready: "ok",
  "needs setup": "warn",
  attention: "warn",
  missing: "danger",
  offline: "danger",
  unknown: "muted",
};

/** What a robot runs, as people say it: "pick v3". */
export function running(robot: Robot): string | null {
  const { policy_name: name, policy_version: version } = robot.status;
  return name ? `${name} v${version}` : null;
}

export const STEP_LABELS: Record<string, string> = {
  check: "Fits the robots",
  prepare: "At the site",
  shadow: "Shadow test",
  approve: "Approval",
  switch: "Switch",
};

export const TERMINAL = new Set(["live", "stopped", "cancelled"]);

export function rolloutTone(state: string): Tone {
  if (state === "live") return "ok";
  if (state === "stopped") return "danger";
  if (state === "cancelled") return "muted";
  if (state === "awaiting_approval") return "warn";
  return "info";
}

export function rolloutLabel(state: string): string {
  return state.replace(/_/g, " ");
}

export function stepTone(state: string): Tone {
  if (state === "passed") return "ok";
  if (state === "failed") return "danger";
  if (state === "running") return "info";
  return "muted";
}

export const KIND_LABELS: Record<string, string> = {
  deploy: "Deploy",
  try: "Try",
  rollback: "Rollback",
};

/** Rollouts a person at a site must approve, newest first. */
export function awaitingApproval(rollouts: Rollout[]): Rollout[] {
  return rollouts.filter((r) => r.status.state === "awaiting_approval");
}
