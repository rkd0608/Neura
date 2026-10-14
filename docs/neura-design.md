# Neura: engineering design v0.1

Status: proposed design, no code written · October 14, 2026
Scope: Neura only (deployment and fleet serving of customer-owned robot policies). This is the single design document for Neura: architecture, connectivity, privacy, console, API, service levels, value and defensibility.
Parent document: NucleiLabs platform and product technical blueprint v0.1.

This document turns the blueprint into decisions an implementation can follow. Each decision lists what it is, why, and what evidence would make us revisit it. Numbers marked *illustrative* are starting points for simulation and tests, never operating values for a customer robot.

---

## 1. Design goals, in priority order

1. **Never unsafe by construction.** Neura cannot cause a robot to execute a stale, duplicated, misinterpreted or unauthorized action, whatever happens to the network, workers or cloud. Correctness here beats throughput everywhere.
2. **Contract-first support.** A policy or robot is "supported" when it passes a published conformance suite, not when someone got it working once. Adding the next model or robot should be adapter work plus tests, never a fork of the runtime.
3. **Scale by adding independent cells.** Capacity grows by adding workers, servers and sites that do not depend on each other at runtime. No component on the action path is shared across sites.
4. **Measured, not assumed, benefit.** Every serving optimization ships with a benchmark against a defined dedicated baseline (§10), including the cases where it loses.
5. **Boring infrastructure where possible.** Use protobuf, gRPC, OCI images, OpenTelemetry, Prometheus, PostgreSQL. Invent only robotics execution semantics.

Non-goals for Neura: training, evaluation-as-a-product, simulators, teleoperation, safety certification, cross-customer GPU sharing.

---

## 2. Closing the policy gap: a reference qualification set

You do not have a customer policy yet, and you should not wait for one. Customers will bring policies that differ along a small number of axes. Neura should be qualified against public reference policies chosen to cover those axes, so the first customer integration is an instance of something already tested.

### 2.1 The axes that change serving behavior

| Axis | Why it matters to Neura |
| --- | --- |
| Model size / latency | Decides whether batching helps, memory headroom, how many sessions per GPU |
| Deterministic vs stochastic output | Decides how parity testing works (exact match vs distributional) |
| Chunk length and control rate | Decides buffer budgets and refill timing |
| Language conditioning | Instruction changes must bump the task epoch |
| Inference-delay awareness (RTC-capable) | Decides which chunk merge modes are valid |
| Stateful vs stateless per request | Stateful policies need isolated session state; stateless ones batch freely |
| Action semantics | Absolute joint positions, deltas, end-effector poses: adapters must never convert silently |

### 2.2 Reference policies

| Order | Policy | Covers | Simulation for qualification |
| --- | --- | --- | --- |
| R1 | **ACT** (LeRobot) | Small, fast, deterministic at inference, long chunks, absolute joint targets. The parity baseline. | ALOHA sim (insertion, cube transfer), ACT's original environments |
| R2 | **SmolVLA** (LeRobot, ~0.45B) | Language-conditioned VLA, flow matching (stochastic), GPU-heavy enough for batching to matter. **The first shared-serving workload.** | LIBERO, Meta-World (the benchmarks it reports on) |
| R3 | **π0 / π0.5** (openpi or LeRobot port) | Large flow VLA, memory pressure, real-time chunking (RTC) compatible | LIBERO |
| Later | Diffusion Policy | Iterative denoising cost, stochastic | PushT |

Why this order: R1 gives single-robot parity quickly with low noise. R2 is where consolidating several robots onto one GPU is plausibly valuable, so it is where the business claim gets tested. R3 stresses memory and the inference-delay-aware merge path.

Hardware path: simulation first; then a low-cost physical arm (SO-101 class, supported by LeRobot) for real-timing and fault tests; then a design partner's arm. Physical tests come before any customer claim.

Before using any reference model, record its code and **weights** license in the artifact manifest. Code licenses (LeRobot, openpi) and weights licenses can differ; verify each at the time of use.

### 2.3 What "supported" means

A (policy, robot, execution profile) triple is supported when:

1. Contract tests pass (§4).
2. Open-loop parity: on recorded observations, Neura's outputs match the reference runtime (bit-exact or within declared tolerance for deterministic models; matching distribution statistics for stochastic models with fixed seeds where the model allows).
3. Closed-loop simulation: task success under Neura is non-inferior to the reference in-process runtime, at the declared timing envelope, over a pre-registered number of episodes.
4. Fault-suite tests pass (§7).
5. For physical support: the same, on hardware, under an agreed test protocol.

---

## 3. System decomposition

The blueprint's three planes stay. Inside a site, Neura runs as **separate processes with protobuf interfaces even on a single machine**. That is the main design choice for scale: going from one box to many is a deployment change, not a rewrite.

```
            Cloud control plane (not on action path)
                 │  desired state, artifacts, audit, aggregate metrics
                 ▼
┌──────────────────────── Site (one failure domain) ────────────────────────┐
│ Site agent ── reconciles manifests, pulls/verifies artifacts, supervises  │
│                                                                           │
│ Gateway ──── authN, admission, leases, message validation, routing        │
│    │                                                                      │
│ Scheduler/router ── per worker-group queues, deadline + fairness          │
│    │                                                                      │
│ Policy workers ── one process per (artifact, device); batch inference     │
│                                                                           │
│ Local store ── leases, committed config, buffered telemetry (SQLite)      │
└───────────────────────────────────────────────────────────────────────────┘
                 ▲ LAN, authenticated stream per session
┌──────────────── Robot computer ────────────────┐
│ Neura robot runtime: cursor, queue, merge,     │
│ freshness, state machine, fault handling       │
│ Integration adapter → customer controller      │
└────────────────────────────────────────────────┘
```

Responsibilities that never move:

- **The robot runtime is the only execution authority.** It owns the action cursor, the task epoch, what was dispatched, and fault responses.
- **The gateway owns who may be served** (leases, admission).
- **Workers are stateless with respect to authority.** They compute chunks for requests. They never decide what runs.

---

## 4. Contracts

Three versioned, immutable documents, each identified by content hash.

### 4.1 Model artifact manifest

- Weights digest(s); runtime OCI image digest; adapter name and version.
- Pre/post-processing versions, including normalization statistics digest.
- Inputs: named features with dtype, shape, units, and for images: camera role, resolution, color order, **encoding accepted** (raw, JPEG quality ≥ N). Image compression changes model inputs, so it is part of the contract, not a transport detail.
- Outputs: action feature names in order, units, frame, **semantics** (`absolute_joint_position`, `delta_joint_position`, `ee_pose_absolute`, …), chunk length H, native control period.
- Properties: `deterministic`, `stateful`, `language_conditioned`, `rtc_capable`, `batchable`, supported devices and precisions.
- Licenses (code, weights), provenance, test evidence references.

### 4.2 Robot profile

Joint names and order, units, frames, valid ranges and rate limits, camera identities and calibration revision, command interface, controller revision, control period, and the **local fault responses the integration actually supports** (e.g. `hold_position` validated: yes/no; `controlled_stop`; `customer_estop_path`).

### 4.3 Execution profile

Binds one artifact to one robot profile:

- Control period, actions executed per chunk, refill threshold.
- Chunk merge mode (§5) and its parameters.
- Freshness budget (max observation age at dispatch and at acceptance).
- Starvation response and grace period, chosen only from the robot profile's validated responses.
- Explicit, versioned adapter transforms (e.g. joint reorder), each with tests.

### 4.4 Compatibility check

Exact matching by name, order, unit, frame and semantics. **No implicit conversions.** A mismatch is a hard rejection with a reason code. Any transform must be declared in the execution profile and covered by its tests. Out-of-range actions are rejected (and count as a fault) unless the robot profile declares validated limiting behavior.

---

## 5. Closing the chunk-merge gap

This is the most important semantic in Neura, because it decides what the robot physically does at chunk boundaries.

### 5.1 Indexing model

- Per task epoch, actions have a global integer index `k` starting at 0. The robot runtime's **cursor** `c` is the index of the next action to dispatch. Actions with index `< c` have been dispatched and are immutable history.
- Each observation records `obs_cursor` = the cursor at capture time.
- A chunk produced from that observation covers indices `[obs_cursor, obs_cursor + H)`.
- When a chunk arrives with the runtime at cursor `c`, the indices in `[obs_cursor, c)` are already in the past and are **dropped (trimmed)**, never executed late. The measured delay `d = c − obs_cursor` is recorded.

### 5.2 Acceptance rules (checked by the robot runtime, in order)

1. Session id, lease generation and task epoch match current values. Else drop silently and count it.
2. Request sequence is the latest outstanding one; duplicates and superseded replies are dropped (idempotent by `(session, epoch, seq)`).
3. Artifact and execution-profile digests match the active revision.
4. Observation age, measured on the robot's own monotonic clock from capture to now, is within budget.
5. Payload integrity, shape, finiteness and range checks pass.
6. After trimming, at least `min_useful_actions` remain. Else drop and count it as a late reply.

### 5.3 Merge modes (declared per execution profile, validated per policy)

| Mode | Behavior on the future part (indices ≥ c) | Valid for |
| --- | --- | --- |
| `replace` (default) | New chunk replaces all queued future actions | Any policy; the reference behavior |
| `append` | New chunk queued after the current one ends | Policies trained for open-loop execution of whole chunks |
| `blend(window, weights)` | Weighted mix over the overlap window, then the new chunk | Policies validated with blending (ACT-style temporal ensembling) |
| `rtc` | Request carries the frozen prefix; model inpaints the continuation for the declared delay; runtime replaces from `c` | Only `rtc_capable` artifacts whose conditioned delay ≥ measured delay |

Hard invariants for every mode:

- An action at index `< c` is never replaced or re-dispatched.
- A merge is atomic with respect to dispatch: the control loop sees either the old queue or the new one.
- Changing mode is an execution-profile change, which needs revalidation.

---

## 6. Closing the authority and fencing gap

Two independent fences, owned by different components.

### 6.1 Lease (who may serve this robot): owned by the gateway

- A robot opens a session with its device credential (mTLS certificate). The gateway grants a **lease** with a monotonically increasing `lease_generation`, stored durably in the site-local store, plus a TTL renewed by heartbeat.
- A second open for the same robot identity while a lease is live returns `BUSY` naming the holder. A takeover requires the lease to expire or be explicitly revoked by an operator; the takeover increments the generation.
- Workers and the scheduler stamp the generation on every response; the robot runtime rejects any generation other than its own.

### 6.2 Task epoch (which task is running): owned by the robot runtime

- The robot runtime increments the epoch on: reset, new or changed instruction, activation of a new revision, operator takeover, recovery from fault, and resume after starvation (if the profile requires a fresh start).
- On increment, the runtime clears its future queue **before** sending anything else, then informs the gateway, which cancels queued requests for the old epoch.
- The server cannot advance the epoch. It can only request a reset.

### 6.3 One actuator authority per robot

The robot runtime takes an exclusive local lock on the controller interface (OS lock or the controller's own exclusive connection). Two runtime processes on the same robot cannot both actuate. Combined with the lease, this covers duplicated clients both locally and remotely.

---

## 7. Robot runtime state machine and faults

States follow the blueprint: `Disconnected → Ready → Armed → Running ⇄ Waiting → Faulted`. Precise additions:

- **Armed → Running** requires local activation permission **and** a first accepted chunk.
- **Running → Waiting**: the queue reaches zero. The runtime invokes the profile's starvation response (e.g. validated hold) and starts a grace timer (*illustrative* 1.0 s, the default in the LeRobot remote-inference design).
- **Waiting → Running**: a fresh chunk computed from an observation taken **after** entering Waiting arrives within grace. Profiles may require a new epoch here.
- **→ Faulted** on: grace exhausted, fence violation from a component that should know better, invalid action, controller command failure, or lost actuator lock. The fault is **latched**: late replies, reconnection or a cloud command do not clear it. Only an explicit local reset, revalidation, and a new epoch return to Ready.
- If a command to the controller fails, the runtime stops generating commands and invokes the customer's independent stop path. It never reports "stopped" without controller confirmation.

### Fault suite (every supported integration must pass, in simulation and with fault injection)

Stale reply, duplicate reply, reordered replies, reply for old epoch, reply for old lease generation, worker crash mid-batch, worker restart (old chunk must never replay), gateway restart, network delay above budget, network partition, packet loss on the stream, camera missing or skewed beyond tolerance, NaN or out-of-range action, oversized message, GPU out-of-memory, cloud control plane down for a long period, offline authorization expiry, operator takeover, second client for the same robot, recording disk full.

---

## 8. Timing without shared clocks

Machines never subtract each other's timestamps.

- The robot sends a **relative budget**: `budget = (queued_actions × control_period) − margin`, i.e. how long until it starves.
- The gateway converts it on receipt into a local deadline: `deadline = recv_time + budget − uplink_estimate − downlink_estimate`, where the link estimates are conservative upper percentiles from heartbeat RTT measurements on that session (using full p99 RTT for each direction is the safe default).
- The scheduler only uses gateway-local deadlines.
- The robot independently re-checks freshness on its own clock (§5.2). The server's deadline is an optimization; the robot's check is the authority.

Refill rule: the robot requests a new chunk when `queued_actions × control_period ≤ p99(round trip) + p99(scheduler wait at admitted load) + margin`. If the admitted load cannot keep this inequality, the session must be **rejected at admission** rather than accepted and starved later.

*Illustrative* simulation envelope to start with: 30 Hz and 50 Hz control; chunks of 50 (SmolVLA) and 100 (ACT); LAN RTT p99 of 2–10 ms; inference p50 of 10–150 ms depending on model and batch size. Real values come from profiling.

---

## 9. Serving, scheduling and scale

### 9.1 Batch groups

A batch group is `(artifact digest, precision, device, input shape bucket)`. Only requests in the same group share a forward pass. Different fine-tuned weights are different groups. One worker process serves one group on one device.

### 9.2 Scheduler

Per group, at each dispatch opportunity:

1. Drop requests that are expired, superseded, or fenced.
2. Order by deadline; apply a fairness rule (no session waits more than N consecutive dispatches it was eligible for).
3. Choose the largest batch whose **profiled p99 latency** at that batch size fits the earliest deadline in it. Never wait to fill a batch beyond the earliest eligible deadline.
4. Dispatch; record predicted vs actual latency; update the profile online.

FIFO and round robin ship as baseline schedulers selectable by config, so every benchmark compares against them. At most one outstanding request per robot.

### 9.3 Admission control

A session is admitted to a group only if the profiled latency at the resulting concurrency still satisfies every admitted session's refill inequality (§8), with configured reserve capacity and memory headroom. Admission is where overload is refused. There are no growing queues.

### 9.4 Scale axes

| Level | How it scales | What does not happen |
| --- | --- | --- |
| Worker | Larger batches inside one forward pass | No batching across different weights |
| Device | Several workers per GPU (one per group) with memory admission; later MPS / MIG where measured to help | No silent precision change under memory pressure |
| Server | Several GPUs; the router places sessions on workers at admission | No migration of a running session mid-task |
| Site | Several servers behind one gateway (gateway replicable; leases in the site store) | Site does not depend on another site or the cloud at runtime |
| Fleet | Add sites. Each site is an independent cell; the control plane reconciles many cells | No cross-site data plane |

Session placement is sticky for the lifetime of a task epoch. Rebalancing and failover to another worker happen only at epoch boundaries with a fresh observation. Warm spare capacity is a configured reserve; the system never relies on a cold worker becoming ready within an action deadline.

Larger installations can adopt Ray Serve / KubeRay underneath the worker layer if it wins on measurement; Neura's gateway, scheduler semantics and robot runtime stay the same above it.

---

## 10. Closing the benchmark-baseline gap

**Dedicated baseline** = one worker per robot, same GPU model, same precision, same compiler optimizations (e.g. `torch.compile`, CUDA graphs where the model supports them), same observation encoding, batch size 1, same harness and network.

Compared configurations: dedicated (one GPU per robot), dedicated (several processes sharing one GPU, no batching), Neura shared with FIFO, round robin, and deadline+fairness.

Workloads: homogeneous (N identical sessions), heterogeneous (mixed models and rates), bursty arrivals, plus a concurrent non-policy GPU job.

Reported metrics: deadline misses per 1,000 requests, starvation events per robot-hour, late-trimmed actions, observation age at dispatch (p50/p99/max), GPU-hours per robot-hour, peak memory, closed-loop task success (simulation) with confidence intervals. Each report states the crossover point where sharing stops paying.

---

## 11. Transport and protocol

- **Schema**: protobuf, versioned (`neura.v1`). Envelope carries protocol version, session id, lease generation, task epoch, sequence, artifact and profile digests. Exact version match required; mismatch is a typed rejection.
- **First transport**: gRPC bidirectional stream per session over mTLS on the LAN. Observations are bounded in size; image payloads raw or compressed as the contract declares; never pickle.
- **Measure**: head-of-line blocking under loss. If it violates budgets, add a Zenoh or QUIC adapter behind the same schema (Zenoh is what the current LeRobot remote-inference design uses, which also makes it a candidate for a compatibility front end).
- **Separate channels by priority**: control (reset, invalidate, close) never waits behind observation payloads.

---

## 12. Model adapters and worker isolation

An adapter implements: `load(artifact)`, `warmup()`, `infer_batch(requests) → chunks`, and declares properties from §4.1. Initial support covers **stateless-per-request** policies only (ACT, SmolVLA and π0 qualify; ACT temporal ensembling belongs to the robot-side `blend` mode). Stateful policies wait for an explicit session-state design.

Workers run the artifact's pinned OCI image, without network access beyond the scheduler socket, with resource limits. Customer model code is treated as untrusted executable code inside that sandbox.

---

## 13. Control plane and releases (built after the data plane is proven)

Desired, prepared and active revisions are stored separately. Release flow: create immutable revision → site agent pulls and verifies digests → compatibility check → warm and profile worker → operator-approved activation at a task boundary (robot increments epoch) → keep the previous revision warm for rollback while it stays compatible. The cloud never directly authorizes motion. The site keeps working on its committed configuration until its offline authorization expires (expiry period configured per customer).

---

## 14. Licensing recommendation (not legal advice)

Adoption as infrastructure depends on others implementing your contract, so split the code now so the decision stays open:

| Component | Recommended stance | Why |
| --- | --- | --- |
| Protocol schema, contract specs, conformance tests | Open, Apache-2.0 | Others can implement and certify against them; Apache-2.0 includes a patent grant |
| Robot runtime and Python SDK | Open, Apache-2.0 | Runs on customer robots; customers and auditors will want to read the code that touches actuation |
| Reference model adapters | Open, Apache-2.0 | Lowers integration cost; contributes back to LeRobot-style ecosystems |
| Scheduler, admission, site agent, control plane, console | Proprietary | Where operational value and support are sold |

Repository rule from day one: open packages never import proprietary packages. Get a lawyer to review before the first public release, and track third-party model and weights licenses in every artifact manifest.

---

## 15. Repository layout (planned, not created)

```
proto/neura/v1/            # protobuf schema (open)
neura_contracts/           # manifests, profiles, compatibility checks (open)
neura_runtime/             # robot runtime, merge, fencing, state machine (open)
neura_sdk/                 # Python client and tooling (open)
neura_adapters/            # ACT, SmolVLA, π0 adapters (open)
neura_server/              # gateway, scheduler, workers, admission (proprietary)
neura_agent/               # site agent (proprietary)
neura_control/             # control plane API (proprietary, later)
console/                   # web console, TypeScript (proprietary, later)
api/                       # OpenAPI spec for the public management API
sim/                       # simulated robots, network and GPU models; harness
conformance/               # contract, fault and parity suites
bench/                     # benchmark harness and reports
docs/
```

---

## 16. Build sequence

| Step | Delivers | Exit criterion |
| --- | --- | --- |
| 1 | Schema, contracts, compatibility checks | Contract test suite passes, including rejection cases |
| 2 | Robot runtime: cursor, merge modes, fences, state machine | Property-based tests prove the invariants in §5–§7 under random interleavings |
| 3 | Simulated harness: virtual robots, simulated network with delay/loss/partition, mock worker with profiled latency | Full fault suite runs in CI without a GPU |
| 4 | Gateway, scheduler, admission, worker process with ACT adapter | Single-robot parity with in-process ACT in ALOHA sim |
| 5 | Shared serving with SmolVLA, N sessions per GPU | Benchmark report vs dedicated baseline, including losing cases |
| 6 | π0-class adapter with `rtc` mode | Closed-loop non-inferiority under injected delay |
| 7 | Physical arm (SO-101 class) | Fault suite and parity on hardware |
| 8 | Site agent, artifact verification, release and rollback flow | Install on a clean second machine from documentation only |
| 9 | Control plane | Site keeps operating through control-plane outage |

Steps 1–3 need no GPU or robot and set the safety guarantees everything else relies on.

---

## 17. Connecting customer GPU servers

### 17.1 Requirements on the customer server

Linux x86-64 or ARM64, a supported NVIDIA driver, the NVIDIA container toolkit, a container runtime, local disk for artifacts and buffered telemetry, and a LAN path to the robots. No Kubernetes, no inbound internet port, no VPN.

### 17.2 Enrollment flow

1. In the console, the customer creates a site and generates a **one-time enrollment token** (short-lived, bound to one organization, project and site).
2. On the server they run one installer command (package or container). It installs the **site agent** only.
3. The agent runs **preflight checks** and shows the results locally before anything else: driver and CUDA versions, GPU models and memory, MIG/MPS state, other GPU processes, disk, robot-network reachability, clock sanity. Failures name the fix.
4. The agent generates its key pair **on the server** (in the TPM when one is available), sends a certificate signing request with the token, and receives a site certificate. The private key never leaves the machine. The token cannot be reused.
5. The agent reports the server's observed capabilities. The customer chooses which GPUs Neura may use; others stay untouched, so Neura can coexist with the customer's own jobs.

### 17.3 Ongoing connection

- **Outbound only.** The agent dials the control plane over TLS 443 (proxy-aware) and holds a long-lived stream for desired state and acknowledgements. The cloud never connects in, so customers do not open firewall ports.
- **Pull, verify, then run.** The agent pulls runtime images and weights from the customer's own registry or object store (preferred) or Neura's, verifies digests and signatures, and only then starts workers. A manifest with a missing or wrong signature is rejected.
- **The site acts on its own.** If the connection drops, the site keeps operating on its committed configuration until its offline authorization expires (§13). Nothing on the action path waits on the cloud.
- **Robots enroll locally.** The agent runs a site-local certificate authority; robot devices get per-device certificates for mTLS to the gateway. Robot traffic never leaves the LAN.
- **Agent upgrades** are signed, staged, health-checked and automatically rolled back if the new agent does not report healthy. Upgrades never happen during an active task epoch.

### 17.4 Customer-controlled boundaries

Configured on the server itself, not in the cloud, so a compromised or mistaken control plane cannot widen them:

- Which GPUs, disk paths and networks Neura may use.
- Whether remote administration is allowed at all, and which operations (deploy, rollback, collect diagnostics).
- Physical activation always needs local approval (§6, §13).
- A local audit log of every instruction received from the cloud and what the agent did with it.
- Stopping the agent stops Neura's management access completely.

### 17.5 Other connection modes (later, on demand)

- **Air-gapped:** signed offline bundles (manifest plus images) imported from an internal registry or removable media; telemetry exported to files.
- **Kubernetes:** an operator and Helm chart for customers who already run clusters, with the same agent semantics.
- **Customer cloud account:** the same agent in the customer's VPC on their GPU instances.

---

## 18. Data privacy

### 18.1 Data classes and where they live by default

| Class | Examples | Default location | Leaves the site? |
| --- | --- | --- | --- |
| Payload | Camera images, robot state, actions, language instructions | Site memory only; optional recordings in the customer's store | **Never**, unless the customer opts in per deployment |
| Model | Weights, normalization statistics, adapter code | Customer registry or site cache | Only if the customer chooses Neura's registry, then encrypted with a customer-managed key |
| Operational metadata | Digests, ids, state transitions, reason codes, timings | Site store | Yes, under a published schema (§18.2) |
| Aggregate metrics | Latency percentiles, deadline misses, utilization | Site; exportable | Yes, or only to the customer's own Prometheus/OTel |

### 18.2 Technical controls

- **Telemetry schema allowlist.** The agent only sends fields defined in a published, versioned schema. No free text, no payload fields, no file contents. A local **outbound ledger** shows exactly what was sent, so customers can verify instead of trusting.
- **Customer-routed telemetry.** Metrics and traces can go only to the customer's own systems, with Neura receiving nothing beyond health and version status.
- **Customer-managed keys.** If weights are stored with Neura, they are encrypted with the customer's KMS key and decrypted only on the site agent. Neura staff cannot read them. Revoking the key revokes access.
- **mTLS everywhere**; encrypted local storage recommended and checked in preflight.
- **No standing remote access.** Support uses break-glass sessions that the customer approves, are time-boxed and are fully audited. No permanent remote shell.
- **Tenancy.** Each customer's site is dedicated. The control plane isolates organizations at the data layer; a dedicated or self-hosted control plane is offered for customers who require it.
- **Supply chain.** Signed images, SBOMs and an open robot runtime (§14), so customers can inspect what runs on their robots and servers.
- **No training on customer data.** Stated in the contract, and also structurally true, because payloads do not reach Neura by default.

### 18.3 Compliance path (not yet claimed)

Data processing agreement from the first pilot; SOC 2 Type II when the control plane holds production customers; ISO 27001 if customers require it.

---

## 19. Console (frontend)

### 19.1 Principles

- **Show what is observed, not what was requested.** Every robot, server and deployment shows desired, prepared and active state separately. A successful click never displays as "running".
- **Every state has a reason.** Each status shows a label, a reason code with plain-language text, when it started, and **how old the data is**.
- **Infrastructure health is not task success.** A healthy worker is shown separately from task outcomes. Outcomes are labeled attempted, inferred or confirmed; unknown stays unknown.
- **Stale data looks stale.** When a site is disconnected, its last known state is shown greyed with "last seen 4 min ago", never as live.
- **The cloud console cannot start motion.** It can request activation. A person at the site approves it (§20.6).
- **No hidden endpoints.** The console uses only the public API (§20). Anything the console can do, the CLI and SDK can do.

### 19.2 Roles and permissions

| Role | Can | Cannot |
| --- | --- | --- |
| Viewer | See status, metrics, audit | Change anything |
| Deployer | Register models and profiles, prepare and validate revisions, request rollout or rollback | Approve physical activation |
| Site operator | Approve or refuse activation locally, take over, reset faults locally | Change org settings |
| Admin | Manage org, projects, sites, SSO, tokens, telemetry routing, support access | Approve motion unless also a site operator |
| Neura support | Only what a customer-granted, time-boxed break-glass session allows | Anything outside that grant |

Deploy and physical-activation permissions are always separate. Admin and activation-related actions require MFA.

### 19.3 Screens

| Screen | Purpose |
| --- | --- |
| Home | Sites at a glance, items needing attention first |
| Site | Servers, GPUs, agent version, connection (last heartbeat age), offline-authorization expiry countdown |
| Connect a server | Enrollment token, install command, live preflight results with fixes (§17.2) |
| Robots | Profile, device enrollment, current state, active revision, lease holder |
| Models | Artifacts, manifest, licenses, compatibility matrix against robot profiles with reasons |
| Execution profiles | Timing, merge mode, starvation response, linked evidence |
| Capacity | "This server can run N robots at this profile", with assumptions and headroom |
| Releases | Prepare → validate → evidence → request activation → canary → rollout; rollback |
| Live sessions | Per robot: state, buffer level, observation age, deadline misses, recent faults. No video unless the customer opted in to payload collection |
| Faults | Latched faults with reason code, the local response taken, linked traces |
| Audit log | Every change and cloud instruction, with actor and result |
| Settings | SSO, API tokens, telemetry routing, outbound ledger view (§18.2), support access grants |

### 19.4 State vocabulary

| Console label | Meaning | Examples of underlying state |
| --- | --- | --- |
| Preparing | Revision is being pulled, verified, warmed or profiled | Site agent preparation phases |
| Ready | Compatible and able to run; not moving | Robot runtime `Ready`/`Armed` |
| Running | Executing accepted actions | Robot runtime `Running` |
| Waiting | Temporarily starved; the approved local response is active | Robot runtime `Waiting` |
| Needs attention | Faulted, rejected or blocked; human action required | `Faulted`, admission rejected, preflight failed |
| Offline | No recent data; last known state shown with its age | Heartbeat missing |
| Unknown | No data has ever arrived | New enrollment |

### 19.5 Local site console

A small local web UI and CLI served by the site agent, available when the cloud is not. It shows status, approves or refuses activation, and resets faults. Operators never depend on the internet to stop, check or approve their robots.

### 19.6 Technology

TypeScript, React, a typed client generated from the OpenAPI spec, server-state caching with background refresh, and accessibility (WCAG 2.1 AA) as an acceptance criterion. The local site console uses the same components against the agent's local API.

---

## 20. Console ↔ backend communication

### 20.1 One public API

REST over HTTPS with JSON. The **OpenAPI 3.1 specification is the source of truth**; the console, CLI, Python SDK and customer CI all use clients generated from it.

- Versioned in the path (`/v1/`). Within a version, only additive changes. Breaking changes need a new version and a published deprecation period (*illustrative* six months).
- The management API is never on the robot action path.

### 20.2 Resource shape

Every resource has the same three parts:

```json
{
  "metadata": { "id": "dep_...", "project": "prj_...", "version": "17", "created_at": "..." },
  "spec":     { "model_artifact": "art_...", "execution_profile": "exp_..." },
  "status": {
    "phase": "Preparing",
    "reason": "ARTIFACT_VERIFYING",
    "message": "Verifying runtime image signature",
    "observed_revision": "rev_...",
    "observed_at": "...",
    "data_age_seconds": 3
  }
}
```

`spec` is what the user wants. `status` is only ever written from site observations, so the console cannot confuse the two.

### 20.3 Resources

Organizations, projects, sites, compute servers, enrollment tokens, robots, robot profiles, model artifacts, execution profiles, deployments, revisions, rollouts, activation requests, sessions (read-only), faults (read-only), events, audit records, metrics queries (aggregates only), API tokens, support grants, operations.

### 20.4 Changes and long-running work

- Every `POST` requires an `Idempotency-Key`; a repeated key returns the original result.
- Updates use optimistic concurrency (`ETag` / `If-Match`); a conflicting write returns `412` instead of silently overwriting.
- Work that depends on a site (prepare, roll out, roll back) returns `202` with an **Operation** resource: `Pending → Running → Succeeded | Failed | Cancelled`, with reason codes. An operation succeeds only when the site acknowledges the result.
- The audit record is written in the same transaction as the change; if auditing fails, the change fails.

### 20.5 Errors, reads and limits

- Errors use RFC 9457 problem details with a stable `reason` code from one catalog shared with the data plane and site agent, plus a `retryable` flag.
- Cursor-based pagination and filtering on every list.
- Rate limits per token, reported in response headers.

### 20.6 Live updates

- A server-sent events stream per project (`/v1/projects/{id}/events`) with resumable cursors and a retention window (*illustrative* 24 hours). Events say *what changed*; the console then re-reads the resource for the truth.
- If the stream fails, the console falls back to polling and labels data age (§19.1).

### 20.7 Authentication and authorization

- People: customer SSO via OIDC (authorization code with PKCE), short-lived access tokens, MFA for admin and activation-related actions.
- Machines: scoped, expiring API tokens, created by admins and shown once.
- Authorization is checked by the server on every request; the console hiding a button is never the control.
- Sites and robots do not use this API; they use certificates (§17).

### 20.8 Control plane ↔ site agent

A separate internal protocol (gRPC over the agent's outbound stream, §17.3):

- **Down:** signed desired-state manifests and allowed commands (prepare, roll out, roll back, collect diagnostics), each checked against the site's local boundaries (§17.4).
- **Up:** acknowledgements, status reports with reason codes, and telemetry batches restricted to the published schema (§18.2).
- The console never talks to a site directly.

### 20.9 Activation flow end to end

```mermaid
sequenceDiagram
    participant U as Deployer (console)
    participant C as Control plane
    participant A as Site agent
    participant O as Site operator (local)
    participant R as Robot runtime
    U->>C: POST activation request (Idempotency-Key)
    C->>C: Record intent + audit; Operation Pending
    C->>A: Signed desired state
    A->>A: Check local boundaries, revision prepared
    A->>O: Approval request at task boundary
    O->>A: Approve
    A->>R: Activate revision
    R->>R: New task epoch, clear queue, fresh observation
    R-->>A: Acknowledged active
    A-->>C: Status: active revision, reason
    C-->>U: Operation Succeeded (event + re-read)
```

If the operator refuses, the site disconnects, or the robot is not at a task boundary, the operation stays Pending or ends Failed with a reason. The cloud never retries motion on its own.

---

## 21. Service levels

### 21.1 Definitions

- **SLI**: what we measure. **SLO**: our internal target. **SLA**: what we promise in a contract, with credits.
- SLAs are set **below** the SLOs we actually meet, and only after pilot data exists. Until then, every target here is an *illustrative* internal SLO.
- Each plane fails differently, so each has its own service levels. Safety properties are **invariants**, not percentages: any infrastructure-caused acceptance of a stale, duplicated or unauthorized action is a Sev-1 incident regardless of how often it happens.

### 21.2 Cloud control plane (illustrative SLOs)

| SLI | Target |
| --- | --- |
| API availability (valid requests without server error), monthly | 99.9% |
| Read latency p95 / write latency p95 (excluding Operation completion) | < 300 ms / < 1 s |
| Desired-state delivery to a connected agent, p95 | < 30 s |
| Site status visible in console while connected, p95 | < 10 s |
| Event stream resumable without loss within retention | 100% |
| Control-plane recovery: data loss window / time to restore | ≤ 5 min / ≤ 4 h |
| Effect of a control-plane outage on running robots | None (design invariant) |

### 21.3 Site data plane (per qualified execution profile)

Targets are set during qualification for each profile and measured on the site.

| SLI | Illustrative target |
| --- | --- |
| Site agent and gateway availability during the customer's operating hours | 99.9% |
| Requests answered within budget at admitted load | ≥ 99.9% |
| Neura-attributable starvation events per robot-hour | Set per profile from qualification |
| Rollback to the previous warm revision, at the next task boundary | < 60 s |
| Conformance and fault suite on the deployed version | 100% pass |
| Unsafe action acceptance caused by Neura | Zero (invariant, Sev-1) |

Admission control (§9.3) is what keeps these honest: load that would break them is refused, not accepted.

### 21.4 Support (illustrative, production tier)

| Severity | Definition | First response |
| --- | --- | --- |
| Sev-1 | Production robots stopped by Neura, or a safety invariant violated | 1 h, 24/7 |
| Sev-2 | Degraded production; releases or rollback blocked | 4 business hours |
| Sev-3 | Non-production issue, workaround exists | 1 business day |
| Sev-4 | Question or feature request | 3 business days |

### 21.5 Responsibility boundaries

SLAs exclude causes outside Neura: customer robots, controllers, networks and GPUs outside the qualified envelope; model task quality; changes made outside Neura. Attribution uses Neura's own traces and reason codes, which the customer can inspect.

### 21.6 How service levels are run

- SLIs come from site telemetry and control-plane monitoring; each customer sees a per-site service-level view and a public status page for the cloud.
- Internal error budgets: when a budget is spent, feature releases stop until reliability work brings it back.

---

## 22. Customer value

A robot company running learned policies today rebuilds the same infrastructure for each program: serving, timing and buffering, fault handling, model release and rollback, monitoring, and capacity planning. Neura sells that infrastructure as a supported product. These are product hypotheses to validate in discovery and pilots.

| Value | What changes for the customer | How we measure it in a pilot |
| --- | --- | --- |
| **Engineering time not spent** | Their autonomy team stops building and maintaining serving and release tooling per robot program | Engineer-hours on deployment and infrastructure before vs during pilot; tickets and incidents owned |
| **Faster, safer model releases** | New policy versions go from "custom rollout" to staged, evidence-backed activation with rollback | Time from trained checkpoint to running on robots; failed or reverted releases |
| **Fewer stalls and incidents** | Starvation, stale actions and faults are handled consistently and are visible | Starvation events and deadline misses per robot-hour; intervention rate |
| **Lower compute per robot** (only where measured) | Several robots share a GPU when the workload allows it | GPU-hours per robot-hour vs the dedicated baseline (§10), including reserve capacity |
| **Evidence for their own customers** | Deployment, version and fault records they can show to end customers and auditors | Records produced; customer feedback |

Engineering time and release safety are the most reliable value; compute savings depend on the workload and must never be the only claim. Some workloads will not benefit from sharing; we say so and still deliver the operational value, or we decline the deal.

Value equation for each pilot, agreed before it starts:

`value = avoided engineering cost + avoided incident cost + compute savings (measured) − Neura fees − integration effort`

---

## 23. Making it simple

Simplicity is a product requirement with acceptance tests, not a slogan.

| Step | Target experience |
| --- | --- |
| Connect a GPU server | One command, preflight report with fixes, no firewall changes (§17) |
| Bring a model | Point at an existing LeRobot-format checkpoint or a supported package; Neura builds the manifest and asks only about what it cannot infer |
| Check compatibility | Automatic contract check against the robot profile, with plain-language reasons for any mismatch |
| Know if it fits | Automatic profiling produces "this server can run N robots at your timing", with its assumptions shown |
| Integrate the robot | Open-source robot runtime and SDK; reference adapters for supported arms |
| Ship a new version | Stage, compare, activate at a task boundary, roll back: one workflow in console or CLI |
| Prove it yourself | Customers run the same conformance and fault suites we use |

*Illustrative* internal targets: first server connected in under 30 minutes; a supported policy running on a supported robot in simulation on day one; second installation needs no custom code from us.

---

## 24. Why customers would stay: defensibility

At launch Neura has **no moat**. The code is replicable, generic GPU hosting exists, model providers bundle serving, and strong teams can build in-house. Defensibility has to be earned. These mechanisms compound with each deployment:

1. **A qualified integration library.** Every policy × robot × controller combination we qualify (adapters, conformance evidence, validated fault responses) makes the next customer with that combination cheap and fast. Competitors start from zero on each one.
2. **Embedded in the operating workflow.** Once releases, rollbacks, fault records and audit history run through Neura, replacing it means rebuilding those workflows. Data stays exportable, so customers stay because the product is useful, not because they are locked in.
3. **Performance knowledge across hardware and models.** Profiles of latency, memory and capacity across GPU types, models and batch sizes make capacity estimates accurate out of the box. Only performance metadata is used, and only with customer consent; never payloads or weights.
4. **The open contract as an ecosystem.** If model developers and arm makers publish Neura-compatible packages and pass our conformance tests, Neura becomes the default way to deploy them. This only works if the contract is open (§14).
5. **A safety and reliability track record.** A record of operating without infrastructure-caused incidents, with an auditable open runtime, is slow to earn and hard to copy.
6. **Neutrality across model providers**, which matters to customers using more than one model family. Not enough on its own.

Not moats: the scheduling algorithm, the transport, the console. Those are table stakes.

### Positioning against alternatives

| Alternative | When it wins | Where Neura must win |
| --- | --- | --- |
| Build in-house | Single program, strong infra team, unique hardware | Many programs or robot types; small teams; release safety; time to deployment |
| Generic GPU cloud | Hosting is the only need | On-site serving, robot-side semantics, fleet releases |
| Model-provider bundle | Customer uses only that provider's models | Mixed models, customer-owned weights, on-site data |
| Open source alone (e.g. LeRobot remote inference) | Research, single robot | Multiple robots per server, fleet operations, support |

---

## 25. What to validate first with customers

1. Do target customers rank infrastructure engineering time and release safety as a real problem with a budget?
2. Will they run a policy outside their present runtime?
3. Which measure would they sign a pilot against?
4. Does the second customer actually reuse the first customer's adapters?

If the answers are no, the strategy changes before more is built.

---

## 26. Remaining open items (these need real customer workloads)

- Customer-specific timing envelope, controller interface and validated local fault responses.
- Pre-registered trial sizes and non-inferiority margins for a pilot.
- Offline authorization expiry acceptable to the customer.
- Whether a customer's policy is stateful (would trigger the session-state design).
- Pricing.

## References

- LeRobot remote inference design (Zenoh transport, aligned/blend/append/RTC chunk modes, latched faults, one client per server): https://github.com/huggingface/lerobot/pull/4937
- LeRobot async inference documentation: https://huggingface.co/docs/lerobot/main/async
- Black, Galliker, Levine, *Real-Time Execution of Action Chunking Flow Policies* (NeurIPS 2025): https://arxiv.org/abs/2506.07339
