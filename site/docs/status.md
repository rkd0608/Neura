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
| M6. Models and GPU sharing | ACT, SmolVLA and π0-class adapters, batching and multi-adapter batching, profiling, benchmark harness | Built; benchmarked on an NVIDIA L4 |
| M7. Production hardening | Mutual TLS, artifact signing, customer-managed keys, outbound ledger, dashboards, installer and signed upgrades, worker sandbox, supply-chain controls, chaos and soak testing, a security review, these docs | Built; 72-hour soaks with injected faults had no invariant violation |
| M8. Physical validation | SO-101 class arms through the same platform: fault suite and parity on hardware | Built and passing on the emulated arm; the run on hardware is pending |
| M9. Capture | Robots record what they see and do, episodes kept and labeled at the site, datasets, export to LeRobot | Built |
| M10. Evaluate | A candidate model against the running one, at the site: simulation, replay and shadow suites, release gates | Built |
| M11. Improve | Fine-tuning at the site on recorded episodes, signed artifacts with lineage, candidate revisions | Built |
| M12. The loop | The loop in the console and these docs; capture, fine-tune, evaluate and activate on the simulated cell | In progress: console, docs and the cell's data path built; the run with a pretrained policy pending |
| M13. The product | Sites, robots, policies and rollouts: the `neura` command, policies from LeRobot checkpoints, Python classes and containers, rollouts with shadow tests and approval at the site, the console's four pages, workspaces for individuals and companies | Built; the one-line installer, stateful policies and a ROS 2 bridge pending |

## Not yet on this site

- **Installation guide.** The installer exists (`deploy/install/install.sh`, with signed agent
  upgrades and rollback); its page is added once it has been run on a clean machine from these
  docs alone.
- **Benchmarks.** Reports from an NVIDIA L4, with and without vision-language-action models, are
  in the repository (`bench/reports/`), each with its commit, digests, hardware and command,
  losing cases included; their page is not written yet.

## Out of scope

Simulators, teleoperation, safety certification, and sharing GPUs between different customers.
Training and evaluation were out of scope until October 2026; they now run at the customer's site,
on its own data and hardware, and never in Neura's cloud by default.

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
