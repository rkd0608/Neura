// Start the backend (e2e/stack.py) once for the run and stop it afterwards.
import { spawn } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

export const REPO = path.resolve(import.meta.dirname, "../..");
// Outside test-results/, which Playwright empties at the start of a run.
export const STATE_FILE = path.join(tmpdir(), "neura-e2e-stack.json");

export interface StackState {
  api_url: string;
  agent_endpoint: string;
  token: string;
  database: string;
}

export function stackState(): StackState {
  return JSON.parse(readFileSync(STATE_FILE, "utf8")) as StackState;
}

export default async function globalSetup(): Promise<() => Promise<void>> {
  const stack = spawn("uv", ["run", "python", "console/e2e/stack.py", "--state", STATE_FILE], {
    cwd: REPO,
    stdio: ["ignore", "pipe", "inherit"],
  });
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("the backend did not start in 90 s")), 90_000);
    stack.stdout.on("data", (chunk: Buffer) => {
      if (chunk.toString().includes("READY")) {
        clearTimeout(timer);
        resolve();
      }
    });
    stack.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`the backend exited with ${String(code)}`));
    });
  });
  return async () => {
    stack.kill("SIGTERM");
    await new Promise((resolve) => stack.on("exit", resolve));
    rmSync(STATE_FILE, { force: true }); // it holds the run's admin token
  };
}
