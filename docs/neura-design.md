# Neura: engineering design v0.1

Status: design in effect; M1 (foundation), M2 (data plane core), M3 (control plane), M4 (site agent) and M5 (web console) implemented; M6 (models and GPU sharing) built, its GPU benchmark report awaiting the GPU runner; M7 onward not yet built · October 15, 2026
Scope: Neura only (deployment and fleet serving of customer-owned robot policies). This is the single design document for Neura: architecture, connectivity, privacy, console, API, service levels, value and defensibility.
Parent document: NucleiLabs platform and product technical blueprint v0.1.

This document turns the blueprint into decisions an implementation can follow. Each decision lists what it is, why, and what evidence would make us revisit it. Numbers marked *illustrative* are starting points for simulation and tests, never operating values for a customer robot.

## 0. Vision and positioning

**Vision:** make deploying robot AI a repeatable software operation. A robotics company should be able to take a supported trained model, connect its robots and computing hardware, and operate that deployment without repeatedly building the serving, coordination and release infrastructure itself. The long-term ambition is that Neura becomes the execution layer robotics developers routinely use.

**First step:** deploying one supported policy across multiple manipulation robots using shared compute.

**How we describe it:** *NucleiLabs builds infrastructure that makes robot AI easier to run. Our first product, Neura, lets robotics companies deploy their models across multiple robots using shared GPU servers. It coordinates inference around each robot's timing requirements and handles model updates, helping teams scale without rebuilding their serving infrastructure.*

**Category:** robot AI serving infrastructure. Deployment management makes the product usable; coordination makes shared serving effective.

**What the customer keeps:** their model, robot design, task capability and operating procedures. Neura owns a supported portion of the recurring infrastructure work around them.

**Intended benefit, still to be validated:** less infrastructure work and better compute efficiency while preserving task performance. The first meaningful milestone is a customer operating multiple robots through Neura, showing a measurable benefit, and choosing the same system for its next installation.

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

Two RTT figures, both measured on the robot clock over a sliding time window (10 s by default) and sent with every heartbeat (§11):

- **p99 RTT** for capacity planning (admission), so planning stays conservative while a recent spike is in the window.
- **Recent RTT** (median of the last five samples) for per-request bounds at the gateway, so serving recovers as soon as the link does. *Implementation finding (M2):* using a long-window p99 for per-request bounds made a robot unservable for about a minute after a single 0.7 s delay spike, because every request was judged too old on arrival. The robot's own acceptance check remains the authority on freshness.

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

*Batching window (M6).* With an idle worker and fewer than `max_batch` requests queued, the scheduler holds the batch open for up to `batch_window_ms` (default 2 ms) after the oldest queued request. It holds only if every queued request stays dispatchable and every request that can still make its deadline still makes it with one more member in the batch. Admission counts the window in its worst case. Without the window, robots that ask within a few milliseconds of each other did not share a pass, and whether they did depended on event-loop timing. The later ones waited a whole forward pass, which the benchmark found in real time and the simulation did not reproduce. The window makes batching deliberate, the same in the gateway and the simulator.

### 9.3 Admission control

A session is admitted to a group only if the profiled latency at the resulting concurrency still satisfies every admitted session's refill inequality (§8), with configured reserve capacity and memory headroom. Admission is where overload is refused. There are no growing queues.

The implemented check (M2) models the worst case: every session requests at once, one non-preemptible batch is already running, and sessions are served in deadline order in batches of at most `max_batch`. For each session it requires:

1. wait before dispatch + uplink ≤ the profile's maximum observation age at dispatch;
2. completion + link ≤ the maximum observation age at acceptance;
3. completion + link ≤ the starvation budget;

plus average utilization ≤ `1 − reserve` and memory headroom.

*M6 update:* the scheduler's batching window (§9.2) serves robots that ask together in one batch, so synchronized arrivals cost one batch's latency rather than a queue of single batches. The CPU benchmark (§12.1) also found that robots desynchronize on their own when a pass is slower than a control period.

*Implementation finding (M2), important for the GPU-sharing bet (§25):* with the example's illustrative 100 ms dispatch-freshness limit and 45 ms batches, condition 1 caps one worker at about one batch of sessions (8), well below its throughput limit. Starvation budgets were never the binding constraint. Synchronized requests are realistic: robots that start together stay in phase. Two levers can raise safe capacity, and both belong in the M6 benchmarks:

- **request phasing:** spreading sessions' refill times so bursts do not align;
- **workload-specific freshness limits:** qualified per execution profile.

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

### 9.5 Sharing when robots run different fine-tunes

GPU sharing is Neura's primary bet (§25), and the biggest technical threat to it is that customers often fine-tune a separate checkpoint per cell, task or site. Plain batching (§9.1) then gives nothing, because each robot is its own batch group. Neura needs a sharing strategy for each case, and the sharing economics benchmark (§16, step 0) measures all of them:

| Case | Sharing mechanism | Expected benefit (to be measured) |
| --- | --- | --- |
| Same weights on several robots | Batching in one forward pass | Highest |
| Shared base model with per-robot adapters (e.g. LoRA) | Multi-adapter batching: one base forward pass with per-request adapter weights, as in multi-LoRA LLM serving | High, if customers fine-tune with adapters |
| Different full fine-tunes of the same architecture | Co-location: several workers on one GPU, time-multiplexed, limited by memory and contention | Moderate; mostly fills idle GPU time between requests |
| Different architectures | Co-location only | Lowest; sometimes negative |

Product consequences:

- Capacity estimates (§23) always state which case applies.
- Where adapter-based fine-tuning would let a customer share GPUs, Neura says so and quantifies it. It never requires it.
- Multi-adapter batching is built only if the benchmark shows it pays for its complexity, and only for adapters whose outputs match unbatched execution within the declared parity tolerance (§2.3).

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

### 12.1 Implementation notes (M6)

- **Adapters** (`packages/neura-adapters`). The interface is `load → warmup(batch sizes) → infer(batch)` with the declared properties (deterministic, batching, multi-adapter, stateless, device, precision, parameters). Registered: `mock`, `lerobot-act`, `lerobot-smolvla`, `lerobot-pi0`, `lerobot-pi05`, `lerobot-diffusion`. A LeRobot checkpoint (`save_pretrained` layout) runs with its own pre- and post-processors, so outputs match LeRobot's inference for the same inputs. Loading checks the checkpoint against the contract (input names and shapes, action size, chunk length) and refuses a mismatch at prepare, never on a robot. Checkpoints that read a history of observations (Diffusion Policy with `n_obs_steps > 1`) are refused as stateful. bf16 and fp16 run under autocast, because the policies create float32 tensors of their own; ACT in bf16 stays within 2 degrees of fp32. Device out-of-memory is a typed error.
- **Verified here:** ACT and Diffusion Policy end to end on CPU (synthetic checkpoints: the real LeRobot architectures with random weights, `neura_adapters.synthetic`). SmolVLA and π0 share the same code path but start from pretrained vision-language backbones downloaded from their publishers, which this build environment cannot reach; their first run is on the GPU runner.
- **Policy workers.** `neura-worker serve` runs one model on one device for one batch group and serves `neura.v1.PolicyWorker` (`Describe`, `Infer`) on a Unix socket (mode 0600, in a private directory). Its first stdout line announces ready or a typed load error, and it exits when its stdin closes, so a worker never outlives its gateway. The gateway's `WorkerBackend` supervises one worker per group and restarts it with backoff. Failures reach robots as typed rejections:
  - a worker that exits during a batch: `WORKER_CRASHED`;
  - a batch not finished by its latest deadline (and at least `worker_timeout_ms`): the worker is killed, `WORKER_TIMED_OUT`;
  - no worker ready: `WORKER_UNAVAILABLE`; a policy error: `WORKER_INFERENCE_FAILED`; a model that cannot load: `WORKER_LOAD_FAILED` or the specific reason.

  A result is delivered only from the worker incarnation the batch was sent to. Batch groups key on the concrete device (`artifact:cuda:0:bf16`); a reload stops the workers of groups no deployment uses, freeing their memory. Workers report their peak memory. The simulated backend (mock policy, profile-sampled latency) remains for the Compose stack and tests. Until M7, workers run in the agent's environment (`neura-agent-gpu` image: PyTorch and LeRobot); the pinned artifact image and sandbox above are M7.
- **Weights at the site** (§18.1). The agent keeps a content-addressed store (`<state>/artifacts`). Files arrive by `neura-agent import-weights DIR [--artifact FILE]` or from configured sources (`NEURA_AGENT_ARTIFACT_SOURCES`: directories or HTTPS base URLs laid out as `<source>/sha256/<hex>`, optional bearer token). Every file is verified against its digest before it is stored. A prepared artifact becomes a read-only checkout of hard links to verified blobs. Weights never pass through the control plane. Missing weights fail prepare with `AGENT_ARTIFACT_UNAVAILABLE`, naming the files and the fix.
- **Prepare with real workers** (`NEURA_AGENT_GATEWAY_BACKEND=workers`, the default):
  1. choose the device: the group's existing one, else the allowed GPU with the fewest groups (`AGENT_DEVICE_UNAVAILABLE` when the profile needs a GPU and none may be used);
  2. check out the weights;
  3. run `neura-worker profile` on that device: load, warm up, and measure p50, p99 and peak memory per batch size;
  4. estimate capacity from the measured profile. The estimate now carries `source: measured_profile`, the `sharing_case` (§9.5), the device and the number of co-located groups.

  The gateway's scheduler and admission start from the same measured profile. The console shows the sharing case and the profile's source.
- **Multi-adapter batching** exists as a mechanism (`neura_adapters.lora`): forward hooks add each request's LoRA term over a stacked, zero-padded adapter bank, with each layer's batch dimension found by a probe pass. Adapters on layers whose forward is bypassed (fused attention) are refused, not silently ignored. `check_parity` compares a mixed batch with each adapter merged and run alone. Per §9.5 it is not in the data plane: the benchmark measures whether it pays first.
- **Benchmark harness** (`packages/neura-bench`, `make bench`): the four sharing cases against the dedicated baselines (§10), described in its README.
  - Measurements through real worker processes: latency per batch size, contention between co-located workers, a background job, multi-adapter cost and parity.
  - A simulated link calibrated from one robot alone in real time.
  - Simulated sweeps using the production robot runtime, gateway and scheduler, with processor sharing on the device. Each configuration's capacity is the largest robot count meeting the SLO.
  - Real-time validation of the main configurations with the production gateway, real workers and gRPC robots.

  Reports go to `bench/reports/<device>/` as `results.json` and a generated `README.md`. The GPU workflow (`.github/workflows/bench.yml`) runs on a self-hosted runner labeled `gpu`. Neither configuration uses `torch.compile` or CUDA graphs, so the comparison stays like for like (§10).
- **What the CPU development host showed** (`bench/reports/cpu-intel-xeon-processor/`; a 4-core CPU with ACT `small` at 128 x 96, so not GPU evidence). The harness fixed problems the unit tests could not see:
  - The scheduler dispatched the first of several near-simultaneous requests alone, so the rest waited a whole forward pass. Whether they batched depended on event-loop timing; the batching window now makes it deliberate (§9.2).
  - The scheduler grew batches past what robots could accept: every chunk of a four-robot batch arrived after the 150 ms acceptance limit and was discarded. Batches now stop growing at members' acceptance limits, and chunks robots reject count as deadline misses in the benchmark. Earlier runs had flattered shared serving by not counting them.
  - Sampling noise in a measured profile can make a larger batch look faster. Profiles are now monotone in batch size (the conservative reading).
  - Synthetic weights produced out-of-range actions, and the robot runtime latched `ACTION_OUT_OF_RANGE` as designed. The synthetic checkpoints now center actions in the robot's ranges.
  - Robots that start together do not stay in phase when a forward pass takes longer than a control period: the first ones served run ahead. With the batching window they are served together and stay in phase. On a GPU, where a pass is shorter than a period, phasing (§9.3) needs the GPU run to settle.

  The numbers, with simulation and real time agreeing within single events except where the CPU slows the harness itself:
  - ACT takes 79 ms at batch 1 and 192 ms at batch 8 here.
  - Same weights: Neura and one process per robot both serve 8 robots, failing at 12. With synchronized arrivals Neura serves 2 and one process per robot 1.
  - Multi-adapter batching passes parity (3e-7) and costs 5 to 11% over the base model, so here it serves as many robots as same-weights batching.
  - Diffusion Policy (244 ms per pass, 8-action chunks) cannot serve one robot at 30 Hz on this CPU.
  - A background job on the same CPU fails every configuration.
  - Admission's worst case admits none here: a 106 ms batch-1 p99 already exceeds the 100 ms dispatch-freshness limit. That is conservative against the 8 robots served with phased arrivals and the 2 with synchronized ones.

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

## 15. Repository layout

A uv workspace; each component is a package under `packages/`. Entries marked *(M1)* exist; the others are created by the milestone that needs them.

```
proto/neura/v1/            # protobuf schema (open) (M1)
api/openapi.yaml           # public management API, spec-first (M1)
catalog/reason_codes.yaml  # shared reason-code catalog (M1)
schemas/                   # generated JSON Schemas for contract documents (M1)
packages/
  neura-contracts/         # manifests, profiles, compatibility check, CLI (open) (M1)
  neura-proto/             # generated protobuf/gRPC bindings, import `neura.v1` (open) (M1)
  neura-control/           # control plane service (proprietary) (M1 skeleton, M3)
  neura-agent/             # site agent and local site API (proprietary) (M1 skeleton, M4)
  neura-runtime/           # robot runtime, session client, gRPC client (open) (M2)
  neura-server/            # gateway, leases, scheduler, admission, workers (proprietary) (M2)
  neura-sim/               # simulated arms and networks, deterministic site simulator (M2)
  neura-adapters/          # ACT, SmolVLA, π0 adapters (open) (M6)
  neura-sdk/               # Python client generated from the OpenAPI spec (open) (M5)
console/                   # web console: React + TypeScript, generated API client, e2e tests (proprietary) (M5)
bench/                     # benchmark harness and reports (M6)
examples/contracts/        # example contracts for an SO-101-class arm (M1)
deploy/                    # Dockerfile and Compose dev stack (M1)
tools/                     # generators and checks (M1)
docs/                      # internal design (this document; never published)
site/                      # public developer docs site source (§26) (M7)
```

Generated files (protobuf bindings, reason-code module, JSON Schemas, example bindings) are committed; `make check-generated` fails CI if any is stale.

---

## 16. Build sequence

| Step | Delivers | Exit criterion |
| --- | --- | --- |
| 0 | **Sharing economics benchmark** (runs alongside steps 1–3; needs one GPU, no robot): SmolVLA and π0-class models with synthetic observations; latency and memory vs batch size; robots per GPU at illustrative timing envelopes (§8); the four sharing cases in §9.5; dedicated baseline (§10) | A report stating robots per GPU shared vs dedicated for each case, and the GPU cost per robot-hour. This is the first evidence for or against the primary bet (§25) |
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

### 16.1 Platform build plan

Decision (October 14, 2026): build the complete Neura platform as designed in this document, engineered for production and scale, before taking it to customers. The demo is the real product: a customer is onboarded in the console, connects their GPU server to the NucleiLabs backend, registers a model, deploys it to robots, and manages it, all on the real system, with simulated robots until hardware is attached.

Every layer exists from the first milestone and deepens with each one, so the end-to-end flow always runs on real components, never mock-ups.

| Milestone | Delivers | Done when |
| --- | --- | --- |
| **M1. Foundation** ✅ | Monorepo and tooling (uv, ruff, mypy strict, pytest), CI with required checks, protobuf schema (§11), OpenAPI spec (§20), reason-code catalog, contracts library with RFC 8785 digests and CLI (§4), control-plane and site-agent service skeletons, local dev stack in Docker Compose (Postgres, control plane, site agent). Console tooling (pnpm) moves to M5; simulated robots join the stack in M2 | `make dev` brings up the whole stack; `make smoke` verifies it end to end; schema, contract and compatibility tests pass in CI |
| **M2. Data plane core** ✅ | Robot runtime (§5–§7), gateway with leases and admission (§6, §9.3), scheduler (§9), policy workers, simulator with fault injection (§7). Sans-IO cores shared by a deterministic simulator (every node on its own clock) and the real gRPC adapters; gateway and simulated robots in the dev stack | The full fault suite and property-based invariant tests pass in CI; N simulated robots served by one worker, in simulation and over gRPC |
| **M3. Control plane** ✅ | FastAPI service on Postgres: organizations, projects, sites, robots, artifacts, profiles, deployments, revisions, operations, events stream, audit, RBAC, OIDC login, API tokens (§20). Alembic migrations; transactional idempotency keys; `If-Match` concurrency; audit and change events in the same transaction as each change; site-report service that alone writes observed state (used by the agent in M4); `GET /v1/me`; bootstrap CLI and development bootstrap | Every response in the test suite is validated against the OpenAPI spec; every spec operation is implemented; idempotency (including concurrent retries), concurrency, roles, OIDC and audit tests pass; the dev stack onboards a customer through the API |
| **M4. Site agent** ✅ | Enrollment with one-time tokens and on-host keys, preflight, outbound stream (TLS), signed manifest reconciliation, contract verification at the site, local boundaries, at-most-once commands, local approval, task-boundary revision switches, signed contact proofs for offline authorization, supervised data plane, local site API and CLI (§17, §19.5); implementation notes in §17.6 | A clean machine enrolls from one command and runs a deployment pushed from the control plane; keeps operating through a control-plane outage and reconnects by itself (`make smoke` runs the whole pilot on the Compose stack) |
| **M5. Web console** ✅ | Onboarding, connect a server, robots, models, compatibility, capacity, releases and rollback, live sessions, faults, audit, settings (§19); members API with invitations; sign-in configuration in `/v1/meta`; site-computed capacity estimates; generated Python SDK; implementation notes in §19.7 | The full onboarding-to-deployment journey works in the browser against the real backend, with end-to-end browser tests (`make console-e2e`, in CI) |
| **M6. Models and GPU sharing** (built; GPU run pending) | ACT, SmolVLA and π0-class adapters (§12), batching, multi-adapter batching and co-location (§9.5), profiler and capacity estimates, benchmark harness (§10); policy worker processes, the site's artifact store, measured profiles at prepare; implementation notes in §12.1 | Benchmark reports for each sharing case vs the dedicated baseline, reproducible from one command on the GPU runner. Built and run on a CPU development host (`bench/reports/`); the GPU report is produced by `make bench` on the GPU runner |
| **M7. Production hardening** | mTLS everywhere, artifact signing, customer-managed keys, outbound ledger (§18), Grafana dashboards, installer and upgrades, chaos and soak tests, docs site (§26) | Security review checklist complete; 72-hour soak with injected faults and no invariant violations |
| **M8. Physical validation** | SO-101-class arms connected through the same platform | Fault suite and parity pass on hardware |

Engineering standards for every milestone: typed code, tests written with the code, CI gates that cannot be bypassed, architecture decisions recorded in this document, and no component on the action path depending on the cloud.

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

### 17.6 Implementation notes (M4)

What M4 built, and where it deliberately differs from the plan above.

**Identity and transport**

- The agent generates an Ed25519 key on the server and sends only the raw public key with the one-time token; the token is single use and expires. Certificates, a site CA and mTLS for robots are M7: until then each stream is authenticated with a short-lived EdDSA JWT (at most 5 minutes) that the agent signs with its key and the control plane verifies against the enrolled public key. Deleting a server ends its stream and invalidates its credential.
- The agent uses TLS to the control plane by default (system roots or a configured CA bundle); plaintext must be enabled explicitly and is for development only. The control plane terminates TLS itself or declares that a load balancer does; staging and production refuse to start otherwise.

**Desired state**

- A site's manifest is canonical JSON (RFC 8785) holding its robots, its deployments and every live revision with the full contract documents, so the site can operate and verify without the cloud. The control plane signs it with Ed25519 and the agent checks it against the key it pinned at enrollment. The agent rejects:
  - an unknown key or a bad signature;
  - a manifest for another site;
  - an older generation.
- The generation increments exactly when the content digest changes.
- A revision that robots are running stays in the manifest whatever its phase (for example after a failed re-prepare). Only an activation changes what robots run.

**Commands and operations**

- Commands live in an outbox table and are delivered on the stream. The agent records each command id durably before acting and executes it at most once. A redelivery is acknowledged again together with its last report.
- Operation reports are persisted and resent after a reconnect; the control plane ignores ones it has already applied.
- An operation the agent was running when it restarted is reported `AGENT_OPERATION_INTERRUPTED` rather than resumed.
- Prepare verifies at the site, from the documents the site itself received:
  - each document's kind and digest;
  - the revision digest;
  - the compatibility check.

  Pulling and verifying weights and images arrives with real model workers (M6).
- Every command is checked against the server's local boundaries (`NEURA_AGENT_ALLOWED_OPERATIONS`, `NEURA_AGENT_ALLOWED_GPU_INDEXES`). An instruction outside them fails with `AGENT_BOUNDARY_DENIED`, and every instruction and decision goes to the local audit log.

**Activation**

1. An activation request reaches the site and waits for local approval. The cloud cannot approve.
2. On approval, each robot's assignment becomes `{current, next}`, and the gateway serves both revisions for that robot.
3. Each robot asks the local site API which revision to run and switches at its own task boundary: it finishes the task, closes its session, verifies the new contracts and opens a session for the new revision.
4. The activation succeeds when every robot runs the new revision. The old revision is then removed and its remaining sessions closed.

A refusal (`AGENT_ACTIVATION_REFUSED`), a cancel from the control plane, or robots not switching within the activation window (`AGENT_ACTIVATION_TIMED_OUT`, 10 minutes by default) reverts the assignment.

**Offline authorization**

- Offline authorization renews only on verified contact:
  - a newer signed generation, which can be committed once and so cannot be replayed;
  - a **contact proof**: the agent sends a random nonce with each status report, and the control plane returns the manifest key's signature over `CONTACT_PROOF_CONTEXT ‖ site_id ‖ 0x00 ‖ nonce`.

  Neither a replayed message nor a stream left open without the control plane behind it can extend authorization, and no clocks are compared across machines.
- When authorization runs out, the agent stops the data plane and refuses activations until contact returns. It is measured on the server's wall clock so that it survives restarts. A server clock moved backwards extends it; bounding that with monotonic time across restarts is M7 hardening.

**Data plane and local API**

- The agent turns committed state into the gateway configuration and writes it only when it changed. The gateway runs as a supervised subprocess (restarted with backoff) and reloads that configuration on `SIGHUP` without a restart.
- The local site API (§19.5) and the `neura-agent` CLI provide:
  - status;
  - approvals, with approve and refuse;
  - robot credentials;
  - robot assignments;
  - the audit log.
- Mutations require the local operator credential; only its SHA-256 is configured on the server, and without it local approvals are disabled. Robots authenticate with device tokens.

**Observed state in the control plane**

- A site's `connected` is derived from the age of its last report (90 s), so a control-plane process that crashes while serving a stream cannot leave a site shown connected.
- Periodic status refreshes `observed_at` but changes the resource version and emits an event only when a fact changes.
- Every database operation in the site endpoint runs shielded from RPC cancellation, and shutdown waits for streams and their writes before closing the database. A transaction abandoned midway would leak its connection.

**Known limits, scheduled**

- Robot device tokens rather than per-device certificates, and one stream per server (M7).
- The `connected` stream count is per process; with several control-plane replicas, liveness relies on report age.

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

### 19.7 Implementation notes (M5)

**One API, typed end to end**

- The console's client is generated from `api/openapi.yaml` (openapi-typescript and openapi-fetch). So is the Python SDK (`tools/gen_sdk.py`). Both are checked for staleness in CI.
- The spec is enforced at compile time. For example, the generated types make every POST supply an `Idempotency-Key`: the console creates one per user action, and the SDK reuses one across its retries.
- Reason codes reach the console from the same catalog as every other component (a generated TypeScript module), so every state and error shows plain-language text and its code.

**Showing observed state honestly**

- Ages are computed as durations: the server's `data_age_seconds` plus the time elapsed in the browser since the fetch. The offline-allowance countdown is a difference of two control-plane timestamps. The browser clock is never compared with a site's.
- A site whose agent stopped reporting reads as Offline with "last seen …". Its robots' last states are greyed. Liveness comes from report age (§17.6).
- Release pages show per robot the observed active revision, separately from what was prepared. An activation request shows "Waiting for approval at the site" until the site reports. The console has no approve action: approval happens locally (§19.5).

**Live updates**

- The console follows the project's change events with `fetch`, because `EventSource` cannot send an `Authorization` header.
- The server starts each stream with an id-only message carrying its position. A client that reconnects before any event resumes from there and misses nothing; the SDK does the same.
- Events invalidate the cached queries of that project, coalesced over 250 ms. Live views also poll as a fallback.

**Members and sign-in**

- Members are invited by email and bound to their SSO subject the first time they sign in with that email verified by the identity provider. They can also be added by subject.
- An organization always keeps an active admin: the API refuses to demote or remove the last one.
- The console uses OIDC authorization code with PKCE as a public client. The issuer and client come from `/v1/meta`. API tokens remain for development and automation.
- Credentials live in `sessionStorage`. MFA for admin and activation-related actions is enforced by the customer's identity provider.

**Capacity** (§9.3, §19.3)

- When a site prepares a revision, its agent computes "this server can run N robots at this profile" with the admission controller the gateway enforces, so estimate and admission cannot disagree.
- The estimate reports the limiting factor (deadline, dispatch or acceptance freshness, memory, throughput) and the assumptions it used. The control plane validates the estimate's shape before storing it.
- Until the profiler measures the GPU (M6), the latency profile is the configured one, and the estimate says so (`source: configured_profile`). The dev stack's estimate, 8 robots limited by dispatch freshness, matches the M2 finding (§9.3).

**Accessibility and quality gates**

- Semantic HTML, labelled controls and named regions throughout. Native `<dialog>` for modals, so focus is trapped and returned. Live regions announce results, and links are underlined so they never rely on color.
- ESLint runs the strict jsx-a11y rules. The end-to-end journey runs axe on every main screen against WCAG 2.1 AA and fails on serious or critical violations.
- The journey plays every role with the real components, in Chromium, in CI:
  1. The customer uses the console.
  2. A GPU server runs the install command the console shows: a real site agent and gateway.
  3. Simulated robots run the robot runtime.
  4. A site operator approves with `neura-agent approve`.

**Serving**

- The production image is nginx with the static build. It proxies `/v1` on the same origin, so no CORS is needed, with buffering off so the event stream flows.
- It sends a strict Content-Security-Policy, `X-Frame-Options: DENY` and no-referrer. Hashed assets are cached immutably.

**Not in M5**

- The local site console is the agent's local API and CLI (§19.5); a browser UI for it, built from the same components, follows with M7.
- Telemetry routing and the outbound-ledger view (§18.2) arrive with the ledger in M7.
- Metrics charts arrive with measured telemetry in M6.

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

### 20.6.1 Implementation notes (M3)

- Event streams end after a configured lifetime (5 minutes by default) and send `retry:`; clients reconnect with `Last-Event-ID`. Each stream starts with an id-only message carrying its position (M5), so a reconnect never skips events. This bounds connection age behind load balancers. A cursor older than the retention window returns `410 API_CURSOR_EXPIRED`.
- The rate limiter is per process. Running several control-plane replicas needs a shared limiter (for example Redis) before production scale.
- Organization memberships for SSO users are managed with the `neura-control bootstrap` CLI until the user-management API lands with the console (M5).

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
| **Lower compute per robot** (primary bet) | Several robots share a GPU safely, so the customer buys fewer GPUs as the fleet grows | GPU-hours per robot-hour vs the dedicated baseline (§10), including reserve capacity |
| **Engineering time not spent** | Their autonomy team stops building and maintaining serving and release tooling per robot program | Engineer-hours on deployment and infrastructure before vs during pilot; tickets and incidents owned |
| **Faster, safer model releases** | New policy versions go from "custom rollout" to staged, evidence-backed activation with rollback | Time from trained checkpoint to running on robots; failed or reverted releases |
| **Fewer stalls and incidents** | Starvation, stale actions and faults are handled consistently and are visible | Starvation events and deadline misses per robot-hour; intervention rate |
| **Evidence for their own customers** | Deployment, version and fault records they can show to end customers and auditors | Records produced; customer feedback |

**Compute savings from safe GPU sharing are the lead value proposition** (decision in §25). The other values are what make sharing safe to adopt and what keep customers once they do. The savings claim is made per workload, only from measurement against the dedicated baseline, and it states which sharing case applies (§9.5). Where a workload does not benefit, we say so before the pilot rather than discovering it during one.

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

## 25. Bets and what must be validated

Decision (October 14, 2026): NucleiLabs bets on **safe GPU sharing across robots, proven per workload, as Neura's lead value**, together with the operational value in §22.

### 25.1 Accepted bets

Taken deliberately. Evidence is gathered while building; the build does not wait for it.

| Bet | What would show it is wrong |
| --- | --- |
| Sharing GPUs across robots saves meaningful money on real workloads | Step 0 (§16) shows little or no robots-per-GPU gain for SmolVLA- and π0-class models at realistic timing, in all four cases of §9.5 |
| Target customers (smaller suppliers and integrators) feel the infrastructure pain | Discovery conversations consistently describe it as solved or minor |
| They will let a vendor onto the action path, on their own servers | Prospects refuse remote execution of their policy under any terms |
| Neura's releases and fault handling beat what they built in-house | Pilot measures (§22) show no improvement |
| Adapters and integrations transfer from one customer to the next | The second installation needs mostly custom work |

### 25.2 Must be validated before selling a pilot

| Hypothesis | Why it is not a bet | Confirmed only when |
| --- | --- | --- |
| **A budget owner exists and would pay** | Without it there is no business, however good the technology is | A named person with budget authority agrees a paid pilot, a letter of intent, or a funded scoping engagement |

Validation runs alongside the build, starting now: conversations with the target profile (§5 of the blueprint), each recorded with who owns the budget, what the problem costs them today, how they would buy (pilot, annual agreement, integration fee), and the next step they agreed to. Interest without a costly next step does not count.

---

## 26. Developer documentation site

### 26.1 Purpose

A small public developer docs site for NucleiLabs, starting with Neura, so engineers can **understand, evaluate and eventually integrate** the product. At this stage its most important job is honesty: it must never present this design as working software.

### 26.2 Status labels

Every page, and every feature or combination mentioned on a page, carries exactly one label. The label is shown as a banner and stored in the page's front matter, so the build can check it.

| Label | Meaning | Allowed wording |
| --- | --- | --- |
| **Proposed** | Designed, not implemented | "Neura is designed to…", "the proposed interface is…" |
| **Experimental** | Implemented and testable, not qualified; may change without notice | "You can try…", "known limitations:…" |
| **Supported** | Passed the conformance, parity and fault suites for a named combination (§2.3), with linked evidence | "Neura supports…" |
| **Deprecated** | Still works, scheduled for removal, with a date | "Will be removed in…" |

Rules:

- "Supported" is the only label allowed to say "supports". A Proposed page uses future or conditional wording.
- Illustrative numbers (§8, §21) appear only marked as illustrative, never as performance claims or commitments.
- Every API and code sample states its label. Proposed samples are clearly marked as sketches that do not run.

### 26.3 Initial pages (all Proposed at launch, except Development status)

| Page | What it explains | Source in this document |
| --- | --- | --- |
| **What is Neura?** | The problem (repeat infrastructure work per robot program), the intended customer (robot suppliers and integrators with several manipulation workcells and site GPU servers), and the deployment model (customer-owned site compute, cloud management off the action path) | Blueprint §1, §5; §1, §22 here |
| **Architecture** | What runs on the robot (runtime, integration adapter), the GPU server (site agent, gateway, scheduler, workers, local console) and the NucleiLabs cloud (control plane, console). What keeps working when the cloud is down | §3, §8, §13, §17.3, §19.5 |
| **Core concepts** | Model artifacts, robot profiles, execution profiles, compute pools, deployments and revisions, sessions, leases and task epochs, action chunks, the cursor and merge modes | §4, §5, §6, §9 |
| **Privacy and access** | Data classes and where they live, what leaves the site, the outbound ledger, customer-managed keys, roles and permissions, who can deploy vs who can approve motion, update authority and local boundaries | §17.4, §18, §19.2, §20.7 |
| **Compatibility** | Two clearly separated tables: **Supported** combinations (empty until the first one passes qualification, and says so) and **Candidates under evaluation** (ACT, SmolVLA, π0-class; ALOHA sim, LIBERO, SO-101 class) | §2 |
| **Development status** | What exists today, what is in progress, what is under investigation, and what is out of scope, mapped to the build sequence | §16, §27 |

Content that stays internal and is never published: customer targets, pricing, defensibility and positioning (§24), bets and validation (§25), and internal SLO targets (§21) until they become contractual SLAs.

### 26.4 Pages added as implementation progresses

| Page | Added when |
| --- | --- |
| Quickstart | A path works end to end in simulation; its commands run in CI |
| Installation (site agent, robot runtime) | The installer exists and has been run on a clean machine from the docs alone |
| Troubleshooting | From real failures: preflight failures, reason codes, common faults |
| Reason code reference | Generated from the shared reason-code catalog (§20.5) |
| SDK reference | Generated from the Python SDK |
| API reference | Generated from the OpenAPI spec (§20.1) |
| Protocol reference | Generated from the protobuf schema (§11) |
| Conformance suite | The suites can be run by a third party |
| Benchmarks | Reports exist that can be reproduced (§10) |

### 26.5 Docs as part of the codebase

- **Source lives in this repository** under `site/`, so code and instructions change in the same pull request and are reviewed together. A change that alters behavior, an interface or a support claim without updating docs fails review.
- **Generated, not hand-written, where facts come from code:** API, SDK, protocol and reason-code references are generated from their sources. The Compatibility "Supported" table is generated from a data file written by qualification runs, so nobody can add a supported combination by hand.
- **Benchmarks are reproducible:** each benchmark page states the commit, artifact digests, hardware, workload and the command to rerun it, and includes the losing cases (§10).

### 26.6 Tooling

- A **static site**: Markdown sources, built to plain HTML, no server and no database.
- Recommended generator: **Docusaurus**, for built-in docs versioning (needed once there are releases), Mermaid diagram support (the diagrams in this document can be reused) and MDX for the status banner component. Astro Starlight is an equally valid choice if a lighter build is preferred; decide once and do not migrate without reason.
- Hosting on a static host (GitHub Pages or Cloudflare Pages) with a **preview build for every pull request**. Domain to be decided.
- Search built into the static build; no third-party tracking scripts.

### 26.7 CI checks for the docs

- The site builds without warnings.
- No broken internal or external links.
- Every page has a valid status label in its front matter.
- Wording lint: pages labeled Proposed or Experimental cannot contain unqualified claims such as "supports", "guarantees" or "production-ready".
- Once a quickstart exists, its commands are executed in CI against the simulator.

### 26.8 Versioning

Until the first release, the site shows a single "next" version with a site-wide banner: *Neura is in design and early development. Nothing described here is a released product.* From the first release, docs are versioned per release, and "next" remains the in-development version.

---

## 27. Remaining open items (these need real customer workloads)

- Customer-specific timing envelope, controller interface and validated local fault responses.
- Pre-registered trial sizes and non-inferiority margins for a pilot.
- Offline authorization expiry acceptable to the customer.
- Whether a customer's policy is stateful (would trigger the session-state design).
- Pricing.

## References

- LeRobot remote inference design (Zenoh transport, aligned/blend/append/RTC chunk modes, latched faults, one client per server): https://github.com/huggingface/lerobot/pull/4937
- LeRobot async inference documentation: https://huggingface.co/docs/lerobot/main/async
- Black, Galliker, Levine, *Real-Time Execution of Action Chunking Flow Policies* (NeurIPS 2025): https://arxiv.org/abs/2506.07339
