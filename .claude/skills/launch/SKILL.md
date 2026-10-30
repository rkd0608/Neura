---
name: launch
description: Start Neura's pieces on this machine to see a change working for real - the control plane, a site agent with simulated robots, the console, the NucleiLabs site and the docs - and stop them afterwards. Use when a change needs to be run, not only tested, or when asked to run, start, try or screenshot Neura.
---

# Launch

Run the real thing, then look at it. Each recipe says how to tell it is up and how to stop it.
When something does not come up, `make dev-report` says what is running and what failed first.

## A control plane and a bench site, without Docker

The fastest real backend: the control plane in a process, a site agent started by the `neura`
command, and a simulated SO-101. Needs Postgres on 127.0.0.1:5432 (`service postgresql start`).

```bash
NEURA_E2E_DATABASE_URL=postgresql://neura:neura@127.0.0.1:5432/neura \
  uv run python console/e2e/stack.py --state /tmp/neura-stack.json &   # prints READY
jq -r .token /tmp/neura-stack.json | uv run neura login --api http://127.0.0.1:18080 --with-token
uv run neura site create lab --bench
uv run neura site join lab --simulate 1 --mock-policies --control-plane 127.0.0.1:18081
uv run neura robots                     # sim-1 ready
```

Then drive it as `site/docs/deploy-a-policy.md` does (`neura policy push`, `neura try`,
`neura status`, `neura logs`). The agent's files and `agent.log` are in
`~/.local/share/neura/sites/lab/`. Stop: `uv run neura site stop lab`, then stop `stack.py`
(it drops its database when it exits). The token stays in the state file: pipe it, never print it.

## The whole dev stack, in Docker

If `docker info` says the daemon is not running, start it: `dockerd > /tmp/dockerd.log 2>&1 &`.
Behind a TLS-intercepting proxy, image builds need its CA: `NEURA_BUILD_CA_BUNDLE=<ca file> make dev`.
The stack's Postgres takes 127.0.0.1:5432: stop a local one first (`service postgresql stop`).

```bash
make dev          # Postgres, control plane, site agent with its data plane, simulated robots, console
make dev-logs     # follow every service's log
make dev-down     # stop it and delete its data
```

| What | Where |
| --- | --- |
| Console | http://127.0.0.1:3000 (API token: in `deploy/compose/compose.yaml`) |
| Control plane API | http://127.0.0.1:8080/v1/meta, readiness at /readyz |
| Site agent's local API | http://127.0.0.1:8090/v1/local/status |
| Gateway (robots) | 127.0.0.1:50051; its status at http://127.0.0.1:8091/v1/local/dataplane |
| Prometheus, Grafana | 127.0.0.1:9090 and 127.0.0.1:3001 (the `observability` compose profile) |

The agent stays unenrolled until something enrolls it: `uv run python tools/smoke_api.py` runs a
whole pilot through the API. `make smoke` builds, starts, verifies and stops all of it. Building
the images takes several GB of disk; `docker builder prune -f` frees the build cache.

## The console against a running API

```bash
make console-dev     # http://127.0.0.1:5173, proxied to the API on :8080 (NEURA_API_URL to change)
```

Sign in with an API token. For the stack above without Docker, start it with
`NEURA_API_URL=http://127.0.0.1:18080 make console-dev`. The end-to-end tests start their own
backend: `make verify VERIFY=--e2e`.

## The NucleiLabs site and the docs

```bash
make landing-dev     # the site: http://127.0.0.1:5174
make docs            # the docs: http://127.0.0.1:5175/docs/
make shots           # both, built and served like production, every page checked in Chromium
```

`make shots` writes the screenshots to `.verify/shots/`: open the ones the change touched.

## Chromium

Playwright's own browser is used when it is installed. Where it is not, point the tools at
another: `NEURA_CHROMIUM=/path/to/chrome`. `tools/shoot.mjs` falls back to
`/opt/pw-browsers/chromium` when Playwright's will not start; `make verify VERIFY=--e2e` uses
`/opt/pw-browsers/chromium` whenever it exists and `NEURA_CHROMIUM` is not set.
