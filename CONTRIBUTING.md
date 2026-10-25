# Contributing to Neura

Thanks for helping build Neura. This page is how the repository works: how to set it up, what CI
checks, and the few rules every change follows.

## Set up

You need Python 3.13, [uv](https://docs.astral.sh/uv/), Node 22 with pnpm, and Docker with Compose.

```bash
make setup            # the Python workspace, into .venv
make console-setup    # the console's packages
make dev              # the whole stack on your machine; the console on http://127.0.0.1:3000
```

For real models (PyTorch and LeRobot, about 5 GB): `make models-setup`.

## Before you push

`make verify` works out what your change can break from its diff against `origin/main` and runs
those checks: lint, types, the tests of every package you touched and of everything that depends
on it, the generated files, and the console, website and docs checks, with the website and docs
opened in Chromium. It ends with what it did not check. `make verify VERIFY=--e2e` adds the
console's end-to-end journey; `make verify VERIFY=--all` runs everything.

Each part's own commands, which CI runs:

| Changed | Run |
| --- | --- |
| Python packages | `make check` (lint, strict types, generated files, tests) |
| The console | `make console-check`, and `make console-e2e` for flows |
| The website | `make landing-check` |
| The docs | `make docs-check` |
| Models, workers, robots | `make test-models` |

Control-plane tests need Postgres:
`docker compose -f deploy/compose/compose.yaml up -d postgres`, then
`NEURA_TEST_DATABASE_URL=postgresql://neura:neura@127.0.0.1:5432/neura make test`.

## Rules every change follows

- **Generated files are committed and checked.** Edit the source (`proto/`, `catalog/`,
  `api/openapi.yaml`, contract models), run `make gen`, and commit both.
- **The API is spec-first.** Add a route to `api/openapi.yaml` before implementing it; the service
  exposes no route the spec does not declare.
- **Reason codes come from `catalog/reason_codes.yaml`.** Never invent one inline.
- **Nothing on the robot action path depends on the cloud.** No cloud credential can approve an
  activation; only someone at the site can.
- **Claims carry evidence.** A doc page or README line that states a number links the report,
  benchmark or test it comes from, losing cases included. Docs pages carry a status label.
- **Design first for big changes.** Decisions are recorded in [docs/neura-design.md](docs/neura-design.md).
- **Files stay small and say what they are for.** A source file stays under 800 lines
  (`make lint`), and starts with a docstring or comment saying what it is for:
  [docs/codemap.md](docs/codemap.md) is built from those (`make gen`). What each feature touches
  is in [docs/features.md](docs/features.md); update its row when the feature moves.

## Branches and pull requests

`main` always passes CI. Work on a branch, open a pull request against `main`, and fill in its
template. Dependabot opens dependency updates against `main` weekly; each goes through the same CI,
including the dependency audit.

## Reporting a security issue

Please do not open a public issue. See [SECURITY.md](SECURITY.md).
