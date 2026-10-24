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
  private readonly cleanup: string[] = [];

  constructor(
    private readonly robots: number,
    /** Simulated arms the agent finds and runs itself (design §29.5), instead of robots
     *  started by the test. */
    private readonly simulated = 0,
  ) {}

  private env(): NodeJS.ProcessEnv {
    return {
      ...process.env,
      NEURA_AGENT_STATE_DIR: this.stateDir,
      NEURA_AGENT_CONTROL_PLANE_URL: "http://127.0.0.1:18080",
      NEURA_AGENT_DEVELOPMENT: "true",
      NEURA_AGENT_CONTROL_PLANE_INSECURE: "true",
      NEURA_AGENT_LOCAL_PORT: "18090",
      NEURA_AGENT_GATEWAY_LISTEN: "127.0.0.1:18051",
      NEURA_AGENT_GATEWAY_STATUS_LISTEN: "127.0.0.1:18091",
      NEURA_AGENT_GATEWAY_BACKEND: "simulated",
      NEURA_AGENT_STATUS_INTERVAL_S: "0.5",
      NEURA_AGENT_OPERATOR_TOKEN_SHA256: createHash("sha256").update(OPERATOR_TOKEN).digest("hex"),
      // Robots and the gateway use mutual TLS; the agent issues the simulated robots' keys and
      // certificates here (development only: real robots send a CSR).
      NEURA_AGENT_DEV_ROBOT_CERTIFICATE_DIR: path.join(this.stateDir, "robot-certs"),
      NEURA_AGENT_SIMULATED_ROBOTS: String(this.simulated),
      NEURA_AGENT_SCAN_SERIAL_PORTS: "false",
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

  /** The customer's release process signs the artifact with its publisher key, and the site
   *  operator trusts that key on the server. Returns the signature, for the console. */
  publish(artifact: string): string {
    const key = path.join(this.stateDir, "..", `${path.basename(this.stateDir)}-publisher.pem`);
    const created = this.run(["neura-contracts", "publisher-key", "--out", key]);
    const publicKey = /public_key\s+([0-9a-f]{64})/.exec(created)?.[1];
    if (!publicKey) throw new Error(`no public key in: ${created}`);
    this.run(["neura-agent", "trust-publisher", publicKey, "--note", "e2e release pipeline"]);
    this.cleanup.push(key);
    return this.run(["neura-contracts", "sign", artifact, "--key", key]);
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
          "--assigned",
          "--cert-dir",
          path.join(this.stateDir, "robot-certs"),
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

  /** The machine that pushes policies: its publisher key, trusted by this site's operator. */
  publisherKey(): string {
    const key = path.join(this.stateDir, "..", `${path.basename(this.stateDir)}-cli.pem`);
    const created = this.run(["neura-contracts", "publisher-key", "--out", key]);
    const publicKey = /public_key\s+([0-9a-f]{64})/.exec(created)?.[1];
    if (!publicKey) throw new Error(`no public key in: ${created}`);
    this.run(["neura-agent", "trust-publisher", publicKey, "--note", "e2e developer"]);
    this.cleanup.push(key);
    return key;
  }

  /** `neura policy push`, as a developer runs it. */
  push(file: string, name: string, robot: string, token: string, key: string): string {
    const config = mkdtempSync(path.join(tmpdir(), "neura-e2e-cli-"));
    this.cleanup.push(config);
    return this.run(["neura", "policy", "push", file, "--name", name, "--for", robot], {
      NEURA_API_URL: "http://127.0.0.1:18080",
      NEURA_TOKEN: token,
      NEURA_CONFIG_DIR: config,
      NEURA_PUBLISHER_KEY: key,
    });
  }

  stop(): void {
    for (const child of this.processes.reverse()) child.kill("SIGTERM");
    rmSync(this.stateDir, { recursive: true, force: true });
    for (const file of this.cleanup) rmSync(file, { recursive: true, force: true });
  }
}
