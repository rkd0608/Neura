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
| `packages/neura-agent` | Site agent that runs on customer GPU servers |
| `packages/neura-runtime` | Robot runtime: action queue, chunk merging, fencing, state machine, session and gRPC clients |
| `packages/neura-server` | Site data plane: gateway, leases, admission, scheduler, policy workers (`neura-gateway`) |
| `packages/neura-sim` | Simulated arms and networks, deterministic site simulator, `neura-sim-robots` |
| `examples/contracts/` | Example contracts for an SO-101-class arm with ACT and SmolVLA |
| `deploy/` | Container images and the local Compose stack |
| `tools/` | Code generators and checks |

## Getting started

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), Docker with Compose.

```bash
make setup     # install the workspace into .venv
make check     # lint, strict type checks, generated-file checks, tests
make dev       # start Postgres, control plane, site agent, gateway and 4 simulated robots
make smoke     # build, start, verify end to end, stop
```

The dev stack bootstraps an organization and an admin API token (development only; see
`deploy/compose/compose.yaml`). `uv run python tools/smoke_api.py` onboards a customer through the
API: project, site, enrollment token, contracts, robots, deployment, revision.

Control-plane tests need PostgreSQL: `docker compose -f deploy/compose/compose.yaml up -d postgres`
then `NEURA_TEST_DATABASE_URL=postgresql://neura:neura@127.0.0.1:5432/neura make test`.

Check a model, robot and execution profile against each other:

```bash
uv run neura-contracts check \
  --artifact examples/contracts/act-so101.artifact.yaml \
  --robot examples/contracts/so101.robot.yaml \
  --profile examples/contracts/act-so101-30hz.profile.yaml
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
