# Working on Neura

Neura deploys trained robot policies to robots from shared GPU servers at the customer's site: a
cloud control plane, a site agent and data plane on the customer's servers, a runtime on each
robot, a CLI, a console, and the NucleiLabs site and docs. Python 3.13 (a uv workspace, one
package per component in `packages/`), React and TypeScript (`console/`, `landing/`), Docusaurus
(`site/`).

## Find your way before reading code

- `docs/codemap.md`: every file and what it is for, by package, with the large files marked.
  Start here instead of listing directories or opening files to see what they are.
- `docs/features.md`: each feature end to end: its code, the tests that prove it, its docs, how
  to see it work.
- `docs/neura-design.md`: why it is built this way. It is long: find the section
  (`grep -n '^##' docs/neura-design.md`) and read that.
- Each file's first lines say what it is for. Read those before reading the rest.

## How to work

1. **Understand.** Find where the change belongs (the maps above) and read the code it touches
   and the design section it cites. Reproduce a bug before deciding what it is.
2. **Plan before editing,** for anything beyond a one-line fix. Write down: what changes in which
   files; what existing code it reuses (search first: `docs/codemap.md`, then grep); how it will be
   proven (which test, which check, which screen to look at). For a bug: its cause, and why it was
   possible. If the plan needs a new abstraction, a fallback or a special case, say why the
   existing code cannot do it.
3. **Build** in the smallest coherent steps, matching the code around it.
4. **Prove it** (the verify skill): `make verify` runs the checks the diff can break and says
   what it did not verify. Anything a person sees gets looked at: `make shots` for the site and
   docs, `make verify VERIFY=--e2e` for the console. A bug gets a failing test first: one.
5. **Report** what the evidence shows: what passed, what failed, how many tests skipped, what was
   not verified and why. Never say it works because it was written.

## Rules

**Never patch blindly.** Find the cause before changing code; fix it where the mistake is made;
then close the class of bug (a check at the boundary, a type that forbids the bad state, one rule
instead of two copies) and look for the same mistake elsewhere. No retries, sleeps, swallowed
exceptions, special-cased inputs, loosened assertions or skipped tests to make something pass.
The debug skill has the procedure and where the evidence is (`make dev-report`, log levels,
`.verify/*.log`).

**Consolidate before adding.** Before writing a helper, fallback, abstraction or special case,
find what already does it and use it, extend it or refactor it. Each fact has one source:

- Reason codes: `catalog/reason_codes.yaml` (Python and TypeScript are generated from it).
- What leaves a site: `catalog/outbound.yaml`.
- The public API: `api/openapi.yaml` (the SDK, the console's types and the reference docs are
  generated from it).
- The protocol: `proto/neura/v1/`.
- Contract documents: inferred by `packages/neura-contracts/src/neura_contracts/inference.py`,
  never hand-written per policy.
- The fleet's words (robot states, rollout states and steps): `catalog/fleet.yaml` (the console's
  and the CLI's are generated from it).
- Design tokens: `console/src/styles/tokens.css`, shared by the console, the site and the docs.

**The open-source boundary.** `neura-contracts`, `neura-proto`, `neura-runtime` and `neura-robots`
are published as `neura-robot` (design §30): they import only each other
(`tools/check_boundary.py`; their tests may reach closed code only behind `pytest.importorskip`).
Closed code may import them, never the reverse. They run on robot computers' own Python, so
3.10 and later: no 3.11+ syntax or library (`asyncio.TimeoutError`, not `TimeoutError`, around
asyncio waits; `neura_contracts.compat.StrEnum`; `typing_extensions.Self`). The published
repository is assembled from them and `open-source/` (its README, docs, CI and packaging) by
`tools/open_source.py`, never edited directly; `make open-source-check` installs it alone on 3.10
and 3.13 and runs its checks, and `make verify` does so whenever it changes.

**Generated files are never edited by hand.** Change the source and run `make gen`
(`make examples` for the example contracts); `make check-generated` fails on anything stale. The
edit hook says when a file you wrote is a source of generated files.

**Keep files small.** A source file stays under 800 lines (`tools/check_sizes.py`, run by
`make lint` and `make verify`). The large files listed in `docs/codemap.md` may not grow: move
code out of them. A new module starts with a docstring (Python) or a comment (TypeScript, CSS,
shell) whose first sentence says what it is for; `docs/codemap.md` is built from those
(`make gen`). When a feature moves or lands, update its row in `docs/features.md`.

**Few tests, each one earning its place.** One test per bug, the smallest that fails without the
fix, added to the test file that already covers that code (`docs/features.md` names it). Prefer
extending an existing test to writing a new one. No tests for docs, configuration, renames,
removed dead code or one-line plumbing, and no matrices of cases where one case proves the point.
A test that would only restate the code is not worth keeping.

**Match the code around you:** its naming, comment density and idiom. Python: ruff (100
columns), mypy strict, tests with warnings as errors. TypeScript: strict, prettier, eslint with
jsx-a11y.

**Never print or commit secrets:** tokens, keys, credentials. Pipe them (the launch skill shows
how); the development tokens in `deploy/compose/compose.yaml` are for the local stack only.

## This machine

- Postgres: tests that need it read `NEURA_TEST_DATABASE_URL`; `make verify` sets it when
  127.0.0.1:5432 answers. Start it with `service postgresql start`.
- Running things: the launch skill (a control plane and a bench site without Docker, the dev
  stack, the console, the site and docs). `make dev-report` when something does not come up.
- `make help` lists every command. `.verify/` holds the logs, summary and screenshots of the last
  run (not committed).
- The edit hook (`.claude/settings.json`, `tools/edit_hook.py`) tells you after each edit when a
  file is over its size budget, does not say what it is for, leaves `docs/codemap.md` stale, or
  is a source of generated files. Act on it in the same change.
