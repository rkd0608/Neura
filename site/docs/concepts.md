---
title: Core concepts
sidebar_position: 3
description: Contracts, deployments and revisions, sessions, leases and task epochs, action chunks and merge modes.
status: Experimental
---

# Core concepts

## Contracts

Three documents describe what runs where. Each has a canonical JSON form and a SHA-256 digest
(RFC 8785 canonicalization), so a site can verify it runs exactly what was approved.

- **Model artifact.** A trained policy: its inputs (cameras, state, optional language), its
  outputs (an action chunk of a given length and features), its weights by digest, its runtime
  image, and the merge modes it was validated for. A publisher signature (Ed25519) says who
  released it; a site runs only artifacts signed by a key its operator trusts.
- **Robot profile.** A robot's command interface (names, units and ranges), cameras, and the fault
  responses its controller can perform (hold position, controlled stop, the customer's own stop
  path).
- **Execution profile.** How a model runs on a robot: control rate, actions per chunk and when to
  ask for the next one, merge mode, freshness limits, starvation response, device and precision.
  It is bound to one artifact and one robot profile by their digests.

A compatibility check (`neura-contracts check`) verifies the three against each other before
anything is deployed.

## Deployments and revisions

A **deployment** puts a model on a set of robots at a site. Each change creates an immutable
**revision**. A revision goes through *prepare* at the site (the agent fetches and verifies the
contracts and weights, starts the worker, measures latency on the real GPU and computes how many
robots the server can serve) and then *activation*, which a site operator approves locally. Each
robot switches at its own task boundary. The previous revision stays warm for rollback.

## Sessions, leases and task epochs

Two fences make sure a robot never acts on something that is no longer meant for it.

- **Lease (owned by the gateway).** A robot opens a session and receives a lease with a
  **generation** number, stored durably at the site, that only ever increases. A second client
  with the same identity is refused while the lease is live. Every chunk carries the generation;
  the robot rejects any other than its own.
- **Task epoch (owned by the robot).** The robot increments its epoch on reset, a new instruction,
  a new revision, an operator takeover, recovery from a fault, and (if the profile asks) resuming
  after starvation. It clears its queue first; chunks for an older epoch are dropped.

## Action chunks, the cursor and merge modes

Within an epoch every action has an index. The robot's **cursor** is the index of the next action
to dispatch; everything before it is history and is never changed or sent again. Each observation
records the cursor; the chunk computed from it covers the indices from there on. When it arrives,
the indices already in the past are trimmed, never executed late, and the rest is merged into the
queue according to the execution profile's **merge mode**:

| Mode | Future actions | Valid for |
| --- | --- | --- |
| `replace` | The new chunk replaces the queued future | Any policy (the reference behavior) |
| `append` | The new chunk is queued after the current one | Policies validated for open-loop whole chunks |
| `blend` | Weighted mix over an overlap window, then the new chunk | Policies validated with blending |
| `rtc` | The model continues a frozen prefix for the measured delay | Artifacts declared real-time-chunking capable |

Changing the merge mode is a change of execution profile, which needs validation again.

## Timing without shared clocks

Machines never compare each other's timestamps. A robot sends a relative budget (how long until
its queue runs dry); the gateway turns it into its own deadline on receipt. The robot checks
freshness on its own clock, and that check is the authority.

## Faults

Waiting too long for actions, an action out of range, a controller that refuses a command or a
lost actuator lock puts the robot in **Faulted**. The fault is latched: late replies,
reconnection and cloud commands do not clear it. Only an explicit local reset does. Every refusal
and fault carries a stable [reason code](./reference/reason-codes.md).
