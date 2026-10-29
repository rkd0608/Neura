# Code map

<!-- GENERATED FILE: do not edit. Source: each file's docstring or header comment.
     Regenerate with `make gen`; `make check-generated` fails when it is stale. -->

Where everything lives, from what each file says it is for. Read this before
searching: it names the file, and the file's own first lines say more. What a feature
touches end to end (code, tests, docs, how to prove it works) is in
[features.md](features.md); why it is built that way is in
[neura-design.md](neura-design.md).

## The repository

| Directory | What it holds |
| --- | --- |
| `.claude/` | The agent harness: settings (an edit hook) and skills (verify, debug, launch). |
| `.github/` | CI workflows, Dependabot, the README's images and the PR template. |
| `api/` | The public management API, spec first (openapi.yaml): the console, SDK and CLI use it. |
| `bench/` | GPU-sharing benchmark reports (written by neura-bench). |
| `catalog/` | Reason codes and the outbound-data allowlist: the sources of generated code. |
| `console/` | The web console (React, TypeScript), its unit tests and its end-to-end tests. |
| `deploy/` | Dockerfile, the Compose dev stack, nginx, installers, observability, cloud deploys. |
| `docs/` | The engineering design (neura-design.md), this map and the feature map. |
| `examples/` | Example contracts, robots and a site, used by tests and the docs. |
| `landing/` | The NucleiLabs site (React, Vite). |
| `packages/` | The Python workspace: one package per component (below). |
| `proto/` | The protobuf schema (neura.v1): the source of the generated bindings. |
| `qualification/` | Hardware qualification reports (written by neura-qualify). |
| `schemas/` | JSON Schemas for the contract documents (generated). |
| `security/` | Accepted vulnerability exceptions, with reasons and expiry dates. |
| `site/` | The developer docs (Docusaurus), served at /docs/ on the NucleiLabs site. |
| `soak/` | Soak test reports (written by neura-sim-soak). |
| `tests/` | Tests that span packages: the API spec, integration and the tools. |
| `tools/` | Generators, checks, smoke tests and the agent harness's tools. |

## Large files

Over the 800-line budget (tools/check_sizes.py). Search them and read the part you
need; when you change one, move code out of it rather than adding to it.

- `console/src/pages/Improve.tsx` (823 lines)
- `console/src/pages/Release.tsx` (1120 lines)
- `console/src/pages/Settings.tsx` (809 lines)
- `console/src/styles/app.css` (1628 lines)
- `landing/src/landing.css` (865 lines)
- `packages/neura-agent/src/neura_agent/core.py` (1221 lines)
- `packages/neura-agent/tests/test_core.py` (1165 lines)
- `packages/neura-bench/src/neura_bench/suite.py` (826 lines)
- `packages/neura-control/src/neura_control/grpc_site.py` (923 lines)
- `packages/neura-control/src/neura_control/services/rollouts.py` (1090 lines)
- `packages/neura-control/tests/test_site_control.py` (856 lines)
- `packages/neura-robots/src/neura_robots/qualify/harness.py` (1107 lines)

## packages/neura

Neura: the `neura` command, and @neura.policy for bringing your own models.

Under `packages/neura/src/`:

- `neura_cli/__init__.py`: The `neura` command line and the policy authoring API (design §29).
- `neura_cli/__main__.py`: `python -m neura_cli`: the `neura` command.
- `neura_cli/app.py`: The `neura` command (design §29.2).
- `neura_cli/commands/account.py`: `neura login`, `neura logout`, `neura whoami`.
- `neura_cli/commands/local.py`: `neura approve` (at the site) and `neura doctor` (on any machine).
- `neura_cli/commands/policies.py`: `neura policies`, `neura policy push|show`, `neura check`: what robots can run.
- `neura_cli/commands/rollouts.py`: `neura deploy|try|rollback|status|cancel|logs`: changing what robots run, and watching it.
- `neura_cli/commands/sites.py`: `neura site ...` and `neura robots`: where robots are, and the robots there.
- `neura_cli/config.py`: Where the CLI keeps what it needs between commands: the API's address, a token, the workspace, the publisher key it signs with, and the sites this machine runs.
- `neura_cli/keys.py`: The publisher key this machine signs policy versions with (design §17.3).
- `neura_cli/local_site.py`: A site agent run by `neura site join` on this machine, for this user (design §29.5).
- `neura_cli/session.py`: A signed-in CLI: its API client, its workspace, and names turned into identifiers.
- `neura_cli/sources.py`: What `neura policy push` and `neura check` accept, and what they make of it (design §29.3).
- `neura_cli/ui.py`: How the CLI talks to people (design §29, "Rules that keep it clean").
- `neura_cli/weights.py`: Getting a version's files to its sites, without Neura ever reading them (design §18).

Tests, under `packages/neura/tests/`: `test_cli_offline.py`

## packages/neura-adapters

Neura model adapters: the interface policy workers run, a deterministic mock, LeRobot policies (ACT, SmolVLA, pi0) and multi-adapter batching.

Under `packages/neura-adapters/src/`:

- `neura_adapters/__init__.py`: Neura model adapters (design §12).
- `neura_adapters/base.py`: The adapter interface policy workers run (design §12).
- `neura_adapters/cli.py`: ``neura-adapters`` command line.
- `neura_adapters/lerobot.py`: LeRobot policies (ACT, SmolVLA, pi0, pi0.5, Diffusion Policy) as Neura adapters (§2.2, §12).
- `neura_adapters/lora.py`: Multi-adapter batching: one base forward pass serving requests for different LoRA adapters (design §9.5).
- `neura_adapters/mock.py`: A deterministic policy for tests, the simulator and development stacks.
- `neura_adapters/profiler.py`: Measuring an adapter on its device: latency per batch size and peak memory (design §9.2).
- `neura_adapters/python.py`: Policies brought as Python classes: ``@neura.policy`` (design §29.3).
- `neura_adapters/synthetic.py`: Checkpoints with the right architecture and random weights, for tests and benchmarks.

Tests, under `packages/neura-adapters/tests/`: `test_lerobot.py`, `test_lora.py`, `test_python_adapter.py`

## packages/neura-agent

Neura site agent: enrollment, reconciliation, artifact verification and the local site API.

Under `packages/neura-agent/src/`:

- `neura_agent/__init__.py`: Neura site agent.
- `neura_agent/__main__.py`: ``neura-agent`` command line.
- `neura_agent/agent_tls.py`: The agent's TLS client certificate for its stream to the control plane (design §17.2).
- `neura_agent/app.py`: Local site API served by the agent (design §19.5).
- `neura_agent/artifacts.py`: The site's artifact store: model weight files, content-addressed and verified (design §13, §18).
- `neura_agent/assets/episodes.css`: The episode viewer's styles: a plain page in the system's light or dark scheme.
- `neura_agent/assets/episodes.js`: The site's episode viewer: lists what this site's robots recorded, shows an episode's camera frames and commands step by step, and labels or deletes it.
- `neura_agent/capture.py`: The site's episode store and the capture service robots upload to (design §28.2).
- `neura_agent/connection.py`: The agent's outbound connection to the control plane (design §17.3).
- `neura_agent/connectivity.py`: Outbound reachability of the control plane.
- `neura_agent/core.py`: The site agent's decisions (design §13, §16, §17).
- `neura_agent/episode_viewer.py`: The site's episode viewer (design §28.2): what a robot recorded, for the site's operator to watch and label.
- `neura_agent/gateway.py`: Turning committed state into the data plane's configuration, and supervising the gateway.
- `neura_agent/identity.py`: The agent's on-host identity: an Ed25519 key generated here, and what enrollment returned.
- `neura_agent/jobs.py`: Evaluation and training jobs at the site (design §28.3, §28.4).
- `neura_agent/launch.py`: The stable launcher systemd runs (design §17.3): ``neura-agent-launch``.
- `neura_agent/ledger.py`: The outbound ledger (design §18.2): every message this server sent to Neura, exactly as sent.
- `neura_agent/metrics.py`: Prometheus metrics for the site agent, on its local API at ``/metrics`` (design §18.2).
- `neura_agent/outbound.py`: Enforcing the outbound schema (design §18.2).
- `neura_agent/placement.py`: Where a revision's batch group runs, and what it measures there (design §9.4, §9.5, §13).
- `neura_agent/preflight.py`: Preflight checks run before enrollment and reported continuously (design §17.2).
- `neura_agent/registry.py`: Sealed weights from Neura's registry (design §18.2), as the artifact source ``neura``.
- `neura_agent/releases.py`: Agent releases (design §17.3): signed bundles, installed side by side, switched atomically and rolled back automatically.
- `neura_agent/robots.py`: The robots this server drives (design §29.5): found or configured, reported, and run.
- `neura_agent/runner.py`: Runs the agent: waits for enrollment, then keeps the control connection, status reporting, activation watching and authorization enforcement going.
- `neura_agent/settings.py`: Site-agent configuration, read from ``NEURA_AGENT_*`` environment variables.
- `neura_agent/site_pki.py`: The site's certificate authority (design §17.3): mutual TLS between robots and the gateway.
- `neura_agent/state.py`: The agent's committed local state, persisted atomically as JSON.
- `neura_agent/trust.py`: Publisher keys this server trusts (design §17.3, §17.4).
- `neura_agent/upgrade.py`: Signed agent upgrades with automatic rollback (design §17.3).

Tests, under `packages/neura-agent/tests/`: `test_agent.py`, `test_artifacts.py`, `test_core.py`, `test_episode_viewer.py`, `test_outbound.py`, `test_preflight.py`, `test_robots.py`

## packages/neura-bench

Neura GPU-sharing benchmarks: shared serving against dedicated baselines (design §9.5, §10).

Under `packages/neura-bench/src/`:

- `neura_bench/__init__.py`: Neura GPU-sharing benchmarks (design §9.5, §10).
- `neura_bench/background.py`: A non-policy compute job for the "concurrent GPU job" workload (design §10): matrix multiplications back to back on the device, until killed or ``--seconds`` pass.
- `neura_bench/cli.py`: ``neura-bench``: the GPU-sharing benchmark (design §9.5, §10).
- `neura_bench/deployments.py`: Contracts and checkpoints for benchmark and test deployments.
- `neura_bench/measure.py`: Measurements on the benchmark device, through the same worker processes production runs.
- `neura_bench/metrics.py`: The benchmark metrics of design §10, computed the same way for simulated and real-time runs.
- `neura_bench/realtime.py`: Real-time runs: the production gateway with real policy worker processes on the device, and simulated robots running the production robot runtime over gRPC (design §10).
- `neura_bench/report.py`: Benchmark reports: results as JSON, and the Markdown report generated from it.
- `neura_bench/sim.py`: Discrete-event simulation of a site serving many robots from several batch groups on several devices, with latency measured on the real device (design §9.5, §10).
- `neura_bench/suite.py`: The GPU-sharing benchmark suite (design §9.5, §10, §16 step 0).

Tests, under `packages/neura-bench/tests/`: `test_report.py`, `test_sim.py`

## packages/neura-contracts

Neura contracts: model artifacts, robot profiles, execution profiles and compatibility checks.

Under `packages/neura-contracts/src/`:

- `neura_contracts/__init__.py`: Neura contracts: the documents that bind a policy to a robot, and their compatibility check.
- `neura_contracts/artifact.py`: Model artifact manifest (design §4.1).
- `neura_contracts/authoring.py`: ``@neura.policy``: any model, in any framework, as a Neura policy (design §29.3).
- `neura_contracts/canonical.py`: Canonical JSON (RFC 8785, JSON Canonicalization Scheme) and content digests.
- `neura_contracts/cli.py`: ``neura-contracts`` command line: validate documents, print digests, check compatibility, sign artifacts and seal weights.
- `neura_contracts/compatibility.py`: Compatibility check between a model artifact, a robot profile and an execution profile (§4.4).
- `neura_contracts/core.py`: Shared vocabularies and constrained types for Neura contracts.
- `neura_contracts/documents.py`: Loading contract documents from YAML or JSON text.
- `neura_contracts/execution.py`: Execution profile (design §4.3): one model artifact bound to one robot profile.
- `neura_contracts/identifiers.py`: ULIDs: 128-bit identifiers that sort by creation time, in Crockford base32 (26 characters).
- `neura_contracts/inference.py`: Contract documents inferred, so that nobody writes them by hand (design §29).
- `neura_contracts/pki.py`: Certificates for Neura's mutual TLS (design §17.2, §17.3, §18.2).
- `neura_contracts/robot.py`: Robot profile (design §4.2).
- `neura_contracts/sealing.py`: Sealed weights: weight files encrypted with a customer-managed key (design §18.1, §18.2).
- `neura_contracts/signing.py`: Publisher signatures on model artifacts (design §17.3, §17.4, §18.2).
- `neura_contracts/site_protocol.py`: Constants the control plane and the site agent must agree on byte for byte (design §17.3).
- `neura_contracts/units.py`: Explicit unit conversions an execution profile may declare (design §4.3, §4.4).
- `neura_contracts/outbound_schema.py`: generated by tools/gen_outbound.py, from catalog/outbound.yaml
- `neura_contracts/reason_codes.py`: generated by tools/gen_reason_codes.py, from catalog/reason_codes.yaml

Tests, under `packages/neura-contracts/tests/`: `test_canonical.py`, `test_cli.py`, `test_compatibility.py`, `test_documents.py`, `test_inference.py`, `test_models.py`, `test_pki.py`, `test_sealing.py`, `test_signing.py`

## packages/neura-control

Neura cloud control plane: management API, metadata, audit and site coordination.

Under `packages/neura-control/src/`:

- `neura_control/__init__.py`: Neura cloud control plane.
- `neura_control/__main__.py`: ``neura-control`` command line.
- `neura_control/api/__init__.py`: HTTP API routers.
- `neura_control/api/auth.py`: Request authentication: API tokens and OIDC access tokens (design §20.7).
- `neura_control/api/common.py`: Shared API plumbing: transactions, idempotency, ETags, pagination, bodies, serialization.
- `neura_control/api/routes_access.py`: Members, API tokens and support access grants (design §18.2, §19.2, §20.7).
- `neura_control/api/routes_contracts.py`: Registered contract documents and compatibility checks (design §4).
- `neura_control/api/routes_core.py`: Organizations, projects, sites, enrollment tokens, servers and robots.
- `neura_control/api/routes_deployments.py`: Deployments, revisions, preparation, activation requests, rollbacks and operations.
- `neura_control/api/routes_fleet.py`: The workspace, policies and their versions, rollouts, activity and publisher keys (design §29).
- `neura_control/api/routes_improvement.py`: Evaluations and training jobs: a site evaluates and improves its own models (design §28.3, §28.4).
- `neura_control/api/routes_login.py`: Signing the CLI in through the browser (device authorization, after RFC 8628).
- `neura_control/api/routes_observe.py`: Sessions, faults, metrics, the change-event stream and audit records.
- `neura_control/api/routes_weights.py`: Neura's registry of sealed weights (design §18.1, §18.2): upload, list, inspect, delete.
- `neura_control/api/schemas.py`: Request bodies, mirroring the request schemas in api/openapi.yaml.
- `neura_control/api/scope.py`: Resolving the organization and project a resource belongs to, with authorization.
- `neura_control/api/serializers.py`: Database rows to API representations (api/openapi.yaml component schemas).
- `neura_control/app.py`: Control-plane HTTP application.
- `neura_control/clock.py`: Wall-clock helpers for persisted timestamps (always UTC).
- `neura_control/database.py`: Database connectivity checks.
- `neura_control/db/__init__.py`: Control-plane persistence.
- `neura_control/db/engine.py`: Async engine and session factory.
- `neura_control/db/locks.py`: Row locks shared by the services that change the same rows.
- `neura_control/db/migrate.py`: Programmatic Alembic migrations (``neura-control migrate``).
- `neura_control/db/migrations/env.py`: Alembic environment: runs migrations synchronously with the psycopg driver.
- `neura_control/db/models.py`: Control-plane schema (PostgreSQL).
- `neura_control/grpc_site.py`: The control plane's side of the site-agent protocol (design §17.2-§17.3, §20.8).
- `neura_control/ids.py`: Typed resource identifiers: ``<prefix>_<ULID>`` (design §20.3, api/openapi.yaml).
- `neura_control/installer.py`: The one-line installer (design §29.2): what a production server's administrator runs to install the site agent and connect it to a site.
- `neura_control/metrics.py`: Prometheus metrics for the control plane, served on their own listener (``metrics_listen``), apart from the public API.
- `neura_control/problems.py`: RFC 9457 problem details carrying Neura reason codes (design §20.5).
- `neura_control/security/__init__.py`: Authentication and authorization.
- `neura_control/security/agent_tokens.py`: Verifying the short-lived tokens site agents sign with their enrolled Ed25519 keys.
- `neura_control/security/oidc.py`: OIDC access-token verification for people signing in through customer SSO (design §20.7).
- `neura_control/security/principal.py`: Who is calling and what they may do (design §19.2, §20.7).
- `neura_control/security/signing.py`: Signing desired-state manifests (design §17.3, §20.8).
- `neura_control/security/tokens.py`: Secrets shown once: API tokens and enrollment tokens.
- `neura_control/services/__init__.py`: Business logic shared by the API and site reports.
- `neura_control/services/bootstrap.py`: Creating the first organization, memberships and tokens (CLI and development bootstrap).
- `neura_control/services/contracts.py`: Registering contract documents from inside the control plane: the execution profiles a rollout derives and the robot profiles a site's agent reports (design §29).
- `neura_control/services/inventory.py`: Robots a site's agent found (design §29.5): registered without anyone creating them.
- `neura_control/services/manifests.py`: Desired-state manifests for sites (design §17.3, §20.8).
- `neura_control/services/operations.py`: Starting site-dependent work: an Operation and the command its site receives (design §20.4).
- `neura_control/services/records.py`: Audit records and change events, written in the same transaction as each change.
- `neura_control/services/release_gates.py`: Release gates (design §28.3): the evidence a revision needs before robots switch to it from the revision they run (a simulation, a replay on recorded episodes, or a shadow on live ones).
- `neura_control/services/revisions.py`: What a revision records about its documents: their compatibility and the revision's digest.
- `neura_control/services/rollouts.py`: Rollouts: one change to what some robots run, from a policy version to robots switched (design §29.4).
- `neura_control/services/sealed_weights.py`: Neura's registry of sealed weights (design §18.1, §18.2).
- `neura_control/services/site_reports.py`: Applying what sites report: the only path that writes observed state (design §20.2, §20.8).
- `neura_control/services/workspace.py`: Workspaces: an organization and the project its sites, robots and policies live in.
- `neura_control/settings.py`: Control-plane configuration, read from ``NEURA_CONTROL_*`` environment variables.
- `neura_control/db/migrations/versions/`: the database migrations

Tests, under `packages/neura-control/tests/`: `conftest.py`, `spec_validator.py`, `test_api_conventions.py`, `test_app.py`, `test_fleet.py`, `test_improvement.py`, `test_improvement_e2e.py`, `test_members.py`, `test_metrics.py`, `test_neura_cli.py`, `test_onboarding_flow.py`, `test_sdk.py`, `test_sealed_weights.py`, `test_security.py`, `test_shadow_e2e.py`, `test_site_agent_e2e.py`, `test_site_control.py`

## packages/neura-learn

Neura's evaluate and improve layers: simulation and replay evaluations of a candidate against the running model, and fine-tuning on episodes recorded at the site.

Under `packages/neura-learn/src/`:

- `neura_learn/__init__.py`: Neura's evaluate and improve layers (design §28.3, §28.4): comparing a candidate model with the one it would replace, at the site, and fine-tuning models on the site's recorded episodes.
- `neura_learn/__main__.py`: `python -m neura_learn`: runs an evaluation or training job (see neura_learn.jobs).
- `neura_learn/evaluate.py`: Evaluating a candidate model against the model it would replace (design §28.3).
- `neura_learn/jobs.py`: Evaluation and training jobs, as the site agent runs them (design §28.3, §28.4).
- `neura_learn/stats.py`: The statistics a release decision rests on (design §28.3).
- `neura_learn/train.py`: Fine-tuning a policy on the site's episodes (design §28.4), with LeRobot's own training loop.

Tests, under `packages/neura-learn/tests/`: `test_aloha_loop.py`, `test_learn.py`, `test_site_jobs.py`, `test_stats.py`

## packages/neura-proto

Generated protobuf and gRPC bindings for the Neura protocol (neura.v1).

Under `packages/neura-proto/src/`:

- `neura/__init__.py`: Neura.
- `neura/v1/`: generated by tools/gen_proto.py, from proto/

## packages/neura-robots

Neura robot integrations: the SO-101 arm over LeRobot, a servo bus emulator, the ALOHA tasks in physics simulation, the robot-side process for arms and the hardware qualification harness.

Under `packages/neura-robots/src/`:

- `neura_robots/__init__.py`: Neura robot integrations (design §3, M8).
- `neura_robots/aloha/__init__.py`: The ALOHA bimanual tasks of gym-aloha (transfer cube, insertion) on the MuJoCo bindings alone.
- `neura_robots/aloha/arm.py`: The simulated ALOHA as a robot integration: what ``neura-arm``'s process, the runtime and the qualification harness see of a real arm (``So101Arm``), on the MuJoCo scene.
- `neura_robots/aloha/cell.py`: ``neura-aloha-cell``: a robot cell of simulated ALOHAs served by a Neura site, running their task back to back, and a page to watch them.
- `neura_robots/aloha/contracts.py`: Contracts for serving an ALOHA policy to the simulated ALOHA: the robot profile (from the scene's actuators), the model artifact (from the checkpoint's own configuration and files) and an execution …
- `neura_robots/aloha/fidelity.py`: Step-for-step comparison of ``neura_robots.aloha`` with gym-aloha itself.
- `neura_robots/aloha/world.py`: Time for the simulated ALOHA.
- `neura_robots/arm.py`: What the robot-side process (``process.ArmProcess``) needs of an arm integration: the runtime's Controller and SensorSource, plus recovery, revalidation and telemetry.
- `neura_robots/cameras.py`: Cameras for robot integrations.
- `neura_robots/cli.py`: ``neura-arm``: serve a real arm through a site gateway.
- `neura_robots/emulator.py`: An emulated SO-101 servo bus: six Feetech STS3215 servos behind a pseudo-terminal.
- `neura_robots/evidence/__init__.py`: Evidence: published policies, on the scenes they were trained for, served by Neura and by the alternatives a robotics team would otherwise use, with the same seeds and the same faults.
- `neura_robots/evidence/cli.py`: ``neura-evidence``: published policies on the scenes they were trained for, served by Neura and by the alternatives a robotics team would otherwise use, with the same seeds and the same faults.
- `neura_robots/evidence/faults.py`: The same faults for every remote system, on the same real-time world and the same TCP fault proxy between the robot and whatever serves it.
- `neura_robots/evidence/lerobot_async.py`: LeRobot's own remote inference (``lerobot.async_inference``, LeRobot 0.6.1) on the simulated ALOHA: its ``PolicyServer`` in a separate process and its ``RobotClient`` driving the robot, both …
- `neura_robots/evidence/local.py`: The in-process baselines: the policy and the scene in one process, no Neura.
- `neura_robots/evidence/policy.py`: A LeRobot checkpoint, run the way ``lerobot-eval`` runs it (LeRobot 0.6): the environment's observation through ``preprocess_observation``, the checkpoint's own preprocessor, ``select_action`` (which …
- `neura_robots/evidence/results.py`: Episode results and their summaries.
- `neura_robots/evidence/served.py`: Episodes served by Neura: the simulated ALOHA behind the robot runtime and ``neura-arm``'s process, a site gateway with its policy worker processes (``qualify.harness.LocalSite``), and gRPC over TCP …
- `neura_robots/evidence/synthetic.py`: A LeRobot ACT checkpoint shaped like the published ALOHA ones (the top camera at 640x480, the 14-value state, 14 actions, chunks of 100), with random weights: for testing the evidence pipeline where …
- `neura_robots/lock.py`: One actuator authority per robot (design §6.3).
- `neura_robots/process.py`: The robot-side process for an arm (design §7): the runtime, the session client and the gateway connection around a connected arm (``arm.Arm``: the SO-101, or the simulated ALOHA), driven by a local …
- `neura_robots/qualify/cli.py`: ``neura-qualify``: qualify a robot integration on hardware (design §7.1).
- `neura_robots/qualify/episodes.py`: Closed-loop parity: operator-scored episodes served by Neura and by LeRobot alone (design §7.1).
- `neura_robots/qualify/harness.py`: Hardware qualification: the fault suite and open-loop parity on a real arm (design §7.1).
- `neura_robots/qualify/reference.py`: The policy as LeRobot runs it on its own: the reference for parity (design §10).
- `neura_robots/so101.py`: The SO-101 arm as a Neura robot integration (design §3, §6.3, §7).

Tests, under `packages/neura-robots/tests/`: `test_aloha.py`, `test_arm_cli.py`, `test_capture_arm.py`, `test_cell.py`, `test_emulator.py`, `test_evidence.py`, `test_process.py`, `test_qualify.py`, `test_so101.py`

## packages/neura-runtime

Neura robot runtime: action queue, chunk merging, fencing, state machine and session client.

Under `packages/neura-runtime/src/`:

- `neura_runtime/__init__.py`: Neura robot runtime.
- `neura_runtime/action_queue.py`: The robot's queue of future actions and the chunk merge rules (design §5).
- `neura_runtime/binding.py`: A verified (artifact, robot profile, execution profile) triple and its explicit transforms.
- `neura_runtime/capture/__init__.py`: Episode capture on the robot (design §28.2): record, spool, upload to the site.
- `neura_runtime/capture/episode.py`: The episode format a robot records and the site stores (design §28.2): ``neura-episode/1``.
- `neura_runtime/capture/recorder.py`: Episode recording on the robot (design §28.2).
- `neura_runtime/capture/spool.py`: The robot's episode spool: episodes written to disk by a background thread (design §28.2).
- `neura_runtime/capture/testing.py`: Synthetic episodes for tests of anything that reads them (site store, export, training, replay evaluation), written through the same spool robots use.
- `neura_runtime/capture/uploader.py`: Uploads finished episodes from the robot's spool to the site (design §28.2).
- `neura_runtime/cli.py`: ``neura-robot``: robot-side tools.
- `neura_runtime/grpc_client.py`: asyncio/gRPC adapter that moves ``SessionClient`` messages to and from a site gateway.
- `neura_runtime/interfaces.py`: Integration points a supported robot integration implements (design §3, §7).
- `neura_runtime/runtime.py`: The robot runtime: the only execution authority in Neura (design §5-§8).
- `neura_runtime/session.py`: Session handshake, lease heartbeats and RTT measurement around a ``RobotRuntime``.
- `neura_runtime/tensors.py`: Encoding and validating ``neura.v1.Tensor`` payloads (row-major, little-endian).
- `neura_runtime/testing.py`: Test doubles for integrations: an in-memory controller and sensor source.

Tests, under `packages/neura-runtime/tests/`: `test_action_queue.py`, `test_capture.py`, `test_grpc_client.py`, `test_runtime.py`, `test_session.py`

## packages/neura-sdk

Python client for the Neura management API, generated from its OpenAPI spec.

Under `packages/neura-sdk/src/`:

- `neura_sdk/__init__.py`: Python client for the Neura management API (generated from api/openapi.yaml).
- `neura_sdk/client.py`: The transport under the generated operations: authentication, idempotency, concurrency preconditions, retries, errors, pagination, operations and the change-event stream.
- `neura_sdk/_operations.py`: generated by tools/gen_sdk.py, from api/openapi.yaml
- `neura_sdk/models.py`: generated by tools/gen_sdk.py, from api/openapi.yaml

## packages/neura-server

Neura site data plane: gateway, leases, admission, scheduler and policy workers.

Under `packages/neura-server/src/`:

- `neura_server/__init__.py`: Neura site data plane: gateway, leases, admission, scheduler and policy workers.
- `neura_server/__main__.py`: Entry point: ``neura-gateway --config site.yaml``.
- `neura_server/admission.py`: Admission control: refuse a session the admitted load could not serve in time (design §9.3).
- `neura_server/backends.py`: What runs a batch group's forward passes, as seen from the gateway's dispatcher.
- `neura_server/capacity.py`: Capacity estimates: "this server can run N robots at this profile" (design §9.3, §9.5, §19.3).
- `neura_server/capture/__init__.py`: Episode capture at the site (design §28.2): the episode store, the upload service robots send finished episodes to, and the export to a LeRobot dataset for training.
- `neura_server/capture/export.py`: Turning site episodes into a LeRobot dataset (design §28.4), the format training reads.
- `neura_server/capture/service.py`: The site's capture service (design §28.2): robots upload finished episodes here.
- `neura_server/capture/store.py`: The site's episode store (design §28.2): every episode robots uploaded, on the site's disk.
- `neura_server/config.py`: Gateway configuration file (YAML) and the objects built from it.
- `neura_server/gateway.py`: The site gateway core (design §6, §8, §11, §12).
- `neura_server/grpc_server.py`: The gateway process: gRPC adapter, batch dispatchers, lease sweeper and local status API.
- `neura_server/inputs.py`: Validating observations against the active contract and decoding them into model inputs.
- `neura_server/leases.py`: Robot leases: who may be served (design §6.1).
- `neura_server/metrics.py`: Prometheus metrics for the gateway, on its status listener at ``/metrics`` (design §18.2).
- `neura_server/profiles.py`: Inference latency profiles indexed by batch size (design §9.2, §13).
- `neura_server/sandbox.py`: Running policy workers (design §12): customer model code is untrusted executable code.
- `neura_server/scheduler.py`: Per-batch-group request scheduling (design §9.2).
- `neura_server/shadow.py`: Shadow serving (design §28.3): a candidate model on the observations robots send the model they run, its actions compared with the running model's and never sent to a robot.
- `neura_server/testing.py`: Helpers for tests of the gateway core: a core with one deployment, and a robot whose runtime and session client talk to it directly (no network).
- `neura_server/worker.py`: The policy worker process: one model on one device, serving one batch group (design §9.1, §12).

Tests, under `packages/neura-server/tests/`: `test_capacity.py`, `test_capture_export.py`, `test_capture_store.py`, `test_gateway.py`, `test_leases_profiles_admission.py`, `test_sandbox.py`, `test_scheduler.py`, `test_shadow.py`

## packages/neura-sim

Neura simulation: simulated arms, sensors, networks and a deterministic site simulator.

Under `packages/neura-sim/src/`:

- `neura_sim/__init__.py`: Neura simulation.
- `neura_sim/arm.py`: A simulated position-controlled arm, its controller and sensors, with fault injection.
- `neura_sim/chaos.py`: Chaos against real processes: ``neura-chaos``.
- `neura_sim/grpc_robots.py`: Run simulated robots against a live gateway over gRPC: ``neura-sim-robots``.
- `neura_sim/invariants.py`: An independent observer of the safety invariants (design §5-§7, §21).
- `neura_sim/network.py`: A one-way network link model for the discrete-event simulator.
- `neura_sim/site.py`: Deterministic discrete-event simulation of a Neura site (design §7, §16 step 3).
- `neura_sim/soak.py`: A long soak of a simulated site with randomly injected faults: ``neura-sim-soak``.

Tests, under `packages/neura-sim/tests/`: `test_fault_suite.py`, `test_invariants.py`

## console

Neura web console: onboarding, sites, robots, models, releases, live sessions (design §19).

Under `console/`:

- `e2e/episodes_site.py`: The site agent's local API with two recorded episodes, for the episode viewer's browser test.
- `e2e/global-setup.ts`: Start the backend (e2e/stack.py) once for the run and stop it afterwards.
- `e2e/site.ts`: The customer's side of the journey: a GPU server running the site agent, robots running the robot runtime, and the site operator using the agent's CLI.
- `e2e/stack.py`: Run the real backend for the console's end-to-end tests.
- `eslint.config.js`: The console's lint rules: TypeScript, React hooks and accessibility (jsx-a11y).
- `playwright.config.ts`: End-to-end tests drive the real console against the real backend: control plane, site agent, gateway and simulated robots (see e2e/stack.py and e2e/site.ts).
- `src/App.tsx`: The console's routes and providers: every page, loaded on first visit, under the sign-in and project frames.
- `src/api/client.ts`: The console's only way to the backend: the public management API (design §19.1, §20.1).
- `src/api/events.ts`: Live updates (design §20.6): the project's change events, read with fetch so the request can carry the Authorization header (EventSource cannot).
- `src/api/queries.ts`: Server state for every screen, cached and refreshed in the background (design §19.6).
- `src/auth/AuthProvider.tsx`: Who is signed in, and how (design §20.7).
- `src/components/Activity.tsx`: The live activity feed: the project's change events as they arrive, in words, linked to what changed.
- `src/components/CommandPalette.tsx`: The command palette (⌘K / Ctrl+K): jump to any page or resource of the project, or run a console action.
- `src/components/Contracts.tsx`: Registering and showing contract documents (design §4): model artifacts, robot profiles and execution profiles.
- `src/components/Dialog.tsx`: A modal built on the native <dialog>: focus is trapped and Escape closes it; focus returns to whatever opened it.
- `src/components/Field.tsx`: A form field: a label, its one control, a hint and an error, wired to each other in one place, so a control is always named by its label and described by its hint and error.
- `src/components/Icons.tsx`: Inline stroke icons (24x24 grid, 1.75 stroke): no icon font or external request, so the console's content security policy stays default-src 'self'.
- `src/components/Layout.tsx`: The console's frame: the sidebar (organization and project switchers, navigation, docs, theme, account), the top bar (where you are, the live connection, search) and the page.
- `src/components/Tables.tsx`: Tables shown on several pages: sessions, faults and audit records.
- `src/components/Toasts.tsx`: Brief confirmations and failures, announced to assistive technology.
- `src/components/nav.ts`: The console's navigation: one place for the sidebar, the command palette and the top bar.
- `src/components/ui.tsx`: Small, accessible building blocks shared by every page.
- `src/lib/context.ts`: Where the console is (the organization and project in the URL) and what the signed-in principal may do there.
- `src/lib/fleet.ts`: The fleet's vocabulary, shared by the console's pages and the `neura` command (design §29): what a robot's state is called, what a rollout's steps and states are called, and their tones.
- `src/lib/format.ts`: Formatting helpers.
- `src/lib/phases.ts`: The console's state vocabulary (design §19.4): every observed phase has one label and one tone.
- `src/lib/theme.ts`: Theme: dark (the default), light, or the operating system's.
- `src/lib/useNow.ts`: A clock for ages and countdowns.
- `src/main.tsx`: The console's entry point: applies the remembered theme and mounts the app.
- `src/pages/Activate.tsx`: Sign a CLI in (design §29.2): `neura login` shows a code and opens this page; a signed-in person checks the code and approves it for one of their workspaces.
- `src/pages/Fleet.tsx`: Fleet: where the robots are and what each one runs (design §29).
- `src/pages/Home.tsx`: Overview: the project at a glance (design §19.3).
- `src/pages/Improve.tsx`: A deployment's improvement loop (design §28): the episodes its robots recorded (counts only; episodes stay at the site), fine-tuning on them at the site, evaluations of a candidate against what the …
- `src/pages/Models.tsx`: Models: registered model artifacts and robot profiles, and which combinations are compatible (with the reasons when they are not), checked by the same code the site runs (design §4.4).
- `src/pages/Observe.tsx`: Capacity, live sessions, faults and the audit log (design §19.3).
- `src/pages/Policies.tsx`: Policies: what robots can run, their versions, and deploying one (design §29).
- `src/pages/Release.tsx`: One release: robots with desired, prepared and active revisions shown separately (§19.1), revisions with compatibility and site-computed capacity, prepare, activation with approval at the site …
- `src/pages/Releases.tsx`: Deployments: every deployment in the project, filtered and searched, and the dialog that creates one (design §19.3).
- `src/pages/Robots.tsx`: Robots: the project's robots, and one robot's page with its sessions, faults and what it runs (design §19.3).
- `src/pages/Rollouts.tsx`: Rollouts: every change to what robots run, step by step, as `neura deploy` shows it (design §29).
- `src/pages/Settings.tsx`: Organization settings: members and roles, API tokens, support access, sign-in (design §19.2, §19.3, §20.7).
- `src/pages/SignIn.tsx`: Sign in (design §20.7): with the organization's SSO when the control plane publishes one, or with an API token.
- `src/pages/Site.tsx`: A site: its connection, offline allowance, servers with GPUs and preflight, and connecting a new server (design §17.2, §19.3).
- `src/pages/Sites.tsx`: Sites & servers: the project's sites, their servers and connection state, and the dialog that adds a site (design §19.3).
- `src/pages/Start.tsx`: The entry point: choose an organization and project, or create the first project.
- `src/styles/app.css`: The console's stylesheet.
- `src/styles/tokens.css`: Design tokens (NucleiLabs design system, shared with the landing page).
- `src/test/render.tsx`: Unit tests render a screen as the console does, against a fake of the API.
- `src/test/setup.ts`: Unit-test setup: unmount what each test rendered and undo its fakes of the API.
- `vite.config.ts`: The console's build, dev server and unit tests (Vite and Vitest).
- `src/api/schema.d.ts`: generated by openapi-typescript (pnpm run gen), from api/openapi.yaml
- `src/generated/`: generated by tools/gen_reason_codes.py, from catalog/reason_codes.yaml

Tests, under `console/`: `e2e/episodes.spec.ts`, `e2e/journey.spec.ts`, `src/api/client.test.ts`, `src/api/events.test.ts`, `src/components/Contracts.test.ts`, `src/components/Field.test.tsx`, `src/components/ui.test.tsx`, `src/lib/context.test.tsx`, `src/lib/fleet.test.ts`, `src/lib/format.test.ts`, `src/pages/Improve.test.tsx`, `src/pages/Start.test.tsx`

## landing

NucleiLabs marketing site: what Neura is, how it works, and how to become a design partner.

Under `landing/`:

- `eslint.config.js`: The NucleiLabs site's lint rules: TypeScript, React hooks and accessibility (jsx-a11y).
- `src/App.tsx`: The NucleiLabs site.
- `src/FleetDemo.tsx`: The hero's illustration: six robots served from one GPU, drawn the way the console draws them.
- `src/config.ts`: Where the site's links go.
- `src/landing.css`: The NucleiLabs site.
- `src/main.tsx`: The NucleiLabs site's entry point: mounts the page.
- `vite.config.ts`: A static site: built once, served by any web server or CDN.

## site

NucleiLabs developer documentation, starting with Neura (design §26).

Under `site/`:

- `docusaurus.config.ts`: NucleiLabs developer docs (design §26).
- `scripts/csp.mjs`: After a build: the Content-Security-Policy the docs are served with (deploy/landing).
- `sidebars.ts`: The docs' side navigation: every page, in reading order.
- `src/components/StatusBanner.module.css`: The status label at the top of every docs page, one color per label.
- `src/components/StatusBanner.tsx`: The status label every page carries (design §26.2), shown above the page's content.
- `src/components/SupportedTable.tsx`: The Supported combinations table (design §26.5): rendered from data/supported.json, which only qualification runs write, so nobody can add a supported combination by hand.
- `src/css/custom.css`: The docs in the NucleiLabs design: the console's and the site's tokens (tokens.css, loaded first), mapped onto Docusaurus's theme variables, with the site's navigation, type and footer.
- `src/theme/DocItem/Content/index.tsx`: Wraps every docs page's content with its status banner, read from the page's front matter.
- `src/theme/Footer/index.tsx`: The site's footer on every docs page: the same columns as the NucleiLabs home page.

## deploy

Dockerfile, the Compose dev stack, nginx, installers, observability, cloud deploys.

Under `deploy/`:

- `gcp/bench.sh`: The GPU-sharing benchmark (design §9.5, §10, §16 step 0) on one NVIDIA L4 in Google Cloud: a Spot VM that runs `neura-bench run --preset gpu` on this commit and is deleted after, whatever happens.
- `gcp/deploy.sh`: Deploy Neura's cloud side to Google Cloud: one Compute Engine VM running the containers and Postgres (or Cloud SQL), Secret Manager for the keys, Artifact Registry for the images (design §20.10).
- `gcp/neura-apply.sh`: Runs on the Neura VM as root.
- `gcp/site.sh`: A customer's site in Google Cloud, for staging (design §20.12): the site kit (deploy/site-demo) on its own VM, in its own network and region, connected over the internet to the Neura that deploy.sh …
- `install/install.sh`: Install the Neura site agent on a GPU server (design §17.2).
- `modal/bench.py`: The GPU-sharing benchmark (design §9.5, §10, §16 step 0) on one NVIDIA L4 at Modal.
- `site-demo/kit.py`: The customer's side of the site kit (deploy/site-demo), run in the kit's image by site.sh.
- `site-demo/site.sh`: The site kit (README.md here): a customer's site on this machine, connected to your Neura.

## tools

Generators, checks, smoke tests and the agent harness's tools.

Under `tools/`:

- `audit_dependencies.py`: Audit every locked dependency for known vulnerabilities (design §18.2, supply chain).
- `build_agent_release.py`: Build and sign a site-agent release bundle (design §17.3).
- `check_docs.py`: Checks for the developer docs site (design §26.2, §26.7).
- `check_sizes.py`: Keep source files small enough to read whole.
- `codemap.py`: The code map (docs/codemap.md), generated from what each file says it is for, and checks that the maps and guides an agent reads stay true.
- `commit_bench_report.sh`: Commits a GPU benchmark report (README.md and results.json) to the bench-reports branch, under bench/reports/<device>/, from the GPU benchmark workflows (.github/workflows/bench-*.yml).
- `dev_report.py`: What is running on this machine and what went wrong, in one report: read it before guessing.
- `edit_hook.py`: What the agent is told right after it writes a file (Claude Code's PostToolUse hook, configured in .claude/settings.json).
- `gen_dashboards.py`: Generate the Grafana dashboards in deploy/observability/grafana/dashboards from the panel definitions below, so their layout stays uniform and their queries are checked by the tests (every metric …
- `gen_docs.py`: Generate the docs site's reference pages from their sources (design §26.4, §26.5).
- `gen_outbound.py`: Generate the outbound schema module from catalog/outbound.yaml (design §18.2).
- `gen_proto.py`: Generate Python protobuf and gRPC bindings from proto/.
- `gen_reason_codes.py`: Generate the reason-code modules (Python, and TypeScript for the console) from catalog/reason_codes.yaml.
- `gen_schemas.py`: Export JSON Schemas for the contract documents into schemas/.
- `gen_sdk.py`: Generate the Python SDK's models and operations from api/openapi.yaml.
- `refresh_example_bindings.py`: Refresh execution-profile bindings in examples/contracts to the current document digests.
- `shoot.mjs`: Look at the NucleiLabs site and the docs the way a visitor does, and fail when a page is broken.
- `smoke_api.py`: Drive a running dev stack through a whole pilot, the way a customer would, using the SDK.
- `smoke_dev_stack.sh`: Bring up the dev stack, verify each service end to end, then tear it down.
- `smoke_site_kit.py`: The site kit (deploy/site-demo) end to end, the way a customer uses it, against the development stack's control plane (deploy/compose) and with the published ALOHA checkpoint from the Hub.
- `staging_site_test.py`: The staging site (deploy/gcp/site.sh) against the deployed Neura, over the internet: the customer's whole flow, then real network faults on the site's uplink.
- `verify.py`: Prove a change works before saying it does: run the checks the change can break, keep their logs, and say plainly what passed, what failed and what was not checked.

## tests

Tests that span packages: the API spec, integration and the tools.

Tests, under `tests/`: `api/test_openapi.py`, `integration/faulty_worker.py`, `integration/test_act_worker.py`, `integration/test_capture.py`, `integration/test_chaos.py`, `integration/test_gateway_mtls.py`, `integration/test_grpc_site.py`, `integration/test_install_upgrade.py`, `integration/test_observability.py`, `integration/test_shadow_worker.py`, `integration/test_shutdown.py`, `integration/test_worker_sandbox.py`, `integration/test_workers.py`, `tools/test_docs.py`, `tools/test_generators.py`, `tools/test_harness.py`
