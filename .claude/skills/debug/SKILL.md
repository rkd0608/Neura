---
name: debug
description: Find and fix the root cause of a bug, a failing test or a broken check in Neura, and the class of bug it belongs to, instead of patching the symptom. Use whenever something fails or behaves unexpectedly, before changing any code to make it pass.
---

# Debug

A bug is fixed when its cause is understood, the fix is at the cause, and the kind of mistake
that allowed it can no longer happen silently. Patching where the symptom shows makes the next
bug harder to find.

## 1. Get the evidence before touching code

- The exact command and its full output: the first error, not the last; the whole traceback.
  `make verify` keeps every check's output in `.verify/<check>.log`.
- What was running: `make dev-report` collects the dev stack's containers and logs, what the
  control plane, site agent and data plane answer, every local site (`neura site join`) with its
  `agent.log`, and pulls errors and tracebacks to the top.
- More detail, where it is decided: `NEURA_CONTROL_LOG_LEVEL=debug` (control plane),
  `NEURA_AGENT_LOG_LEVEL=debug` (site agent), `NEURA_LOG_LEVEL=debug` (gateway, policy workers,
  `neura-arm`, the ALOHA cell, `neura-qualify`).
- A process that dies (a policy worker, a profiler, the gateway): its own stderr says why. Get it
  before guessing. A policy worker's output is in its gateway's log: stdout as `worker <name>:`
  lines, stderr as it was written (`packages/neura-server/src/neura_server/backends.py`).
- Tests: `uv run pytest path::test -x -vv --tb=long --log-cli-level=DEBUG`; `--lf` reruns the last
  failures. Warnings are errors here (`filterwarnings = ["error"]`): a warning is a real failure.
- The console: the Playwright trace (`console/test-results/`), the browser console, and the API's
  problem response (`reason` is a reason code from `catalog/reason_codes.yaml`).

## 2. Reproduce it with a failing test

Write the smallest test that fails the way the bug does, in the test file of the module at fault
(`docs/features.md` names it), and watch it fail. If it cannot be reproduced, the cause is not
known yet: keep collecting evidence. A flaky failure is a bug with a cause (timing, ordering,
shared state); find it rather than retrying.

## 3. Trace it to the cause

Follow the wrong value back from where it shows to where it was made. Say the mechanism in one
sentence ("the agent reports the old revision because it reads the state before the switch is
committed"), and check that sentence against the code and the evidence. Then ask why it was
possible: a missing check at a boundary, two copies of the same rule, a type that allows a bad
state, an untested path. That answer is what the fix must change.

## 4. Fix the cause, and its class

- Fix it where the mistake is made, not where it is noticed.
- Close the class: validate at the boundary every caller crosses, make the bad state
  unrepresentable in the type, replace the duplicated rule with the one that already exists, or
  add the property test that would have caught every instance. Search for the same mistake
  elsewhere (`docs/codemap.md`, then grep) and fix those too, or say where they are.
- Never: retry or sleep until it passes, catch and ignore the exception, special-case the input
  that failed, loosen or delete the assertion, skip the test. Each hides the cause and leaves it
  in place.

## 5. Prove it

The failing test passes; the test for the class passes; `make verify` is green (the verify
skill). For anything a person sees, look at it.

## 6. Say what it was

In the commit message and the report: the symptom, the cause, why it was possible, and what
the fix changes so it cannot recur.
