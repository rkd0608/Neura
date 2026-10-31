---
name: verify
description: Prove a change to Neura works before saying it does. Use before reporting that anything is done, fixed or working, before committing, and whenever a change touches the console, the NucleiLabs site or the docs (they need to be looked at, not only tested).
---

# Verify

"Done" means proven. Tests passing is not the same as the product working, and nothing is
reported as working on the strength of having written it.

## 1. Run the checks for what changed

```bash
make verify                     # the checks this diff can break (plan: make verify VERIFY=--plan)
make verify VERIFY=--e2e        # also the console's journey in Chromium, against the real backend
make verify VERIFY=--models     # also the model tests (after make models-setup)
make verify VERIFY=--all        # everything, and the whole test suite
```

`tools/verify.py` works out the checks from the diff against origin/main (commits, staged,
unstaged and new files): lint and types for Python, the tests of every package changed or
depending on one, the generated files, the maps, the file-size budget, and the console, site and
docs checks when they changed, with screenshots of the site and docs. Each check's output is in
`.verify/<check>.log`; the summary is `.verify/summary.md` and ends with what was not verified.

Postgres: tests that need it skip without it. `make verify` uses 127.0.0.1:5432 when it answers;
if it says Postgres is not answering, start it (`service postgresql start`) and run again. A
skipped test proves nothing: read the skip reasons in `.verify/pytest.log`.

The tests run with an empty TMPDIR of their own, and the check fails if anything is left in it:
a test or a process that does not clean up after itself would leave the same on every run, and
on a customer's server.

## 2. A failure is information, not an obstacle

Read the failing check's log from the top of its first error, find the cause (the debug skill),
fix that, and run `make verify` again. Never make a check pass by weakening it: no skipped or
deleted tests, no loosened assertions, no `# type: ignore` or `noqa` without a reason written next
to it, no raising a size budget to fit a file.

## 3. Look at what people will see

Anything a person sees must be looked at, not only tested.

- The NucleiLabs site and the docs: `make shots` (verify runs it when they changed) serves the
  builds the way production's nginx does, with its headers and Content-Security-Policy, opens every
  page in Chromium on a desktop and a phone, and fails on a console error, an exception, a failed
  or refused request, a request to another origin, a broken image, a page wider than a phone, or
  an accessibility violation (axe, WCAG 2.1 AA, e.g. text under the contrast minimum).
  Then open the screenshots in `.verify/shots/` (Read the PNGs) for the pages the change touched,
  at both sizes, and check they look right: layout, spacing, text, both themes.
- The console: `make verify VERIFY=--e2e` runs `console/e2e/journey.spec.ts` (sign in to rollout,
  with accessibility checks on every screen) and keeps a screenshot of each screen in
  `.verify/console/`. Look at the screens the change touched. On failure, Playwright's trace is
  under `console/test-results/`: `cd console && pnpm exec playwright show-trace <trace.zip>`.
- A change to a flow (CLI, API, agent): run the flow, not only its unit tests. The test that does
  is in `docs/features.md`; for a manual run, the launch skill starts the pieces.

## 4. Report what the evidence shows

Say what was run and what it showed, using the summary: which checks passed, any that failed and
why, how many tests ran and skipped, and what was not verified (the summary's last section: e2e,
models, CI-only workflows, the images). If something could not be run here, say so and say what
would run it. Do not round "most checks passed" up to "works".
