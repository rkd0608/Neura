// The hero's illustration: six robots served from one GPU, drawn the way the console draws them.
// It is a simulation in the browser, labeled as such; nothing here is live data.
import { useEffect, useState, useSyncExternalStore } from "react";

interface Robot {
  name: string;
  buffer: number;
  rtt: number;
  state: "Running" | "Waiting";
}

const START: Robot[] = [
  { name: "arm-01", buffer: 44, rtt: 14.8, state: "Running" },
  { name: "arm-02", buffer: 31, rtt: 18.2, state: "Running" },
  { name: "arm-03", buffer: 47, rtt: 12.9, state: "Running" },
  { name: "arm-04", buffer: 22, rtt: 21.4, state: "Running" },
  { name: "arm-05", buffer: 38, rtt: 16.1, state: "Running" },
  { name: "arm-06", buffer: 9, rtt: 33.0, state: "Waiting" },
];

const EVENTS = [
  "chunk accepted · arm-03 · 50 actions",
  "batch of 4 · 11.2 ms on gpu-0",
  "chunk accepted · arm-01 · 50 actions",
  "arm-06 starved: holding position",
  "chunk accepted · arm-06 · resumed",
  "batch of 3 · 9.8 ms on gpu-0",
  "late reply refused · arm-02 · stale epoch",
];

const MOTION = "(prefers-reduced-motion: reduce)";

function useReducedMotion(): boolean {
  return useSyncExternalStore(
    (listener) => {
      const query = window.matchMedia(MOTION);
      query.addEventListener("change", listener);
      return () => query.removeEventListener("change", listener);
    },
    () => window.matchMedia(MOTION).matches,
    () => false,
  );
}

export function FleetDemo() {
  const reduced = useReducedMotion();
  const [robots, setRobots] = useState(START);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (reduced) return;
    const timer = window.setInterval(() => {
      setTick((t) => t + 1);
      setRobots((current) =>
        current.map((r, i) => {
          // Each robot drains one action per tick and is refilled with a fresh chunk when low.
          let buffer = r.buffer - (2 + (i % 3));
          let state: Robot["state"] = "Running";
          if (buffer < 12) {
            buffer = 46 + ((i * 7) % 5);
          }
          if (r.state === "Waiting" && Math.random() < 0.5) state = "Waiting";
          const rtt = Math.max(9, Math.min(34, r.rtt + (Math.random() - 0.5) * 3));
          return { ...r, buffer, rtt, state };
        }),
      );
    }, 700);
    return () => window.clearInterval(timer);
  }, [reduced]);

  const running = robots.filter((r) => r.state === "Running").length;
  const log = Array.from({ length: 4 }, (_, i) => EVENTS[(tick + i) % EVENTS.length] ?? "");

  return (
    <figure
      className="demo"
      aria-label="Illustration: six robots served from one GPU, as the Neura console shows them"
    >
      <div className="demo-bar" aria-hidden="true">
        <span />
        <span />
        <span />
        <span className="demo-title">Neura · Warehouse 3 · deployment pilot</span>
        <span className="demo-live">
          <i /> Live
        </span>
      </div>
      <div className="demo-body" aria-hidden="true">
        <div className="demo-robots">
          <div className="demo-head">
            <span>Robot</span>
            <span>State</span>
            <span>Action buffer</span>
            <span className="r">RTT p99</span>
          </div>
          {robots.map((r) => (
            <div className="demo-row" key={r.name}>
              <span className="mono">{r.name}</span>
              <span className={`demo-state ${r.state === "Running" ? "ok" : "warn"}`}>
                {r.state}
              </span>
              <span className="demo-meter">
                <span style={{ width: `${Math.min(100, (r.buffer / 50) * 100)}%` }} />
              </span>
              <span className="r mono">{r.rtt.toFixed(1)} ms</span>
            </div>
          ))}
        </div>
        <div className="demo-side">
          <div className="demo-gpu">
            <span className="demo-label">gpu-0 · shared</span>
            <span className="demo-big">
              {running}
              <small> / 6 robots running</small>
            </span>
            <span className="demo-label">batch group · act-so101 · 30 Hz</span>
            <span className="demo-meter wide">
              <span style={{ width: `${58 + (tick % 5) * 4}%` }} />
            </span>
          </div>
          <ul className="demo-log">
            {log.map((line, i) => (
              <li key={`${tick}-${i}`} className={i === 0 ? "fresh" : undefined}>
                {line}
              </li>
            ))}
          </ul>
        </div>
      </div>
      <figcaption className="demo-caption">
        Illustration of the console, simulated in your browser.
      </figcaption>
    </figure>
  );
}
