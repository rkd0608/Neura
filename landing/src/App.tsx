// The NucleiLabs site. Every claim on it is something the product does today, or is labeled as
// in progress; numbers come from the published test reports.
import { useEffect, useState } from "react";

import { CONSOLE_URL, CONTACT_URL, DOCS_URL } from "./config";
import { FleetDemo } from "./FleetDemo";

export function App() {
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <Nav />
      <main id="main">
        <Hero />
        <WorksWith />
        <Value />
        <HowItWorks />
        <Safety />
        <Sharing />
        <Console />
        <Security />
        <Partner />
      </main>
      <Footer />
    </>
  );
}

function Mark({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <defs>
        <linearGradient id="nl-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#c4b9ff" />
          <stop offset="1" stopColor="#6a52e8" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill="#141220" />
      <g fill="none" stroke="url(#nl-mark)" strokeWidth="1.8">
        <ellipse cx="16" cy="16" rx="11" ry="4.6" />
        <ellipse cx="16" cy="16" rx="11" ry="4.6" transform="rotate(60 16 16)" />
        <ellipse cx="16" cy="16" rx="11" ry="4.6" transform="rotate(-60 16 16)" />
      </g>
      <circle cx="16" cy="16" r="2.6" fill="#c4b9ff" />
    </svg>
  );
}

function Nav() {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  return (
    <header className={`nav${scrolled ? " scrolled" : ""}`}>
      <div className="wrap nav-inner">
        <a href="#top" className="logo" aria-label="NucleiLabs, home">
          <Mark />
          <span>NucleiLabs</span>
        </a>
        <nav aria-label="Site" className="nav-links">
          <a href="#neura">Neura</a>
          <a href="#how">How it works</a>
          <a href="#safety">Safety</a>
          <a href="#security">Security</a>
          <a href={DOCS_URL}>Docs</a>
        </nav>
        <div className="nav-cta">
          <a href={CONSOLE_URL} className="btn ghost">
            Sign in
          </a>
          <a href="#partner" className="btn primary">
            Become a design partner
          </a>
        </div>
      </div>
    </header>
  );
}

function Hero() {
  return (
    <section className="hero" id="top" aria-labelledby="hero-title">
      <div className="hero-glow" aria-hidden="true" />
      <div className="hero-grid" aria-hidden="true" />
      <div className="wrap hero-inner">
        <a className="pill" href="#neura">
          <span className="pill-tag">Neura</span>
          Robot AI serving infrastructure
          <span aria-hidden="true">→</span>
        </a>
        <h1 id="hero-title">
          Run your robot policies
          <br />
          <span className="gradient">across your fleet, on your GPUs.</span>
        </h1>
        <p className="lede">
          NucleiLabs builds infrastructure that makes robot AI easier to run. Neura deploys your
          trained policies to many robots from shared GPU servers at your site, schedules inference
          around each robot&apos;s timing, and handles releases and rollbacks, so your team ships
          robots instead of rebuilding serving infrastructure.
        </p>
        <div className="hero-cta">
          <a href="#partner" className="btn primary large">
            Become a design partner
          </a>
          <a href={DOCS_URL} className="btn large">
            Read the docs
          </a>
        </div>
        <FleetDemo />
      </div>
    </section>
  );
}

function WorksWith() {
  const items = ["LeRobot", "ACT", "SmolVLA", "π0 and π0.5", "Diffusion Policy", "SO-101 arms"];
  return (
    <section className="works-with" aria-label="Works with">
      <div className="wrap">
        <p className="eyebrow center">Serves the policies you already train</p>
        <ul className="logos">
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function Value() {
  const cards = [
    {
      title: "Shared GPU serving",
      text: "Many robots on one GPU server: requests are batched, adapters of one base model run together, and the scheduler orders work by each robot's deadline.",
      tag: "Batching · LoRA · co-location",
    },
    {
      title: "Releases you can trust",
      text: "Every change is an immutable revision, verified and warmed at the site before anything moves. Robots switch at a task boundary. Rolling back is one request.",
      tag: "Prepare · approve · roll out",
    },
    {
      title: "Faults handled at the robot",
      text: "A late, duplicate or out-of-range action is refused before it reaches the controller. Faults latch until a person resets them at the site, and the robot holds.",
      tag: "Fencing · latched faults · holds",
    },
  ];
  return (
    <section className="section" id="neura" aria-labelledby="value-title">
      <div className="wrap">
        <p className="eyebrow">Neura</p>
        <h2 id="value-title" className="h2">
          Deploying a policy shouldn&apos;t mean
          <br />
          building a serving stack.
        </h2>
        <p className="section-lede">
          Robotics teams rebuild the same infrastructure for every installation: inference servers,
          timing, releases, monitoring. Neura is that layer, on your own hardware.
        </p>
        <div className="cards three">
          {cards.map((card) => (
            <article className="card" key={card.title}>
              <span className="mono-tag">{card.tag}</span>
              <h3>{card.title}</h3>
              <p>{card.text}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

const STEPS = [
  {
    n: "01",
    title: "Connect a GPU server",
    text: "One command installs the site agent. It dials out to Neura over TLS: no inbound ports, no VPN. Its key never leaves the server.",
    code: "$ neura-agent enroll --control-plane https://… --token ntk_…\n✓ preflight: driver, GPUs, disk\n✓ enrolled server gpu-01 at Warehouse 3",
  },
  {
    n: "02",
    title: "Describe the model and the robot",
    text: "Contracts say what runs and how: the model's inputs and outputs, the robot's commands and limits, the timing. Mismatches are refused with a reason, never converted silently.",
    code: "$ neura-contracts check act.artifact.yaml so101.robot.yaml act-30hz.profile.yaml\n✓ compatible: 6 actions, 33.3 ms period, replace merge",
  },
  {
    n: "03",
    title: "Deploy, with approval at the site",
    text: "Create a revision; the site prepares it (pulls, verifies signatures, warms, profiles) and estimates capacity. Activation is requested from the cloud and approved by a person at the site.",
    code: "revision rev_…XNPK9A   prepared · 8 robots per batch group\nactivation requested → approved by ops@warehouse\n4 / 4 robots running at their next task boundary",
  },
  {
    n: "04",
    title: "Operate the fleet",
    text: "Live sessions, buffers and round trips per robot, latched faults with their local response, capacity per site, and an audit log of every change.",
    code: "robot-3   Running   buffer 46   rtt p99 15.7 ms\nrobot-2   Running   buffer 44   rtt p99 19.3 ms\nrobot-1   Waiting   holding: starved 0.4 s",
  },
];

function HowItWorks() {
  const [active, setActive] = useState(0);
  const step = STEPS[active] ?? STEPS[0];
  return (
    <section className="section alt" id="how" aria-labelledby="how-title">
      <div className="wrap">
        <p className="eyebrow">How it works</p>
        <h2 id="how-title" className="h2">
          From a trained checkpoint to robots running it.
        </h2>
        <div className="how">
          <ol className="how-steps">
            {STEPS.map((s, i) => (
              <li key={s.n}>
                <button
                  type="button"
                  className={`how-step${i === active ? " active" : ""}`}
                  aria-pressed={i === active}
                  onClick={() => setActive(i)}
                >
                  <span className="how-n">{s.n}</span>
                  <span>
                    <span className="how-title">{s.title}</span>
                    <span className="how-text">{s.text}</span>
                  </span>
                </button>
              </li>
            ))}
          </ol>
          <div className="terminal" aria-live="polite">
            <div className="terminal-bar" aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <pre>
              <code>{step?.code}</code>
            </pre>
          </div>
        </div>
      </div>
    </section>
  );
}

function Safety() {
  const points = [
    [
      "Leases and generations",
      "Only the current session for a robot can serve it; a stale one is fenced off.",
    ],
    ["Task epochs", "A reply computed for an earlier task can never drive the next one."],
    [
      "Latched faults",
      "Late replies, reconnection or a cloud command never clear a fault. Only a person at the site does.",
    ],
    ["Confirmed holds", "The robot reports a stop only when its controller confirms it."],
    [
      "Local authority",
      "The cloud requests; the site approves. Nothing on the action path depends on the cloud.",
    ],
  ];
  return (
    <section className="section" id="safety" aria-labelledby="safety-title">
      <div className="wrap split">
        <div>
          <p className="eyebrow">Safety</p>
          <h2 id="safety-title" className="h2">
            Built so a robot never executes a stale action.
          </h2>
          <p className="section-lede">
            Networks drop, workers crash, gateways restart. Neura is designed around one rule: a
            robot only executes actions that are fresh, in order, within its limits and meant for
            the task it is doing now.
          </p>
          <ul className="checklist">
            {points.map(([title, text]) => (
              <li key={title}>
                <strong>{title}.</strong> {text}
              </li>
            ))}
          </ul>
        </div>
        <div className="evidence" aria-label="Test evidence">
          <div className="evidence-card">
            <span className="big">13,028</span>
            <span>
              faults injected across three 72-hour soak runs, checked by an independent invariant
              monitor
            </span>
          </div>
          <div className="evidence-card">
            <span className="big">0</span>
            <span>invariant violations: no stale, duplicated or unauthorized action executed</span>
          </div>
          <div className="evidence-card">
            <span className="big">21</span>
            <span>
              hardware fault scenarios for SO-101 arms, from network partitions to a servo overload.
              Passing on the emulated arm; qualification on hardware is in progress.
            </span>
          </div>
          <a href={DOCS_URL} className="text-link">
            How the fault suite works →
          </a>
        </div>
      </div>
    </section>
  );
}

function Sharing() {
  return (
    <section className="section alt" aria-labelledby="sharing-title">
      <div className="wrap split reverse">
        <figure
          className="diagram"
          aria-label="Robots, the site gateway, policy workers and the GPU"
        >
          <svg viewBox="0 0 520 360" role="img" aria-hidden="true">
            <defs>
              <linearGradient id="flow" x1="0" x2="1">
                <stop offset="0" stopColor="#a595ff" stopOpacity="0.15" />
                <stop offset="1" stopColor="#a595ff" stopOpacity="0.8" />
              </linearGradient>
            </defs>
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <g key={i}>
                <rect x="16" y={22 + i * 54} width="104" height="36" rx="8" className="node" />
                <text x="68" y={45 + i * 54} className="node-text">
                  robot {i}
                </text>
                <path
                  d={`M120 ${40 + i * 54} C 170 ${40 + i * 54}, 170 180, 210 180`}
                  className="flow"
                  stroke="url(#flow)"
                />
              </g>
            ))}
            <rect x="210" y="118" width="128" height="124" rx="12" className="node accent" />
            <text x="274" y="150" className="node-title">
              gateway
            </text>
            <text x="274" y="174" className="node-text">
              leases
            </text>
            <text x="274" y="194" className="node-text">
              admission
            </text>
            <text x="274" y="214" className="node-text">
              deadline scheduler
            </text>
            <path d="M338 180 L 380 180" className="flow" stroke="#a595ff" />
            <rect x="380" y="98" width="124" height="164" rx="12" className="node" />
            <text x="442" y="126" className="node-title">
              GPU
            </text>
            {[0, 1, 2].map((i) => (
              <g key={i}>
                <rect x="396" y={140 + i * 38} width="92" height="28" rx="6" className="worker" />
                <text x="442" y={158 + i * 38} className="node-text">
                  {["batch · ACT", "LoRA ×8", "co-located"][i]}
                </text>
              </g>
            ))}
          </svg>
        </figure>
        <div>
          <p className="eyebrow">Shared serving</p>
          <h2 id="sharing-title" className="h2">
            One GPU, many robots, each on time.
          </h2>
          <p className="section-lede">
            The gateway admits a robot only if its server can meet the robot&apos;s timing at the
            measured latency, then batches requests across robots, runs per-robot adapters of one
            base model together, and keeps every robot&apos;s action buffer from running dry.
          </p>
          <ul className="checklist">
            <li>
              <strong>Admission from measurements.</strong> Each site profiles the model on its own
              GPU and states how many robots it can serve, with its assumptions.
            </li>
            <li>
              <strong>Graceful when starved.</strong> If replies stop, the robot runs its approved
              response (hold) and waits; it never improvises.
            </li>
            <li>
              <strong>Measured, not promised.</strong> The benchmark harness compares sharing with a
              GPU per robot, losing cases included. GPU results are being published.
            </li>
          </ul>
        </div>
      </div>
    </section>
  );
}

function Console() {
  return (
    <section className="section" aria-labelledby="console-title">
      <div className="wrap">
        <p className="eyebrow center">The console</p>
        <h2 id="console-title" className="h2 center">
          Your whole fleet, as it really is.
        </h2>
        <p className="section-lede center">
          The console shows what the sites observe, never what was merely requested: every state has
          a reason and an age, and stale data looks stale. It cannot start motion.
        </p>
        <div className="screenshot">
          <img
            src="/console-overview.webp"
            alt="The Neura console: a project overview with robots running, live sessions, deployments, charts of action buffers and round trips, and a live activity feed."
            width={1800}
            height={1150}
            loading="lazy"
          />
        </div>
      </div>
    </section>
  );
}

function Security() {
  const items = [
    [
      "Runs on your servers",
      "Policies, observations and weights stay at your site. Only aggregates reach Neura.",
    ],
    [
      "Mutual TLS everywhere",
      "Robots, gateways and agents prove who they are with certificates from your site's own CA.",
    ],
    [
      "Signed artifacts",
      "A site runs only what a publisher it trusts signed; the signature covers weights and image.",
    ],
    [
      "Your keys",
      "Weights can be sealed with keys in your own KMS. Revoke the key and nothing more is opened.",
    ],
    [
      "An outbound ledger",
      "Every message a site sends to Neura is recorded locally, hash-chained, for you to inspect.",
    ],
    [
      "Sandboxed workers",
      "Each policy worker runs in a pinned, unprivileged container with no network.",
    ],
  ];
  return (
    <section className="section alt" id="security" aria-labelledby="security-title">
      <div className="wrap">
        <p className="eyebrow">Security and privacy</p>
        <h2 id="security-title" className="h2">
          You keep your models, your data and the last word.
        </h2>
        <div className="cards three compact">
          {items.map(([title, text]) => (
            <article className="card" key={title}>
              <h3>{title}</h3>
              <p>{text}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function Partner() {
  return (
    <section className="section partner" id="partner" aria-labelledby="partner-title">
      <div className="wrap">
        <div className="partner-card">
          <div className="hero-glow small" aria-hidden="true" />
          <p className="eyebrow center">Design partners</p>
          <h2 id="partner-title" className="h2 center">
            Put Neura on your robots.
          </h2>
          <p className="section-lede center">
            We are working with a small number of robotics teams. We integrate your model and robot,
            run a pilot on 3 to 5 robots at your site against success measures we agree up front,
            and build what you need next.
          </p>
          <div className="hero-cta center">
            <a href={CONTACT_URL} className="btn primary large">
              Talk to us
            </a>
            <a href={DOCS_URL} className="btn large">
              Explore the docs
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="footer">
      <div className="wrap footer-inner">
        <div className="footer-brand">
          <a href="#top" className="logo">
            <Mark size={22} />
            <span>NucleiLabs</span>
          </a>
          <p>Infrastructure that makes robot AI easier to run.</p>
        </div>
        <nav aria-label="Footer" className="footer-links">
          <div>
            <span>Product</span>
            <a href="#neura">Neura</a>
            <a href="#how">How it works</a>
            <a href={CONSOLE_URL}>Console</a>
          </div>
          <div>
            <span>Developers</span>
            <a href={DOCS_URL}>Documentation</a>
            <a href={`${DOCS_URL}quickstart`}>Quickstart</a>
            <a href={`${DOCS_URL}status`}>Development status</a>
          </div>
          <div>
            <span>Company</span>
            <a href="#partner">Design partners</a>
            <a href={CONTACT_URL}>Contact</a>
          </div>
        </nav>
      </div>
      <div className="wrap footer-base">
        <span>© {new Date().getFullYear()} NucleiLabs</span>
        <span>Neura is in development. Nothing here is a released product.</span>
      </div>
    </footer>
  );
}
