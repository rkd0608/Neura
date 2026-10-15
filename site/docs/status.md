---
title: Development status
sidebar_position: 6
description: What exists today, what is in progress and what is out of scope, by milestone.
status: Experimental
---

# Development status

Neura is built in milestones, each delivering every layer end to end on real components, with
simulated robots until hardware is attached. Nothing here is a released product.

| Milestone | What it delivers | State |
| --- | --- | --- |
| M1. Foundation | Repository, CI, protocol, management API specification, reason codes, contracts library and CLI, local stack | Built |
| M2. Data plane core | Robot runtime, gateway with leases and admission, scheduler, policy workers, deterministic simulator with fault injection | Built |
| M3. Control plane | Management API on PostgreSQL: organizations, projects, sites, robots, contracts, deployments, operations, events, audit, roles, sign-in, API tokens | Built |
| M4. Site agent | Enrollment, signed desired state, preflight, local approval, task-boundary switches, offline authorization, local site API and CLI | Built |
| M5. Web console | Onboarding to deployment in the browser, live sessions, faults, audit, settings; generated Python SDK | Built |
| M6. Models and GPU sharing | ACT, SmolVLA and π0-class adapters, batching and multi-adapter batching, profiling, benchmark harness | Built; benchmark on a GPU pending |
| M7. Production hardening | Mutual TLS, artifact signing, customer-managed keys, outbound ledger, dashboards, installer and signed upgrades, worker sandbox, chaos and soak testing, these docs | In progress |
| M8. Physical validation | SO-101 class arms through the same platform: fault suite and parity on hardware | Not started |

## Not yet on this site

- **Installation guide.** The installer exists (`deploy/install/install.sh`, with signed agent
  upgrades and rollback); its page is added once it has been run on a clean machine from these
  docs alone.
- **Benchmarks.** The harness runs and a report from a CPU development host exists; the page is
  added with the GPU report, which states its commit, digests, hardware and command, losing cases
  included.

## Out of scope

Training, evaluation as a product, simulators, teleoperation, safety certification, and sharing
GPUs between different customers.

## Status labels

Every page carries exactly one label, shown at its top.

| Label | Meaning |
| --- | --- |
| **Proposed** | Designed, not implemented. Samples are sketches that do not run. |
| **Experimental** | Implemented and testable, not qualified. It may change without notice; known limitations are listed. |
| **Supported** | Passed the conformance, parity and fault suites for a named combination, with linked evidence. |
| **Deprecated** | Still works, scheduled for removal on a stated date. |

Only a page labeled Supported may claim support for a combination. Numbers on pages labeled
otherwise are measurements on a stated setup or are marked as illustrative.
