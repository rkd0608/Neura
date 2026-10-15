---
title: Fault suite, soak and chaos
sidebar_position: 8
description: How Neura is tested against faults, and how to run the same tests yourself.
status: Experimental
---

# Fault suite, soak and chaos

Neura's first design goal is that it never causes a robot to execute a stale, duplicated,
misinterpreted or unauthorized action, whatever happens to the network, the workers or the cloud.
Three layers of tests check this, and anyone can run them from the repository.

## The invariant monitor

Every test below is watched by an **independent invariant monitor**. It observes a site from the
outside (what each robot sent and received, what the gateway emitted, every command each
controller was given), keeps its own model of what each robot was allowed to accept, and records
a violation when:

- a chunk is accepted that is not for the robot's own session, lease generation and task epoch,
  that answers an older request than the latest, or that is accepted twice;
- a chunk is accepted whose observation was too old, or with the wrong digest, anchor or revision;
- a chunk is accepted that the gateway did not emit under a live lease;
- an action is dispatched out of order, out of range, while not running, while a fault is
  latched, or without an accepted chunk that produced it;
- a latched fault clears without an explicit local reset;
- lease generations fail to increase across gateway restarts.

Mutation tests break each rule on purpose (a runtime that accepts duplicates, a reconnect that
clears a fault, an altered action) and require the monitor to report it.

## The fault suite

Each scenario from the design's fault list runs in the deterministic site simulator, which drives
the same robot runtime, gateway, scheduler and admission code that runs in production, with every
machine on its own clock: stale, duplicate and reordered replies; replies for an old epoch or an
old lease generation; worker crash mid-batch and restart; gateway restart and host freeze;
network delay, partition and loss; missing or skewed cameras; NaN and out-of-range actions;
oversized messages; out-of-memory; operator takeover; a second client for the same robot.

```bash
uv run pytest packages/neura-sim
```

## The soak

`neura-sim-soak` runs the simulated site in virtual time with a seeded fault campaign: about one
fault a minute drawn from the whole list, on random robots, with an operator who finishes tasks,
hands robots back after a takeover and resets latched faults. It tracks availability and resource
use every simulated hour and requires every robot to be running at the end. The same seed
reproduces the same run.

```bash
uv run neura-sim-soak --hours 72 --report soak.json   # about two hours on one core
uv run neura-sim-soak --help                          # faults, models, merge modes, seeds
```

## Chaos against real processes

`neura-chaos` starts a real gateway process with its policy worker processes behind a TCP fault
proxy, serves simulated robots over gRPC, and kills, hangs, freezes, restarts and reloads them
while stalling, dropping and delaying the network:

```bash
uv run neura-chaos --duration 300 --report chaos.json
```

## Where they run

Every CI run includes the fault suite, the mutation tests, a five-minute soak with a fault every
two seconds, and 45 seconds of chaos. A scheduled workflow runs the 72-hour soak for each
reference model and merge mode and 30 minutes of chaos every week.
