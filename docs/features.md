# Feature map

What each feature touches end to end: where its code is, which tests prove it, where it is
documented, and how to see it work. Read the feature's row before changing it, so the change
lands where the feature already lives and its own tests prove it. Every file is in
[codemap.md](codemap.md); why things are built as they are is in
[neura-design.md](neura-design.md) (the § numbers).

Written by hand and checked: `make check-generated` (`tools/codemap.py --check`) fails when a
path, a `file::test` or a `make` target named here no longer exists. When a feature moves or a
new one lands, update its row in the same change.

## The path a policy takes

A policy version is pushed (CLI), checked against the robots it will drive (contracts), rolled out
by the control plane, prepared and approved at the site (site agent), served by the gateway's
policy workers (data plane), and executed only after the robot runtime accepts each action chunk.
The rows below follow that path.

### Contracts and the compatibility check (§4, §29.3)

What a model reads and returns, what a robot provides, and whether they fit; the documents are
inferred, so nobody writes them by hand.

- Code: `packages/neura-contracts/src/neura_contracts/compatibility.py`,
  `packages/neura-contracts/src/neura_contracts/inference.py`,
  `packages/neura-contracts/src/neura_contracts/authoring.py` (`@neura.policy`)
- Tests: `packages/neura-contracts/tests/test_compatibility.py`,
  `packages/neura-contracts/tests/test_inference.py`
- Docs: `site/docs/concepts.md`, `site/docs/policies.md`, `site/docs/compatibility.mdx`
- See it: `uv run neura check hold.py --run` (the policy in `site/docs/deploy-a-policy.md`)

### The `neura` command (§29)

Sign in, sites, robots, policies, deploy, try, rollback, status, logs, approve, doctor.

- Code: `packages/neura/src/neura_cli/` (one module per command group in `commands/`; `ui.py`
  for how it talks)
- Tests: `packages/neura-control/tests/test_neura_cli.py::test_the_quickstart_from_a_fresh_workspace_to_a_moving_arm`
  (the docs' page, run against a live control plane and a real agent),
  `packages/neura/tests/test_cli_offline.py`
- Docs: `site/docs/deploy-a-policy.md`, `site/docs/troubleshooting.md`

### Signing the CLI in through the browser (§29.2)

`neura login` shows a code; a person types it in the console, sees what waits for it, approves.

- Code: `packages/neura-control/src/neura_control/api/routes_login.py`,
  `packages/neura/src/neura_cli/commands/account.py`, `console/src/pages/Activate.tsx`
- Tests: `packages/neura-control/tests/test_fleet.py::test_the_cli_signs_in_through_the_browser`,
  `packages/neura-control/tests/test_fleet.py::test_a_person_sees_what_a_code_signs_in_before_approving_it`,
  `console/src/pages/Activate.test.tsx`, and the journey's sign-in test (`console/e2e/journey.spec.ts`:
  a person signed in with the e2e stack's test SSO issuer approves, the CLI gets its token)

### Rollouts: fits, at the site, shadow, approval, switch (§29.4, §28.3)

One change to what robots run, and every step it goes through; rollbacks.

- Code: `packages/neura-control/src/neura_control/services/rollouts.py` (large: search it),
  `packages/neura-control/src/neura_control/services/release_gates.py`,
  `packages/neura-control/src/neura_control/api/routes_fleet.py`
- Tests: `packages/neura-control/tests/test_fleet.py::test_deploy_on_a_bench_from_check_to_live`,
  `packages/neura-control/tests/test_fleet.py::test_a_second_version_is_shadowed_then_rolled_back`,
  `packages/neura-control/tests/test_fleet.py::test_an_inferior_shadow_stops_the_rollout`,
  `packages/neura-control/tests/test_fleet.py::test_concurrent_deploys_start_one_rollout_per_robot`
- Docs: `site/docs/deploy-a-policy.md`; the console's Rollouts page (`console/src/pages/Rollouts.tsx`)

### The control plane and its API (§13, §20)

The public management API (spec first), auth, audit, change events, and the site protocol.

- Code: `api/openapi.yaml` (the spec; `make gen` regenerates the SDK, the console's types and the
  reference docs), `packages/neura-control/src/neura_control/api/`,
  `packages/neura-control/src/neura_control/grpc_site.py` (the site side; large)
- Tests: `tests/api/test_openapi.py`, `packages/neura-control/tests/test_api_conventions.py`,
  `packages/neura-control/tests/test_security.py`, `packages/neura-control/tests/test_sdk.py`,
  `packages/neura-control/tests/test_site_servers.py` (sites with several servers),
  `packages/neura-control/tests/test_events.py` (the change-event stream)
- Docs: `site/docs/reference/api.md` (generated), `site/docs/architecture.md`
- Needs Postgres: `NEURA_TEST_DATABASE_URL` (`make verify` sets it when Postgres answers)

### The site agent (§13, §17)

Enrollment, the outbound connection, reconciling committed state, verifying artifacts, approvals,
signed upgrades, and the local site API.

- Code: `packages/neura-agent/src/neura_agent/core.py` (its decisions; large),
  `packages/neura-agent/src/neura_agent/runner.py`, `packages/neura-agent/src/neura_agent/app.py`
- Tests: `packages/neura-agent/tests/test_core.py` (large),
  `packages/neura-control/tests/test_site_agent_e2e.py::test_enroll_deploy_approve_switch_and_survive_outage`
- Docs: `site/docs/privacy-and-access.md`, `site/docs/architecture.md`

### Installing a server: the one-line installer and agent releases (§17.2, §17.3, §29.2)

- Code: `packages/neura-control/src/neura_control/installer.py`, `deploy/install/install.sh`,
  `tools/build_agent_release.py`, `packages/neura-agent/src/neura_agent/releases.py`
- Tests: `packages/neura-control/tests/test_fleet.py::test_production_servers_install_with_one_line`,
  `tests/integration/test_install_upgrade.py`; installing as root, in a container (needs Docker):
  `tests/integration/test_install_upgrade.py::test_installing_as_root_runs_nothing_as_root_from_the_agents_own_directories`;
  `/install.sh` (and every other route the API serves) through the console's nginx:
  `tests/integration/test_console_proxy.py` (needs Docker)
- Docs: `site/docs/deploy-a-policy.md` ("A production server")

### Serving: gateway, leases, admission, scheduler, workers (§6, §9, §12)

- Code: `packages/neura-server/src/neura_server/gateway.py`,
  `packages/neura-server/src/neura_server/scheduler.py`,
  `packages/neura-server/src/neura_server/admission.py`,
  `packages/neura-server/src/neura_server/worker.py`,
  `packages/neura-server/src/neura_server/backends.py` (supervising workers),
  `packages/neura-server/src/neura_server/sandbox.py`,
  `packages/neura-server/src/neura_server/robot_service.py` (the gRPC service robots call)
- Tests: `packages/neura-server/tests/test_gateway.py`, `packages/neura-server/tests/test_scheduler.py`,
  `packages/neura-server/tests/test_sandbox.py` (each run may write only a directory of its own),
  `tests/integration/test_workers.py`, `tests/integration/test_worker_sandbox.py` (needs Docker
  and a worker image: a worker sees only its own socket),
  `tests/integration/test_shutdown.py` (stopped with SIGTERM, it stops its workers and leaves
  nothing behind)
- Model adapters: `packages/neura-adapters/src/neura_adapters/` (LeRobot, Python classes, LoRA
  batching); their real-model tests need `make models-setup`, then `make test-models`

### The robot runtime: the only execution authority (§5 to §8)

Action queue and chunk merging, fencing, the state machine, sessions.

- Code: `packages/neura-runtime/src/neura_runtime/runtime.py`,
  `packages/neura-runtime/src/neura_runtime/action_queue.py`,
  `packages/neura-runtime/src/neura_runtime/session.py`
- Tests: `packages/neura-runtime/tests/test_runtime.py`, `packages/neura-runtime/tests/test_action_queue.py`,
  `packages/neura-runtime/tests/test_session.py` (which rejections end a session),
  `packages/neura-runtime/tests/test_tensors.py`,
  `packages/neura-sim/tests/test_fault_suite.py` (every fault in §7, with an independent
  invariant monitor: `packages/neura-sim/src/neura_sim/invariants.py`)
- Docs: `site/docs/fault-suite.md`, `site/docs/architecture.md`

## Robots

### The SO-101 arm and hardware qualification (§7.1)

- Code: `packages/neura-robots/src/neura_robots/so101.py`,
  `packages/neura-robots/src/neura_robots/emulator.py` (the servo bus, emulated),
  `packages/neura-robots/src/neura_robots/qualify/` (`harness.py` is large)
- Tests: `packages/neura-robots/tests/test_so101.py`, `packages/neura-robots/tests/test_emulator.py`,
  `packages/neura-robots/tests/test_qualify.py`; on the emulated arm: `make qualify-emulated`
- Docs: `site/docs/hardware-qualification.md`

### The simulated ALOHA, the site kit and closed-loop evidence (§10.1, §20.11)

- Code: `packages/neura-robots/src/neura_robots/aloha/`, `packages/neura-robots/src/neura_robots/evidence/`,
  `deploy/site-demo/`
- Tests: `packages/neura-robots/tests/test_aloha.py`, `packages/neura-robots/tests/test_evidence.py`,
  `packages/neura-robots/tests/test_cell.py`

### Simulation, chaos and soak (§7, §21)

- Code: `packages/neura-sim/src/neura_sim/site.py`, `packages/neura-sim/src/neura_sim/chaos.py`,
  `packages/neura-sim/src/neura_sim/soak.py`
- Tests: `packages/neura-sim/tests/test_invariants.py`, `tests/integration/test_chaos.py`
- Run it: `make soak`, `make chaos`

## The loop: capture, evaluate, improve (§28)

- Code: `packages/neura-runtime/src/neura_runtime/capture/` (on the robot),
  `packages/neura-server/src/neura_server/capture/` (at the site), `packages/neura-learn/src/neura_learn/`,
  `packages/neura-server/src/neura_server/shadow.py`, `packages/neura-agent/src/neura_agent/jobs.py`
- Tests: `tests/integration/test_capture.py`, `packages/neura-learn/tests/test_learn.py`,
  `packages/neura-control/tests/test_improvement.py`, `packages/neura-server/tests/test_shadow.py`,
  `packages/neura-agent/tests/test_jobs.py` (a job writes only its own output, what it prints
  stays at the site, and it never outlives the agent),
  `packages/neura-learn/tests/test_site_jobs.py` (evaluation and fine-tuning at the site, end to
  end; needs the models group)
- Docs: `site/docs/improvement-loop.md`; the console: `console/src/pages/Improve.tsx`

## Benchmarks: sharing a GPU (§9.5, §10)

- Code: `packages/neura-bench/src/neura_bench/` (`suite.py` is large)
- Tests: `packages/neura-bench/tests/test_sim.py`, `packages/neura-bench/tests/test_report.py`
- Run it: `make bench-cpu` here; `make bench` on a GPU; reports in `bench/reports/`

## Security and privacy (§17.8, §18)

mTLS between robots and the gateway, signed artifacts and manifests, sealed weights, the outbound
ledger and its allowlist.

- Code: `packages/neura-contracts/src/neura_contracts/pki.py`,
  `packages/neura-contracts/src/neura_contracts/signing.py`,
  `packages/neura-contracts/src/neura_contracts/sealing.py`, `catalog/outbound.yaml` (the
  allowlist; `make gen`), `packages/neura-agent/src/neura_agent/ledger.py`
- Tests: `packages/neura-contracts/tests/test_signing.py`, `packages/neura-contracts/tests/test_sealing.py`,
  `packages/neura-agent/tests/test_outbound.py`, `tests/integration/test_gateway_mtls.py`,
  `packages/neura-control/tests/test_sealed_weights.py` (Neura's registry of sealed weights)
- Docs: `site/docs/privacy-and-access.md`, `SECURITY.md`

## Interfaces people use

### The console (§19)

Fleet, Policies, Rollouts and Settings, on the public API only.

- Code: `console/src/pages/`, `console/src/components/ui.tsx` (shared parts),
  `console/src/components/Field.tsx` (a form field wired to its label, hint and error),
  `console/src/styles/app.css` (large), `console/src/styles/tokens.css` (shared with the site
  and docs)
- Tests: `console/src/lib/fleet.test.ts` and the other unit tests (`make console-check`);
  `console/e2e/journey.spec.ts`, the whole journey against the real backend with accessibility
  checks on every screen (`make verify VERIFY=--e2e` keeps a screenshot of each in
  `.verify/console`)

### The NucleiLabs site and the docs (§26)

- Code: `landing/src/`, `site/docs/`, `site/src/`, `site/docusaurus.config.ts`;
  served together by `deploy/landing/default.conf` (the docs at /docs/)
- Tests: `tools/check_docs.py` (labels, wording; `make docs-check`), `tests/tools/test_docs.py`
- See it: `make shots` opens every page in Chromium with production's headers and fails on any
  error; screenshots in `.verify/shots`

## The development harness

- Code: `tools/verify.py` (`make verify`), `tools/shoot.mjs` (`make shots`),
  `tools/dev_report.py` (`make dev-report`), `tools/codemap.py` (this map's check and
  `docs/codemap.md`), `tools/check_sizes.py` (`make lint`), `tools/edit_hook.py` (the edit hook in
  `.claude/settings.json`)
- Tests: `tests/tools/test_harness.py`
- Guides: `CLAUDE.md`, `.claude/skills/verify/SKILL.md`, `.claude/skills/debug/SKILL.md`,
  `.claude/skills/launch/SKILL.md`
