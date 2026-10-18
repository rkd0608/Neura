---
title: The improvement loop
sidebar_position: 11
description: Record what deployed robots do, fine-tune the running model on it at the site, evaluate the result against the running model, and gate activation on the evidence.
status: Experimental
---

# The improvement loop

Neura records what a deployment's robots see and do, fine-tunes the running model on those
recordings, and evaluates the result against the model the robots run. Each of these steps runs
at your site, on your servers. The new model then goes through the usual release flow: prepare,
approve at the site, switch at a task boundary.

**What stays at the site:** episodes (camera frames, robot state, actions, instructions), datasets
and the trained weights. **What reaches Neura:** episode counts by outcome, the new model's
artifact manifest (its file digests and sizes, and its lineage), evaluation verdicts and their
statistics, and training losses. Every field is listed in the
[outbound allowlist](reference/outbound) and recorded in the site's outbound ledger.

## 1. Record episodes

Recording is off until both sides allow it.

1. **The site's operator** sets the most the site may record, in the agent's environment:
   `NEURA_AGENT_CAPTURE_ALLOWED=failures` (faults, takeovers and tasks labeled as failures) or
   `all`. The default is `off`.
2. **The deployment** asks for recording in its spec:

   ```json
   { "spec": { "capture": { "mode": "all", "image_stride": 3 } } }
   ```

   `image_stride` records a camera frame every *n* control periods; commands and robot state are
   recorded every period. The site records the lesser of what the deployment asks and what its
   operator allows.

A robot records only when it has a spool directory (`capture_dir` in `neura-arm`'s configuration).
Recording never slows the control loop: when the robot's disk or writer falls behind, frames are
dropped and counted, and the episode is marked incomplete.

An episode is one task, from arming until it stops, the operator takes over, a fault latches or
the task changes. Label its outcome on the robot (`outcome success|failure NOTE` at the operator
prompt), from an integration such as a task sensor, or later at the site:

```bash
neura-agent episodes
neura-agent label ep_01J... success --operator ops@site --note "placed on the first try"
```

Without a label, a fault or takeover counts as a failure and anything else as unknown.

## 2. Fine-tune at the site

```bash
curl -X POST "$NEURA_API/v1/deployments/$DEPLOYMENT/training-jobs" \
  -H "Authorization: Bearer $NEURA_TOKEN" -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"spec": {"base_revision_id": "rev_...", "episodes": {"outcomes": ["success"]},
       "recipe": {"steps": 2000, "batch_size": 8}}}'
```

The site snapshots the episodes the filter selects (only this deployment's robots and revisions),
trains with LeRobot's training loop from the base revision's checkpoint, and signs the new model
with the site's training key. By default the fine-tune keeps the base model's normalization
statistics (`"normalization": "dataset"` uses the recordings' statistics instead).

The job runs as a sandboxed process, one at a time, on a GPU that serves no robot. If every GPU
serves robots, the job is refused (`AGENT_JOB_DEVICE_BUSY`) unless the operator sets
`NEURA_AGENT_JOB_SHARE_GPUS=true`; `NEURA_AGENT_JOB_DEVICE` pins a device (`cpu`, `cuda:1`).
The operator can turn remote training and evaluation off by leaving `train` and `evaluate` out of
`NEURA_AGENT_ALLOWED_OPERATIONS`.

When the job succeeds, Neura checks the site's result before registering anything: the new
artifact changes only the weights, names its base, this job and its dataset in its lineage, and
carries a valid signature; its execution profile is the base's, bound to the new artifact. Neura
then registers both and adds a **candidate revision** to the deployment. A result that does not
check out registers nothing (`TRAINING_RESULT_REJECTED`).

## 3. Evaluate the candidate

```bash
curl -X POST "$NEURA_API/v1/deployments/$DEPLOYMENT/evaluations" \
  -H "Authorization: Bearer $NEURA_TOKEN" -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"spec": {"candidate_revision_id": "rev_...", "suite": {"kind": "replay"}}}'
```

The baseline defaults to the revision the deployment's robots run. Two suites:

| Suite | What it does | Verdict |
| --- | --- | --- |
| `simulation` | Both models on the same seeded scenes of a simulated task (ALOHA transfer cube or insertion), the world waiting for each model | Paired test on success rates against a margin (0.10 by default): `superior`, `non_inferior`, `inconclusive` or `inferior` |
| `replay` | Both models on observations the robots recorded, each prediction compared with what the robot then did | `non_inferior` when the candidate's error on successful episodes is at most the baseline's × 1.25 + 0.01 of each action's range; `inconclusive` without successful episodes |

A replay catches a candidate that drifts from behavior that worked. It cannot show that a
candidate is better, because the baseline produced the recorded actions; use a simulation suite,
or a canary rollout, for that.

## 4. Gate activation on the evidence

```json
{ "spec": { "release_gates": { "evaluations": ["replay"] } } }
```

With a gate, an activation request is refused (409, `API_RELEASE_GATE_UNMET`, naming what is
missing) until the candidate has a succeeded evaluation of each listed kind, against the revision
each targeted robot runs, with a `superior` or `non_inferior` verdict. Robots that run nothing
yet, or already run the candidate, need no evidence, and neither does a rollback.

In the console, a deployment's **Improve** tab shows the recorded episodes, the fine-tuning jobs
and their losses, the evaluations and their verdicts, and the release gates.

## Limits

- Shadow serving (a candidate served on live observations, its actions never reaching a robot) is
  not built yet.
- The simulation suite covers the ALOHA tasks only; a customer task needs its own simulated scene.
- Recording does not yet capture an operator's corrective motions during a takeover.
