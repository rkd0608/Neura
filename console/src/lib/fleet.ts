// The fleet's words for the console's pages (design §19.4, §29): what a robot's state is called,
// what a rollout's steps and states are called, and their tones. They come from
// catalog/fleet.yaml, the same source as the words the `neura` command prints.
import type { Schemas } from "../api/client";
import {
  ROLLOUT_KINDS,
  ROLLOUT_LABELS,
  ROLLOUT_TONES,
  STEP_LABELS as STEPS,
  STEP_TONES,
  type RolloutState,
  type StepState,
} from "../generated/fleet";
import type { Tone } from "./phases";

export {
  ROBOT_TONES as ROBOT_TONE,
  TERMINAL,
  robotState,
  type RobotState,
} from "../generated/fleet";

type Robot = Schemas["Robot"];

/** What a robot runs, as people say it: "pick v3". */
export function running(robot: Robot): string | null {
  const { policy_name: name, policy_version: version } = robot.status;
  return name ? `${name} v${version}` : null;
}

export const STEP_LABELS: Readonly<Record<string, string>> = STEPS;
export const KIND_LABELS: Readonly<Record<string, string>> = ROLLOUT_KINDS;

export function rolloutTone(state: RolloutState): Tone {
  return ROLLOUT_TONES[state];
}

export function rolloutLabel(state: RolloutState): string {
  return ROLLOUT_LABELS[state];
}

export function stepTone(state: StepState): Tone {
  return STEP_TONES[state];
}
