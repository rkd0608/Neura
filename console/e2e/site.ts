// The customer's side of the journey: a GPU server running the site agent, robots running the
// robot runtime, and the site operator using the agent's CLI. Each runs the real component.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { REPO } from "./global-setup";

export const OPERATOR_TOKEN = "e2e-operator-credential";
export const ROBOT_PREFIX = "arm-";

export class Site {
  readonly stateDir = mkdtempSync(path.join(tmpdir(), "neura-e2e-agent-"));
  private readonly processes: ChildProcess[] = [];

  constructor(private readonly robots: number) {}

  private env(): NodeJS.ProcessEnv {
    const tokens = Object.fromEntries(
      Array.from({ length: this.robots }, (_, i) => [
        `${ROBOT_PREFIX}${i}`,
        `dev-token-${ROBOT_PREFIX}${i}`,
      ]),
    );
    return {
      ...process.env,
      NEURA_AGENT_STATE_DIR: this.stateDir,
      NEURA_AGENT_CONTROL_PLANE_URL: "http://127.0.0.1:18080",
      NEURA_AGENT_CONTROL_PLANE_INSECURE: "true",
      NEURA_AGENT_LOCAL_PORT: "18090",
      NEURA_AGENT_GATEWAY_LISTEN: "127.0.0.1:18051",
      NEURA_AGENT_GATEWAY_STATUS_LISTEN: "127.0.0.1:18091",
      NEURA_AGENT_STATUS_INTERVAL_S: "0.5",
      NEURA_AGENT_OPERATOR_TOKEN_SHA256: createHash("sha256").update(OPERATOR_TOKEN).digest("hex"),
      NEURA_AGENT_DEV_ROBOT_TOKENS: JSON.stringify(tokens),
    };
  }

  private run(args: string[], extra: NodeJS.ProcessEnv = {}): string {
    return execFileSync("uv", ["run", ...args], {
      cwd: REPO,
      env: { ...this.env(), ...extra },
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
  }

  /** Run the install command exactly as the console shows it, then start the agent. */
  enroll(installCommand: string): string {
    const args = installCommand.trim().split(/\s+/);
    const output = this.run(args);
    this.processes.push(
      spawn("uv", ["run", "neura-agent", "run"], { cwd: REPO, env: this.env(), stdio: "ignore" }),
    );
    return output;
  }

  startRobots(): void {
    this.processes.push(
      spawn(
        "uv",
        [
          "run",
          "neura-sim-robots",
          "--gateway",
          "127.0.0.1:18051",
          "--agent-url",
          "http://127.0.0.1:18090",
          "--robots",
          String(this.robots),
          "--robot-prefix",
          ROBOT_PREFIX,
          "--task-seconds",
          "2",
        ],
        { cwd: REPO, env: this.env(), stdio: "ignore" },
      ),
    );
  }

  pendingApprovals(): { command_id: string; robot_names: string[] }[] {
    try {
      const out = JSON.parse(this.run(["neura-agent", "approvals"])) as {
        items?: { command_id: string; robot_names: string[] }[];
      };
      return out.items ?? [];
    } catch {
      return []; // the agent is still starting
    }
  }

  /** The site operator approves locally, with the credential only the site has. */
  approve(commandId: string, operator: string): string {
    return this.run(["neura-agent", "approve", commandId, "--operator", operator], {
      NEURA_AGENT_OPERATOR_TOKEN: OPERATOR_TOKEN,
    });
  }

  stop(): void {
    for (const child of this.processes.reverse()) child.kill("SIGTERM");
    rmSync(this.stateDir, { recursive: true, force: true });
  }
}
