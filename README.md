# Neura

**NucleiLabs builds infrastructure that makes robot AI easier to run.** Neura, our first product,
lets robotics companies deploy their models across multiple robots using shared GPU servers. It
coordinates inference around each robot's timing requirements and handles model updates, so teams
can scale without rebuilding their serving infrastructure.

Status: in development. Nothing here is a released product. See `docs/neura-design.md` for the
design and the build plan (§16.1).

## Repository layout

| Path | What it is |
| --- | --- |
| `docs/neura-design.md` | The engineering design (internal) |
| `proto/neura/v1/` | Protocol schema: robot ↔ gateway data plane, control plane ↔ site agent |
| `api/openapi.yaml` | Public management API (spec-first) |
| `catalog/reason_codes.yaml` | Every reason code used by every component |
| `schemas/` | Generated JSON Schemas for contract documents |
| `packages/neura-contracts` | Model artifact, robot profile and execution profile contracts; compatibility check; CLI |
| `packages/neura-proto` | Generated protobuf and gRPC bindings (`from neura.v1 import ...`) |
| `packages/neura-control` | Cloud control plane: management API on Postgres, migrations, auth, audit, events (`neura-control`) |
| `packages/neura-agent` | Site agent on customer GPU servers: enrollment, signed desired state, local approval, supervised data plane, local site API (`neura-agent`) |
| `packages/neura-runtime` | Robot runtime: action queue, chunk merging, fencing, state machine, session and gRPC clients |
| `packages/neura-server` | Site data plane: gateway, leases, admission, scheduler, policy worker processes (`neura-gateway`, `neura-worker`) |
| `packages/neura-adapters` | Model adapters: LeRobot ACT, SmolVLA, π0, π0.5 and Diffusion Policy, a mock, synthetic checkpoints, profiler, multi-adapter (LoRA) batching |
| `packages/neura-bench` | GPU-sharing benchmark: each sharing case against the dedicated baselines (`neura-bench`) |
| `packages/neura-sim` | Simulated arms and networks, deterministic site simulator, `neura-sim-robots` |
| `packages/neura-sdk` | Python client for the management API, generated from the spec |
| `console/` | Web console: React + TypeScript, client generated from the spec, end-to-end tests |
| `examples/contracts/` | Example contracts for an SO-101-class arm with ACT and SmolVLA |
| `bench/reports/` | Benchmark reports, one directory per device |
| `deploy/` | Container images and the local Compose stack |
| `tools/` | Code generators and checks |

## Getting started

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), Node 22 with pnpm (console), Docker
with Compose.

```bash
make setup     # install the workspace into .venv
make check     # lint, strict type checks, generated-file checks, tests
make dev       # start Postgres, control plane, site agent (with its data plane), 4 simulated
               # robots, and the web console on http://127.0.0.1:3000
make smoke     # build, start, run a whole pilot end to end, stop

make console-setup   # pnpm install
make console-check   # format, lint (incl. accessibility), strict types, unit tests
make console-e2e     # the whole journey in Chromium against the real backend

make models-setup    # also install PyTorch and LeRobot (about 5 GB), for real models
make test-models     # LeRobot adapters, real worker processes, a smoke run of the benchmark
make bench           # the GPU-sharing benchmark on a GPU; report in bench/reports/<gpu>/
```

`make setup` installs the default environment only; run `make models-setup` again after it to
keep PyTorch and LeRobot.

The dev stack bootstraps an organization and an admin API token (development only; see
`deploy/compose/compose.yaml`). Sign in to the console with that token. The site agent starts unenrolled and the robots wait for an
assignment. `uv run python tools/smoke_api.py` then runs a pilot the way a customer would:

1. Onboard through the API: project, site, contracts, robots, deployment, revision.
2. Enroll the agent with the install command the API returns.
3. Prepare the revision at the site.
4. Request activation and approve it locally (`neura-agent approve`).
5. Check that the robots run the revision.
6. Stop the control plane and check that the site keeps operating, then start it and check that
   the site reconnects.

On the "server" (the `site-agent` container), the operator uses the agent CLI:

```bash
docker compose -f deploy/compose/compose.yaml exec site-agent neura-agent status
docker compose -f deploy/compose/compose.yaml exec site-agent neura-agent approvals
docker compose -f deploy/compose/compose.yaml exec \
  -e NEURA_AGENT_OPERATOR_TOKEN=ntop_dev_operator_public_token \
  site-agent neura-agent approve <command-id> --operator you@site
```

Control-plane tests need PostgreSQL: `docker compose -f deploy/compose/compose.yaml up -d postgres`
then `NEURA_TEST_DATABASE_URL=postgresql://neura:neura@127.0.0.1:5432/neura make test`.

Check a model, robot and execution profile against each other:

```bash
uv run neura-contracts check \
  --artifact examples/contracts/act-so101.artifact.yaml \
  --robot examples/contracts/so101.robot.yaml \
  --profile examples/contracts/act-so101-30hz.profile.yaml
```

The dev stack runs the mock policy with simulated GPU latency. A real site runs one policy worker
process per model on its GPU (the agent's default). Model weights stay at the site: copy a
checkpoint onto the server and import it, and `prepare` loads it on the GPU and measures it there:

```bash
neura-agent import-weights /path/to/checkpoint --artifact my-model.artifact.yaml
```

A site runs an artifact only if a publisher key its operator trusts signed it (design §17.7). The
customer's release process signs, and the site operator trusts the key on the server:

```bash
uv run neura-contracts publisher-key --out release.pem      # once; prints the public key
uv run neura-contracts sign my-model.artifact.yaml --key release.pem   # register with this
neura-agent trust-publisher <public key>                     # on the server
```

To keep weights in a store you do not fully control (a shared bucket, or Neura's registry), seal
them with your own key first; only the site, holding that key, can open them:

```bash
uv run neura-contracts key-file --out weights.kek
uv run neura-contracts seal /path/to/checkpoint --out sealed/ --key-file weights.kek
```

Watch the simulated robots being served:

```bash
curl -s http://127.0.0.1:8091/v1/local/dataplane | python3 -m json.tool
```

## Conventions

- **Generated files are committed and checked.** Edit the source (`proto/`, `catalog/`, contract
  models), run `make gen`, commit both. CI fails on stale output.
- **Spec-first API.** `api/openapi.yaml` is the contract; the service must not expose routes the
  spec does not declare.
- **Reason codes come from the catalog.** Never invent a code inline.
- **Nothing on the robot action path depends on the cloud.**
