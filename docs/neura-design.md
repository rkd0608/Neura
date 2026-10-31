# Neura: engineering design v0.1

Status: design in effect; M1 (foundation), M2 (data plane core), M3 (control plane), M4 (site agent) and M5 (web console) implemented; M6 (models and GPU sharing) built, its GPU reports in (an L4, with and without VLAs, §9.5); M7 (production hardening) implemented; M8 (physical validation) built and passing on the emulated arm, its hardware run awaiting the arm; M9 (capture), M10 (evaluate) and M11 (improve) implemented, M12 (the loop: console and local tools) in progress (§28) · October 18, 2026
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

Non-goals for Neura: simulators, teleoperation, safety certification, cross-customer GPU sharing. (Training and evaluation were non-goals until October 17, 2026: §28.)

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

### 7.1 Hardware integration and qualification (M8)

**The first integration: SO-101-class arms over LeRobot** (`packages/neura-robots`)

- `So101Arm` is the runtime's `Controller` and `SensorSource` for an SO-101 arm (six Feetech STS3215 servos). It is built on LeRobot 0.6: its Feetech bus driver, the calibration made with `lerobot-calibrate`, and its cameras. Customers keep the calibration and tools they already use. Neura adds what makes the arm safe to serve remotely:
  - **One actuator authority** (§6.3). An exclusive `flock` on a lock file named after the resolved serial device, taken before connecting, plus `TIOCEXCL` on the open port, so any other program fails to open it. `has_authority` fails if the lock file is replaced or the port is closed, and the runtime faults with `ACTUATOR_LOCK_LOST`.
  - **No jump on connect.** Every goal is set to the present position before torque is enabled, so a goal an earlier program left in the servos is never executed.
  - **The contract matches the arm.** Before anything moves, the robot profile is checked against the arm: command names against its motors, units (degrees for joints, 0–100 for the gripper), every command range inside the calibrated range, and fault responses against what the arm can do. A profile that allows a command outside the calibration is refused.
  - **Confirmed holds.** `hold_position` and `controlled_stop` set every goal to the present position, then read the goals and torque back. Only then does the runtime report a stop. Torque is never dropped as a fault response: an SO-101 falls without it, so `brake` is refused. `customer_stop_path` runs the integrator's own command (a relay that cuts servo power, say).
  - **A joint that gives up fails the command.** Every 10 commands (one sync read, about 1 ms) the torque of every joint is read back. A servo whose overload protection dropped torque is not following, so the command fails (`CONTROLLER_COMMAND_FAILED`) and the others hold. The hold is then *not* confirmed, so no stop is claimed.
  - **Motion limit per command.** LeRobot's `max_relative_target` (10 degrees by default) caps each joint's move per command, behind the runtime's range checks.
  - **Honest timestamps.** Joint state is stamped when read. Each image carries its own capture time, converted from its age to the robot's monotonic clock.
- **The emulator** (`neura-arm-emulator`): six STS3215 servos behind a pseudo-terminal, speaking Feetech protocol 0. That covers ping, read, write, sync read, sync write, reg write and action, with checksums, the control table, EEPROM lock and sign-magnitude homing. LeRobot's real bus driver opens it like the arm's USB adapter, so everything above the serial port runs unchanged in development and CI. Dynamics: goal tracking at the servo's speed, and sag under gravity without torque. Faults: an unplugged servo, a silent bus, corrupted packets, a stalled joint, and an overload that drops torque. It is a test double for the bus, not a model of the arm's mechanics.
- **`neura-arm`** is the robot-side process for a real arm: the runtime, the session client and the gateway connection around a connected arm (`examples/robots/so101-arm.yaml`).
  - **Nothing moves without the operator.** It never arms on its own.
  - **Operator commands** (`arm`, `stop`, `takeover`, `reset`, `instruction`, `status`, `quit`) run on the control loop, the one thread that owns the bus. A command that waited more than 2 s (typed while no revision was served, say) is dropped, never executed late.
  - **`reset`** first brings every goal to the present position and torque back on, because a tripped servo answers every read with an error until then. It then revalidates the actuator, bus, calibration and contract (§7).
  - **Revisions.** It serves a pinned revision or whatever the site assigns. A new assignment applies at a task boundary, after the new robot profile is checked against the arm, without letting go of it. A revision that does not fit is refused and the current one keeps running.
  - **Session refusals are shown to the operator** with their reason (`ADMISSION_CAPACITY_EXCEEDED` with the predicted observation age, for example) instead of retrying silently.

**Qualification** (`neura-qualify`). A combination is Supported (§26.2) only after three things pass on the real robot.

1. **The hardware fault suite.** The site's gateway process and its real policy workers run on the robot computer behind `neura-chaos`'s TCP fault proxy. The arm is served by `neura-arm`'s own process code and watched by the independent invariant monitor (§17.7). Each scenario arms the robot, lets it run and injects one fault. It then checks the runtime's verdict (state, reason, whether the response was confirmed) and what the arm physically did, from its joint readings:
   - after a fault or during starvation the arm held still within tolerance (1 degree by default), measured from 0.3 s after the hold;
   - it received no command while faulted or starved;
   - a latched fault stayed latched until the reset;
   - the monitor saw no violation;
   - every command it ever received was inside the profile's ranges.

   The scenarios cover the §7 list where it applies to a robot:

   | Scenario | Covers |
   | --- | --- |
   | `nominal` | runs at the control rate, control loop on time; records parity samples |
   | `network_jitter` | latency within budget: keeps running |
   | `network_delay` | stale reply, network delay above budget |
   | `partition` | network partition, late (reordered) replies after it heals: starvation fault, still latched |
   | `reset_connections` | packet loss on the stream (TCP loses as a stall or a reset) |
   | `gateway_kill`, `gateway_freeze` | gateway restart, a stalled host |
   | `worker_kill` | worker crash mid-batch and restart, no replay |
   | `operator_takeover`, `second_client` | the same names in §7, with reply for an old lease generation |
   | `camera_frozen`, `camera_unplugged` | camera missing or too old |
   | `invalid_action`, `nan_action` | out-of-range and NaN actions with a valid digest |
   | `duplicate_chunk`, `old_epoch_chunk` | duplicate reply, reply for an old epoch |
   | `controller_failure`, `torque_loss`, `bus_unplugged` | controller command failure; the last two must not claim a stop |
   | `second_runtime_blocked`, `actuator_lock_lost` | one actuator authority (§6.3) |

   Oversized messages, GPU out-of-memory, a long control-plane outage and offline authorization expiry are not robot-side, and stay in the gateway, worker and agent suites. Recording does not exist yet.
2. **Open-loop parity.** During `nominal`, the harness keeps the raw readings behind some observations (joint positions and camera frames as captured) and the chunks the site returned. It then runs the checkpoint through LeRobot alone on the same readings: `prepare_observation_for_inference`, the checkpoint's processors and `predict_action_chunk`. None of Neura's code is on that path: not the tensor encoding, transport, batching, worker or action mapping. The largest difference must be within 0.001 in the model's action units. The reference requires the artifact's state in the robot's own units, as LeRobot training produces.
3. **Closed-loop non-inferiority.** Operator-scored episodes of the same task are served two ways: by Neura, and by LeRobot's own loop on the robot computer (`predict_action` each control period, as `lerobot-record --policy.path` does).
   - **Order.** After an unscored warm-up of both, episodes alternate in ABBA blocks, and the operator is not told which way each was served.
   - **Verdict.** The lower bound of the one-sided 95% interval for the difference in success rates (Newcombe's hybrid score interval) must be above minus the margin.
   - **Defaults.** 30 episodes each and a margin of 0.15: with both at 90%, the bound is about −0.14. Ten episodes each cannot show it even at 100%.

`neura-qualify promote` writes the docs site's Supported entry only from a complete hardware report (every scenario hardware can run, including the `--manual` one; `torque_loss` is induced only on the emulator), with parity and episodes for the same artifact and execution profile that passed. It refuses emulated reports, and reports judged by a looser protocol than the defaults (`qualify/protocol.py`: hold tolerance, parity tolerance, nominal duration, episodes per arm, margin, blinding); each report records what it was judged by.

**Hardware test protocol**

- **Setup:**
  - the arm calibrated with `lerobot-calibrate`;
  - `neura-arm check` passes;
  - the robot profile's command ranges inside the calibrated ranges (the integration refuses them otherwise);
  - `max_relative_target` at 10 degrees or less;
  - `on_exit: hold`;
  - the trained checkpoint that will be deployed, on the device it will be deployed on, with the execution profile being qualified.
- **Safety:** an operator stands at the arm's power switch for the whole run; the workspace is clear of people and fragile objects. The harness asks for confirmation before anything moves. For `bus_unplugged` (`--manual`), the operator unplugs and replugs the servo bus when asked.
- **Run:** `neura-qualify run ... --manual`, then `neura-qualify episodes ...`. Commit both reports under `qualification/reports/<arm>/`, then promote.

**Results on the emulated arm** (`qualification/reports/emulated/2026-10-15.json`; synthetic ACT on CPU, synthetic cameras): 21 of 21 scenarios passed in 125 s; parity 1.5e-5 over 10 chunks; 0 invariant violations over 1,899 dispatched actions, 63 accepted chunks, 12 latched faults and 11 resets. `make qualify-emulated` runs it in CI. The run on hardware is pending: it needs the arm.

**Found and fixed while building it**

- **Non-finite actions.** The runtime's range check passed NaN (every comparison with NaN is false). Decoding already refused non-finite tensors, so no NaN reached a controller. The check no longer relies on that: anything not inside the range is out of range.
- **Reset after an overload.** A tripped STS3215 answers every read with an overload error until its torque is re-enabled, so revalidating first refused every reset. Reset now restores goals and torque with sync writes, which have no status packet, and then revalidates.
- **Admission on a slow host.** On a CPU, the latency tail made admission predict an observation age of 104 ms against a 100 ms budget. It refused new sessions until the slow samples aged out of the 120 s horizon (§9.3, the M7 fix). That is correct, but it was invisible: the robot retried silently. Refusals now reach the operator with their reason. The emulated contracts use freshness budgets a CI runner's CPU meets; a hardware run qualifies its profile unchanged.

**Known limits**

- One arm type: SO-101-class arms on LeRobot's Feetech driver.
- Camera faults, controller failures and corrupted chunks are injected at the integration boundary, not by breaking hardware. A servo overload is induced only on the emulator.
- `TIOCEXCL` does not bind a privileged process; the lock file still does.
- After the servo bus is unplugged, `neura-arm` must be restarted: LeRobot keeps the port marked open.
- The robot's control loop runs on an asyncio event loop with the bus calls inline. The report states late ticks: 0 of 300 in the emulated nominal run. A tighter integration moves the controller to its own real-time thread (§9).

---

## 8. Timing without shared clocks

Machines never subtract each other's timestamps.

- The robot sends a **relative budget**: `budget = (queued_actions × control_period) − margin`, i.e. how long until it starves.
- The gateway converts it on receipt into a local deadline: `deadline = recv_time + budget − uplink_estimate − downlink_estimate`, where the link estimates are conservative upper percentiles from heartbeat RTT measurements on that session (using full p99 RTT for each direction is the safe default).
- The scheduler only uses gateway-local deadlines.
- The robot independently re-checks freshness on its own clock (§5.2). The server's deadline is an optimization; the robot's check is the authority.

Refill rule: the robot requests a new chunk when `queued_actions × control_period ≤ p99(round trip) + p99(scheduler wait at admitted load) + margin`. If the admitted load cannot keep this inequality, the session must be **rejected at admission** rather than accepted and starved later.

Two RTT figures, both measured on the robot clock over a sliding time window (10 s by default) and sent with every heartbeat (§11):

- **p99 RTT** for capacity planning (admission), so planning stays conservative while a recent spike is in the window. A session is admitted before its robot has measured anything, with the configured default (`default_rtt_ms`); each heartbeat then replaces its demand with the p99 it reports, so later admissions plan with the link it has. An admitted session is never evicted: on a link that turns out too slow, it keeps its place, and the robot's own freshness checks refuse what arrives too late. *Audit fix:* admission used the default for the session's life, and the reported p99 only reached the status page.
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

The implemented check (M2) models the worst case: every session requests at once, one non-preemptible batch of the other sessions is already running, and sessions are served in deadline order in batches of at most `max_batch`. For each session it requires:

1. wait before dispatch + uplink ≤ the profile's maximum observation age at dispatch;
2. completion + link ≤ the maximum observation age at acceptance;
3. completion + link ≤ the starvation budget;

plus average utilization ≤ `1 − reserve` and memory headroom.

*Evidence fix:* the batch already running holds only the other sessions' requests, at most `max_batch` of them. A robot has at most one request outstanding (§9.2), so its own previous request is never in flight when it asks again. The check used to size that batch from every session, the robot's own included. With one robot on a CPU, whose pass is slower than the 100 ms dispatch limit, it imagined the robot waiting behind itself and refused it: a robot that reconnected after a stall was refused although it had just been served (§10.1). Capacities of more than `max_batch` robots do not change; only those that ended at or below one batch can rise. On the CPU benchmark's recorded profile (`bench/reports/cpu-intel-xeon-processor/`, 106 ms a pass), the estimate goes from 0 robots to 1. A re-run on the same host at the fix gave 1 too, with every pass slower that day, so the recorded report keeps its measurements.

*M6 update:* the scheduler's batching window (§9.2) serves robots that ask together in one batch, so synchronized arrivals cost one batch's latency rather than a queue of single batches. The CPU benchmark (§12.1) also found that robots desynchronize on their own when a pass is slower than a control period.

*Implementation finding (M2), important for the GPU-sharing bet (§25):* with the example's illustrative 100 ms dispatch-freshness limit and 45 ms batches, condition 1 caps one worker at about one batch of sessions (8), well below its throughput limit. That is with the 20 ms default link allowance; planned with the links robots measure (§8), about 1 ms on a LAN, two batches (16) fit. Starvation budgets were never the binding constraint. Synchronized requests are realistic: robots that start together stay in phase. Two levers can raise safe capacity, and both belong in the M6 benchmarks:

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

**Step 0 on an NVIDIA L4** (October 17, 2026; `bench/reports/nvidia-l4/`, GPU benchmark (Modal) run 39013279255). Synthetic checkpoints with LeRobot's real architectures at base size, 640 x 480 cameras, bf16, through real worker processes. The simulator matched the real-time runs: ACT at 16 robots missed nothing in either; SmolVLA and π0 at 4 robots missed 624 in 1,000 real against 627 simulated.

| Case | One GPU per robot | One process per robot, shared GPU | Neura |
| --- | --- | --- | --- |
| ACT, same weights, phased | 1 | 96 (memory-bound at 128) | 32 (deadline + fairness) |
| ACT, same weights, synchronized | 1 | 96 | 24 |
| ACT, per-robot LoRA adapters | 1 | 96 | 24 (multi-adapter batching) |
| ACT, four full fine-tunes | 1 | 96 | 96 (co-located groups); 64 with a background job |
| ACT and Diffusion Policy | 0 | 1 | 1 |
| SmolVLA, π0 | 0 | 0 | 0 |

What it shows:

1. **Sharing pays, by an order of magnitude.** One L4 serves 24 to 96 ACT robots at the SLO instead of 1, about 0.03 GPU-hours per robot-hour.
2. **Batching is not why.** ACT's pass is overhead-bound on the L4: 36 ms for one observation, 20 ms each in a batch of 16, and four passes side by side cost only 10% more than one. A Neura worker runs one pass at a time, so it tops out near 32 robots. Separate processes overlap their passes, and so do Neura's co-located groups: four fine-tune workers served 96. The next lever is several workers (or CUDA streams) per batch group, batching within each.
3. **The one-process-per-robot figure is an upper bound.** Contention was measured up to four processes and extrapolated linearly beyond; real-time validation ran four. Without MPS, many CUDA contexts time-slice, so 96 is likely optimistic. Measuring contention at 16 to 64 processes is the next measurement. (Measured to 24 since, and the memory check found incomplete: VLAs on the L4, finding 1.)
4. **The VLA profile cannot be met on an L4, shared or not.** A SmolVLA or π0 pass takes 0.38 to 0.40 s for one observation (0.82 and 1.75 s for 16), past the profile's 150 ms acceptance limit, so even a dedicated L4 fails at one robot. Measuring VLA sharing needs a profile qualified for such passes (a later acceptance limit and more actions left at refill, as real-time chunking allows) or a faster GPU. Memory is where Neura should win for π0: 8.5 GB a process fits two per L4 one process each, against one shared 9 GB worker. (Measured since with such a profile: VLAs on the L4, below.)
5. **Admission is about six times too cautious on a GPU.** It estimates 4 ACT robots (acceptance freshness), against 24 measured with synchronized arrivals and 16 validated in real time. Its worst case has every robot ask at the same instant behind a full batch of the others, forever; real robots drift apart. A capacity estimate that low would make the console undersell the GPU.

**VLAs on the L4** (October 17, 2026; `bench/reports/nvidia-l4-vla/`, GPU benchmark (Modal) run 39049627963, `--preset vla`). The same models, with a profile a 0.4 s pass can meet: chunks of 50 actions, the next requested at 30 left (a second before the queue starves at 30 Hz), and an observation at most 600 ms old at dispatch and 1.5 s at acceptance. ACT's same-weights case ran again as the reference, its co-location now measured at 1 to 24 processes instead of extrapolated from 4. Real time ran 16 ACT robots per configuration, 4 robots on Neura for each VLA, and 2 processes for each VLA.

| Case | One GPU per robot | One process per robot, shared GPU | Neura, one worker |
| --- | --- | --- | --- |
| ACT, same weights, phased | 1 | 96 in simulation (24 measured to fit; finding 1) | 32 |
| ACT, same weights, synchronized | 1 | 24 | 24 (FIFO); 16 (deadline + fairness) |
| SmolVLA, phased or synchronized | 1 | 12 in simulation; 2 validated in real time | 1 in simulation; 4 held in real time, at the edge (finding 3) |
| π0, phased or synchronized | 1 | 2 (8.5 GB a process) | 2 |

What it shows:

1. **Measured contention cuts the synchronized baseline from 96 to 24, and the phased 96 is unproven.** Twenty-four ACT processes side by side each run 5 times slower than one alone. When every robot asks at once, every process runs at once, so one process per robot serves 24 synchronized robots. Phased, few run at the same moment, and simulation still reaches 96. That 96 also assumes each process holds only its peak allocation (198 MiB). In Modal's container the driver reported no memory per process, so both L4 reports left out every process's CUDA context and caches. Twenty-four processes did fit (the contention run); the true phased ceiling lies between 24 and 96. The benchmark now falls back to the device's memory in use, with the worker alone on it, less its use before the worker started. The next GPU run measures it. Neura's figures are unaffected: one process.
2. **One Neura worker cannot keep up with a VLA's robots.** A SmolVLA pass takes 0.40 s for one observation and 0.54 s for 8, and four SmolVLA processes side by side slow each other by only 4%: the L4 is mostly idle during a pass. Separate processes overlap their passes. A Neura worker runs one pass at a time, and with the gateway's 2 ms batching window it does not batch either. Robots arrive a few milliseconds apart, the first goes alone, the next waits a whole pass behind it, and they stay out of step (mean batch 1.01 at two robots). Waiting one 0.4 s pass and then running its own uses about 800 of a robot's 900 ms, so the tail of the pass time starves it.
3. **Four SmolVLA robots on one worker sit on a knife edge.** In real time the 4 robots settled into two pairs, every pass a batch of 2, and missed nothing in 306 requests. The simulation of the same run fell into batches of 1 and 3 and missed 491 in 1,000. Re-simulating from the report's measurements shows why. With passes 10% faster than the 10-second latency measurement (the real run's were about that), the simulation misses about 5 in 1,000; at 15% faster, none. A configuration that 10% of the pass time flips is not a capacity to sell: the honest figure is 1 to 4. For π0, where the device itself is the limit, real time and simulation agreed (245 and 263 misses in 1,000 at 4 robots).
4. **A batching window sized to the pass is the lever.** Re-simulated from the measurements, with synchronized arrivals and a 50 ms window instead of 2 ms (a 4% wait on a 0.4 s pass), one worker serves 12 SmolVLA robots and 4 π0 robots at the SLO, in batches of 12 and 4. π0 would be the first case where Neura beats one process per robot on this GPU: one copy of its 8.5 GB of weights, where separate processes fit only two. Phased arrivals gain less (46 misses in 1,000 at two SmolVLA robots, from 239): robots that request 170 to 330 ms apart do not fall into one window. For them the gateway would have to bring a batch group's robots into step, or the group would have to run several passes at once (step 0, finding 2). None of this is built: a window per batch group from its measured pass time comes first, and needs a real-time run before the console quotes it.
5. **Admission is right for π0 and ACT, not for SmolVLA.** It estimates 2 π0 robots (measured: 2) and 3 ACT robots (measured: 16 synchronized, so still cautious). For SmolVLA it estimates 3, above the simulation's 1 and below the 4 that real time held at the edge. It models robots served in deadline order in batches, the batching that a 2 ms window does not deliver to robots arriving apart.

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

### 10.1 Closed-loop evidence in simulation (`neura-evidence`, the Evidence workflow)

One published policy, served five ways, on the scene it was trained for:
- **The policy.** LeRobot's `act_aloha_sim_transfer_cube_human`, migrated to LeRobot's processor format by LeRobot's own tool. Every system loads the same migrated files.
- **The scene.** gym-aloha's transfer-cube scene, ported to plain MuJoCo (`neura_robots.aloha`). Every run checks the port against gym-aloha step for step: physics, rewards, contacts and camera frames identical.
- **The seeds.** Episode *i* uses seed 1000 + *i* for every system, so outcomes compare seed by seed. Every system gets one warm-up episode first, which is not scored.
- **The clock.** Except for `lerobot-eval`, the world advances every 20 ms of wall-clock time on its own thread, whatever the robot software is doing. It keeps moving while the policy computes, while a chunk is late and while the robot holds (`RealTimeWorld`). An episode is 400 world steps.

| System | What it is |
|---|---|
| `lerobot-eval` | LeRobot's own protocol, in process: the world waits while the policy computes. LeRobot publishes its success rates this way. The workflow runs LeRobot's `lerobot-eval` on gym-aloha too, as the reference |
| `lerobot-realtime` | In process, in the robot's 50 Hz loop (as `lerobot-record --policy.path` runs a policy): the robot stalls while the model computes |
| `neura` | The full Neura path: robot runtime, gRPC, site gateway, policy workers |
| `lerobot-async-scaled` | LeRobot's remote inference (`PolicyServer` and `RobotClient`, unmodified), its joint-similarity tolerance scaled to radians |
| `lerobot-async` | The same, as shipped. Its tolerance is one unit of joint distance: one degree on the SO-100 it was written for, one radian on ALOHA, so it skips nearly every observation unless the queue is empty |

The remote systems use matched settings:
- chunks of 100 actions;
- the next chunk requested with 30 left (Neura's refill point, LeRobot's `chunk_size_threshold` 0.3);
- the newest chunk replaces the old (LeRobot's `latest_only`).

They also run behind the same TCP fault proxy, so they get the same faults: added delay each way, a network stall, and the model process killed mid-episode.

**Fair to LeRobot's remote inference.** Its first runs scored 0 of 10. Three defects were in the harness, not in LeRobot:
1. **One renderer on two threads.** LeRobot's client draws the camera from its own thread. One MuJoCo renderer's EGL context on two threads failed with `EGL_BAD_ACCESS`. Each thread now has its own renderer, and the images are identical.
2. **A camera read that took 40 to 80 ms.** While its queue is low, LeRobot's client reads the camera every control tick. The simulated robot drew each frame inside that read, so the client's 50 Hz loop ran at 20 to 40 Hz. The policy's motion played out in slow motion against a world clock that kept real time. A real camera read costs almost nothing: LeRobot's cameras draw frames on a background thread, and `async_read` returns the newest. The simulated camera now does the same. A read waits for a fresh frame only when the newest is older than 200 ms (`async_read`'s own timeout) or the episode has just started.
3. **A cold model each episode.** The server loads the policy afresh for each client, and the first forward pass after loading took 1.1 s, against about 0.2 s warm. Neura's workers and the in-process baseline are warm when an episode starts. The async server now is too: one inference through LeRobot's own calls while the world is stopped, then `Ready` to reset its state for the client.

What remains is LeRobot's own design, recorded on every episode's line:
- **Control ticks.** Its loop sends each observation inline, as a blocking RPC that returns after the server has unpacked and checked it. About 900 KB each. The loop ticks 355 to 360 times in 400 steps on one 4-vCPU runner, and about 310 on a slower one.
- **Empty queue.** None with the scaled tolerance. A median of 19 ticks per episode as shipped, which waits for the queue to empty before the server takes an observation.
- **Observations sent.** 90 to 120 per episode, one per tick while the queue is low. The server skips most of them.

**Results: task success** (Evidence run 38900364537, 50 seeds per system, every system on the same runner: a 4-vCPU GitHub runner, CPU only).

| System | Succeeded | 95% CI | Median steps to success |
|---|---|---|---|
| `lerobot-eval` | 38/50 (76%) | 63–86% | 288 |
| `lerobot-realtime` | 36/50 (72%) | 58–83% | 328 |
| `lerobot-async` | 34/50 (68%) | 54–79% | 346 |
| `neura` | 33/50 (66%) | 52–78% | 322 |
| `lerobot-async-scaled` | 30/50 (60%) | 46–72% | 331 |

- **The intervals overlap.** On 50 seeds, no difference between the real-time systems is significant. Served by Neura, over gRPC, the gateway and a worker process, the policy does as well as LeRobot's in-process loop.
- **The world keeps moving.** Against `lerobot-eval`, where the world waits for the policy, real-time systems take 35 to 60 more steps to succeed: while a chunk is computed, the arm stalls or keeps moving on an older plan.
- **Seed outcomes move with timing.** `lerobot-eval` and `lerobot-realtime` agree on 46 of 50 seeds. Neura agrees with `lerobot-realtime` on 31. Neura asks for the next chunk with 30 actions left, and switches to it, computed from a newer observation, when it arrives. `lerobot-realtime` executes all 100, then stalls while the next is computed. The rates are close; which seeds succeed is not. 16 seeds succeeded for all four real-time systems, and 3 failed for all four.
- **LeRobot's async inference.**
  - With the scaled tolerance: 338 actions executed in 359 control ticks (medians), no empty ticks, the server at 356 ms a chunk.
  - As shipped: 317 actions in 356 ticks, 19 empty ticks, the server at 381 ms a chunk.
  - Every failure in every system ran the full 400 steps without finishing the hand-over, and every success reached the final reward.

**Results: the same faults** (Evidence run 38915350398, 10 seeds per cell, every cell on the same runner, which was slower than the closed-loop one: LeRobot's server took 410 to 490 ms a chunk there, against about 360).

| Scenario | `neura` | `lerobot-async-scaled` | `lerobot-async` |
|---|---|---|---|
| No fault | 9/10 | 2/10 | 4/10 |
| 100 ms added each way | 0/10, 1 faulted | 1/10 | 0/10 |
| 400 ms added each way | 0/10, 10 faulted | 0/10 | 0/10 |
| Network stalled 3 s, 2 s in | 0/10, 7 faulted | 0/10 | 0/10 |
| Model process killed 2 s in | 0/10, 10 faulted | 0/10 | 0/10 |

LeRobot's remote inference reported no fault in any scenario: it has no way to. What each system did, from the summary's medians:
- **No fault.**
  - Neura accepted 5 chunks an episode, each from an observation 342 ms old on arrival.
  - LeRobot's loop ticked 313 to 324 times in 400 steps and executed about 295 actions. That is too few to finish the hand-over on most seeds; the closed-loop runner executed 317 to 338.
- **100 ms added each way.**
  - Neura's evidence profile, tuned for a GPU, allows an observation to be at most 100 ms old when it reaches the model. With 100 ms added on the way in, the gateway expired nearly every request (`REQUEST_EXPIRED`). The robot stayed armed and did not move. It started no task, so it did not fault.
  - LeRobot's client blocks its control loop on every observation it sends, so each tick waited for a round trip. Its loop ticked 155 times in 400 steps as shipped, and 270 with the scaled tolerance. The arm moved in slow motion.
  - A profile with room for the network would leave margin for this delay (the site kit's CPU profile allows 600 ms at dispatch). This run did not measure one: in §20.12 the robots and the gateway share the site's server, and the impaired link is the site's uplink to Neura.
- **400 ms added each way.**
  - Neura faulted in every episode: in 7 its starvation grace ran out, and in 3 the robot was refused a new session (see the stall, below).
  - LeRobot kept executing chunks computed from observations 1.2 to 1.5 s old (medians; the oldest 1.9 s). Per episode, 22 actions with the scaled tolerance and 68 as shipped came from chunks older than 1.5 s, the age past which Neura refuses a chunk.
- **Network stalled.**
  - Neura faulted in 7 episodes: 4 from starvation, held after the grace period, and 3 refused a new session after the stall.
  - **Why it was refused** (reproduced on the development host, where the gateway said why). The stall outlasted the robot's lease, so the robot opened a new session. Admission refused it: `ADMISSION_CAPACITY_EXCEEDED: 1 sessions: observation age at dispatch 233637 us exceeds 100000 us`.
    - Admission assumes a batch is already running when a request arrives. With one robot, that batch can only be the robot's own previous request, which is never in flight at the same time.
    - By then the gateway had measured this CPU's inference time, and that one imagined batch put the predicted wait past the profile's 100 ms dispatch budget.
    - The same robot had been served one episode earlier, its observations 120 ms old on arrival. So episodes alternated: served, then refused on reconnecting, then served again.
    - The fix counts only the other robots' requests in that batch (§9.3, *Evidence fix*).
  - When the stall ended, LeRobot executed the chunk it had been waiting for, computed from an observation 3.3 s old: 16 actions with the scaled tolerance, 90 as shipped.
- **Model process killed.**
  - Neura faulted in every episode (`STARVATION_GRACE_EXHAUSTED`), 1 s after its queue ran out, the arm held.
  - LeRobot executed the 100 actions it had queued, then had nothing to execute for about 100 ticks. The restarted server had no policy loaded: the client sends its policy instructions only once, at start. Its log filled with `UNAVAILABLE` errors, and nothing told the robot or an operator.

From this run on, each Neura episode that never got a chunk says why (`no chunk accepted: REQUEST_EXPIRED x29`, say), and each summary gives the median count of every reason code the robot was given.

**Re-run after the admission fix** (Evidence run 39008312777, 10 seeds per cell). No robot was refused a session in any scenario. Before the fix, 3 were refused after the 400 ms delay and 3 after the stall. Every Neura fault was now its starvation grace running out, the arm held:

| Scenario | Neura | LeRobot async, scaled | LeRobot async |
| --- | --- | --- | --- |
| No fault | 5/10 | 1/10 | 0/10 |
| 100 ms each way | 0/10, 1 faulted | 0/10 | 0/10 |
| 400 ms each way | 0/10, 10 faulted | 0/10 | 0/10 |
| Network stalled 3 s | 0/10, 6 faulted | 0/10 | 0/10 |
| Model process killed | 0/10, 10 faulted | 0/10 | 0/10 |

This runner was slower again: LeRobot's server took about 730 ms a chunk (410 to 490 in the run above), and Neura's chunks were about 500 ms old when the robot accepted them (342). Every system succeeded less, LeRobot most. As before, LeRobot faulted nowhere, and it executed actions from chunks older than 1.5 s after the stall and the 400 ms delay (medians of 23 to 85 actions an episode).

**Not modeled.** The simulator's software renderer shares the CPU with LeRobot's server while the client reads frames. A real camera costs the server nothing. With the camera frozen (no drawing at all), a full-size model executes about 376 actions per 400 steps on the development host, against about 340. On a real deployment the server runs on its own machine, and the network carries about 45 MB/s while the queue is low.

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
- **Policy workers.** `neura-worker serve` runs one model on one device for one batch group and serves `neura.v1.PolicyWorker` (`Describe`, `Infer`) on a Unix socket (mode 0600, in a directory made for that run alone). The gateway uses it only once the socket answers `Describe` for the artifact the group serves. Its first stdout line announces ready or a typed load error. It watches its stdin from the moment it starts, and when the gateway closes it (or exits) a worker still loading exits at once, one serving after stopping its server, so a worker never outlives its gateway. Stopping, the gateway kills a worker that has not announced ready instead of waiting for it, and whoever stops a gateway (the site agent) waits as long as that can take (`neura_server.supervision`) before killing it. *Audit fix:* a worker read its stdin only once serving, the gateway waited 5 s for each worker, and the agent killed the gateway after 5 s: a gateway stopped while a model loaded died with its worker and worker directory left behind. The gateway's `WorkerBackend` supervises one worker per group and restarts it with backoff. Failures reach robots as typed rejections:
  - a worker that exits during a batch: `WORKER_CRASHED`;
  - a batch not finished by its latest deadline (and at least `worker_timeout_ms`): the worker is killed, `WORKER_TIMED_OUT`;
  - no worker ready: `WORKER_UNAVAILABLE`; a policy error: `WORKER_INFERENCE_FAILED`; a model that cannot load: `WORKER_LOAD_FAILED` or the specific reason.

  A result is delivered only from the worker incarnation the batch was sent to. Batch groups key on the concrete device (`artifact:cuda:0:bf16`); a reload stops the workers of groups no deployment uses, freeing their memory. Workers report their peak memory. The simulated backend (mock policy, profile-sampled latency) remains for the Compose stack and tests. Until M7, workers run in the agent's environment (`neura-agent-gpu` image: PyTorch and LeRobot); the pinned artifact image and sandbox above are M7.
- **Weights at the site** (§18.1). The agent keeps a content-addressed store (`<state>/artifacts`). Files arrive by `neura-agent import-weights DIR [--artifact FILE]` or from configured sources (`NEURA_AGENT_ARTIFACT_SOURCES`: directories or HTTPS base URLs laid out as `<source>/sha256/<hex>`, optional bearer token). Every file is verified against its digest before it is stored. A prepared artifact becomes a read-only checkout of hard links to verified blobs. Plaintext weights never pass through the control plane; sealed ones do only if the customer chooses Neura's registry (§17.7). Missing weights fail prepare with `AGENT_ARTIFACT_UNAVAILABLE`, naming the files and the fix.
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

  Reports go to `bench/reports/<device>/` as `results.json` and a generated `README.md`. The GPU workflow (`.github/workflows/bench.yml`) runs on a self-hosted runner labeled `gpu`. Without one, two workflows rent an NVIDIA L4 for the run and commit the report to the `bench-reports` branch: `bench-gcp.yml` on a Google Cloud Spot VM (`deploy/gcp/bench.sh`; a new project's GPU quota is 0, and Google may refuse it until the billing account has history), and `bench-modal.yml` at Modal (`deploy/modal/bench.py`), which needs no quota. Neither configuration uses `torch.compile` or CUDA graphs, so the comparison stays like for like (§10).
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
| 0 ✅ (L4, §9.5) | **Sharing economics benchmark** (runs alongside steps 1–3; needs one GPU, no robot): SmolVLA and π0-class models with synthetic observations; latency and memory vs batch size; robots per GPU at illustrative timing envelopes (§8); the four sharing cases in §9.5; dedicated baseline (§10) | A report stating robots per GPU shared vs dedicated for each case, and the GPU cost per robot-hour. This is the first evidence for or against the primary bet (§25) |
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
| **M6. Models and GPU sharing** (built; first GPU report: L4, §9.5) | ACT, SmolVLA and π0-class adapters (§12), batching, multi-adapter batching and co-location (§9.5), profiler and capacity estimates, benchmark harness (§10); policy worker processes, the site's artifact store, measured profiles at prepare; implementation notes in §12.1 | Benchmark reports for each sharing case vs the dedicated baseline, reproducible from one command on the GPU runner. Built and run on a CPU development host (`bench/reports/`); the GPU report is produced by `make bench` on the GPU runner |
| **M7. Production hardening** ✅ | mTLS everywhere, artifact signing, customer-managed keys, outbound ledger (§18), Grafana dashboards, installer and upgrades, chaos and soak tests, docs site (§26) | Security review checklist complete; 72-hour soak with injected faults and no invariant violations. Done: §17.8, and three 72-hour soaks with no violation (§17.7, `soak/reports/`) |
| **M8. Physical validation** (built; hardware run pending) | SO-101-class arms connected through the same platform: the SO-101 integration over LeRobot, a servo bus emulator, `neura-arm`, and `neura-qualify` (hardware fault suite, open-loop parity, closed-loop non-inferiority); implementation notes in §7.1 | Fault suite and parity pass on hardware. Built and passing on the emulated arm (`qualification/reports/`); the hardware report is produced by `neura-qualify` with the arm |
| **M9. Capture** ✅ | Episode recording on the robot off the action path, the site's capture service and episode store, labels, dataset snapshots, retention, LeRobot export, deployment capture settings within the site's boundary (§28.2) | A recorded task reaches the site's store, verified and labeled, while the arm is served as usual (emulated SO-101); episodes never leave the site |
| **M10. Evaluate** (built) | Closed-loop simulation suites, replay on captured episodes, shadow serving, release gates (§28.3) | A candidate's evidence is computed at the site and required before activation |
| **M11. Improve** (built) | Fine-tuning jobs at the site, signed artifacts with lineage (§28.4) | A captured dataset fine-tunes a model at the site into a new, signed, traceable artifact |
| **M12. The loop** (in progress) | API, console and the end-to-end run (§28.5) | Capture, fine-tune, evaluate and activate, end to end, on the simulated cell |

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

- The agent generates an Ed25519 key on the server and sends only the raw public key with the one-time token; the token is single use and expires. Each stream is authenticated with a short-lived EdDSA JWT (at most 5 minutes) that the agent signs with its key and the control plane verifies against the enrolled public key; M7 adds client certificates on top (§17.7). Deleting a server ends its stream and invalidates its credential.
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
3. Each robot asks which revision to run (the local site API in M4; the gateway, over mTLS, since M7) and switches at its own task boundary: it finishes the task, closes its session, verifies the new contracts and opens a session for the new revision.
4. The activation succeeds when every robot runs the new revision. The old revision is then removed and its remaining sessions closed.

A refusal (`AGENT_ACTIVATION_REFUSED`), a cancel from the control plane, or robots not switching within the activation window (`AGENT_ACTIVATION_TIMED_OUT`, 10 minutes by default) reverts the assignment.

An activation can be approved while an earlier one is still in progress, for example after a robot that never switched. Then each robot keeps the revision it actually runs: if it already runs the earlier activation's revision, that revision becomes its `current`. The gateway keeps serving it until the robot switches at its next task boundary. The earlier activation can no longer complete, so it ends at once, without reverting anything, with `AGENT_ACTIVATION_SUPERSEDED`. Approving the revision a robot already runs clears a revision still waiting in `next`.

An activation for more robots than the server can serve at the revision's execution profile fails at once with `ADMISSION_CAPACITY_EXCEEDED`. The limit is the capacity measured at prepare. The gateway's admission, which uses the same model, would refuse the extra robots a session, so the activation could only time out.

**Offline authorization**

- Offline authorization renews only on verified contact:
  - a newer signed generation, which can be committed once and so cannot be replayed;
  - a **contact proof**: the agent sends a random nonce with each status report, and the control plane returns the manifest key's signature over `CONTACT_PROOF_CONTEXT ‖ site_id ‖ 0x00 ‖ nonce`.

  Neither a replayed message nor a stream left open without the control plane behind it can extend authorization, and no clocks are compared across machines.
- When authorization runs out, the agent stops the data plane and refuses activations until contact returns. It is measured on the server's wall clock so that it survives restarts. Since M7 it is also bounded by monotonic time persisted across restarts, so a clock moved backwards no longer extends it (§17.7).

**Data plane and local API**

- The agent turns committed state into the gateway configuration and writes it only when it changed. The gateway runs as a supervised subprocess (restarted with backoff) and reloads that configuration on `SIGHUP` without a restart.
- The local site API (§19.5) and the `neura-agent` CLI provide:
  - status;
  - approvals, with approve and refuse;
  - robot credentials;
  - robot assignments;
  - the audit log.
- Mutations require the local operator credential; only its SHA-256 is configured on the server, and without it local approvals are disabled. Robots authenticated with device tokens until M7 replaced them with certificates (§17.7).

**Observed state in the control plane**

- A site's `connected` is derived from the age of its last report (90 s), so a control-plane process that crashes while serving a stream cannot leave a site shown connected.
- Periodic status refreshes `observed_at` but changes the resource version and emits an event only when a fact changes.
- Every database operation in the site endpoint runs shielded from RPC cancellation, and shutdown waits for streams and their writes before closing the database. A transaction abandoned midway would leak its connection.

**Known limits, scheduled**

- Robot device tokens and several streams per server were replaced in M7 (§17.7).
- The `connected` stream count is per process; with several control-plane replicas, liveness relies on report age.

### 17.7 Implementation notes (M7)

**Mutual TLS between robots and the gateway**

- The agent creates the **site CA** (ECDSA P-256) on first use and keeps its key on the server (`<state>/pki/ca.key`, mode 0600). Robot traffic never leaves the site, so neither does its trust root.
- The site CA issues the gateway's server certificate for the names robots dial (`NEURA_AGENT_GATEWAY_TLS_NAMES`; by default the hostname, `localhost` and `127.0.0.1`). It is renewed once a third of its 90-day lifetime remains, and the gateway picks up new certificate files without a restart.
- A robot makes its own key (`neura-robot keygen NAME`), and the site operator signs its CSR (`neura-agent robot-certificate NAME --csr FILE`, or `POST /v1/local/robots/{name}/certificate`). The CA sets the identity `neura:robot:<site_id>/<name>` itself; it takes nothing from the CSR except the public key. Robot certificates last 365 days.
- The gateway serves a robot only if its client certificate:
  - was issued by the site CA and is valid;
  - has a fingerprint still allowed for that robot;
  - names that robot and this site.
- Revoking (`neura-agent robot-revoke NAME`) removes the robot's fingerprints; the gateway's next reload closes its sessions with `AUTH_DEVICE_REVOKED`.
- Robots ask the gateway which revision to run (`GetAssignment`) over the same mTLS channel. The local site API is now for operators only.
- Device tokens remain only for a gateway run without TLS (`NEURA_AGENT_GATEWAY_TLS=false`, development).

**Mutual TLS between the agent and the control plane**

- The control plane's **agent CA** signs the agent's CSR at enrollment with the identity `neura:server:<server_id>` and records its fingerprint. When the control plane terminates TLS, the stream listener requires that certificate as well as the short-lived JWT. A certificate of another server, or of a deleted one, is refused.
- Enrollment has its own listener (`NEURA_CONTROL_AGENT_ENROLL_LISTEN`, server-only TLS), because an agent has no certificate before it enrolls. That listener serves only `Enroll`. The install command points at it.
- The agent renews its certificate over the authenticated stream once a third of its lifetime remains (30 days by default, `NEURA_CONTROL_AGENT_CERTIFICATE_DAYS`). The control plane keeps every unexpired fingerprint, so the old certificate works until the new one is installed.
- **One stream per server.** A newer authenticated stream for a server closes its previous one.
- Staging and production refuse to start without:
  - the agent CA files (`NEURA_CONTROL_AGENT_CLIENT_CA_CERT_FILE`, `..._KEY_FILE`);
  - a database URL with `sslmode=verify-full`, unless `NEURA_CONTROL_DATABASE_TLS_LOCAL` declares that the database is reached over a local socket or sidecar.
- In development, without CA files, the control plane creates an ephemeral agent CA.
- The dev stack writes robot bundles (key and certificate) into a shared volume (`NEURA_AGENT_DEV_ROBOT_CERTIFICATE_DIR`). Real robots never receive a key.

**Publisher signatures on model artifacts**

- An artifact's digest covers its whole canonical document: every weight file's digest and size, the runtime image pinned by digest, the adapter and its versions. A **publisher** (the customer's release process) signs that digest with an Ed25519 key:
  - `neura-contracts publisher-key --out KEY` creates the key (PEM, mode 0600) and prints its public key;
  - `neura-contracts sign ARTIFACT --key KEY` prints the signature;
  - `neura-contracts verify` checks one against trusted keys.
- The API takes signatures at registration or later (`POST /v1/model-artifacts/{id}/signatures`). It verifies each against the digest and keeps one per key. The document and its digest never change. The site manifest carries the signatures with the artifact.
- **The site runs only what a publisher it trusts signed.** Before anything of an artifact is pulled or run, prepare needs a valid signature by a trusted key. Otherwise it fails with:
  - `AGENT_ARTIFACT_UNTRUSTED` when no signature is from a trusted key;
  - `AGENT_ARTIFACT_SIGNATURE_INVALID` when a trusted key's signature does not verify.
- Trust is a boundary of the server (§17.4), not of the cloud. Keys come from `NEURA_AGENT_TRUSTED_PUBLISHER_KEYS` and from a trust file (`<state>/trusted-publishers` by default), which the site operator edits with `neura-agent trust-publisher`, `distrust-publisher` and `publishers`. The agent reads them on every prepare, so a change needs no restart.
- Every decision is in the local audit log (`artifact.verified` with the publisher, `artifact.refused` with the reason). A compromised control plane can neither make a site run an unsigned artifact nor forge a signature: the private key never leaves the customer's release process.
- Because the runtime image is pinned by digest inside the signed document, the publisher's signature also covers which image runs.
- `NEURA_AGENT_REQUIRE_ARTIFACT_SIGNATURES=false` turns the check off, for development only.
- The console shows each artifact's publishers and accepts signatures when registering or later.

**Customer-managed keys: sealed weights**

- `neura-contracts seal DIR --out OUT --key-file KEK` (or `--key-command CMD --key-id ID`) encrypts each file of a checkpoint into `OUT/sha256/<plaintext hex>`, the layout artifact sources use, and prints the artifact's `weights` list.
- The format (`neura_contracts.sealing`):
  - each file gets a fresh 256-bit data key, wrapped by the customer's key-encryption key;
  - AES-256-GCM in 1 MiB chunks, each chunk bound to the header, its position and whether it is last, so tampering, truncation and reordering all fail authentication;
  - the plaintext digest is checked again after decryption.
- **Key providers** on the server (`NEURA_AGENT_ARTIFACT_KEYS`):
  - a key file (`{"file": PATH}`, AES key wrap);
  - a command (`{"key_id": ID, "command": [...]}`, run as `<command> wrap|unwrap` with the key on stdin). This is the bridge to AWS KMS, Google Cloud KMS, Azure Key Vault, Vault transit or an HSM, without the agent holding cloud credentials of its own.
- Revoking or disabling the key in the customer's KMS stops every further unseal (`AGENT_ARTIFACT_KEY_UNAVAILABLE`).
- Any source may hold sealed files. The agent recognizes them, opens them with the matching key and verifies them like any other file. A file that fails authentication is refused with `AGENT_ARTIFACT_DIGEST_MISMATCH`, and nothing partial is kept.
- Opened weights are kept at rest in the site's own store, on the customer's server. Encrypted local storage is recommended.

**Neura's registry (ciphertext only)**

- With `NEURA_CONTROL_SEALED_WEIGHTS_DIR` set, the control plane keeps weights for customers who choose to:
  - `PUT /v1/projects/{project_id}/sealed-weights/{digest}` stores a sealed file (SDK: `upload_sealed_weights`);
  - list, get and delete endpoints manage them.
- It accepts only a well-formed sealed file for the digest in the path: magic, header bounds, the header's plaintext digest, and the exact length the header implies. Uploads are staged, and a file changes only after its record is committed: a second transaction moves an upload into place if the record is still that upload's, and removes a file whose record was deleted, under a lock per digest. A change that fails before its commit leaves the file as its record says, and a concurrent upload or delete of the same digest never leaves them disagreeing. Opening the registry clears staged uploads untouched for an hour (`test_sealed_weights.py`). The control plane holds no key and cannot open what it stores.
- Agents list the source `neura` in `NEURA_AGENT_ARTIFACT_SOURCES`. They fetch with `SiteControl.FetchSealedWeights` over their authenticated channel (the short-lived token and, with TLS, the client certificate), scoped to their project. The agent refuses anything unsealed from the registry.

**What leaves the site: the outbound schema and ledger**

- `catalog/outbound.yaml` is the published, versioned allowlist of everything a site agent may send: every field of the stream, of enrollment and of registry fetches, the keys of operation results, and the telemetry metric catalog. The agent filters every outgoing message against it, so a field missing from the schema is never sent. A test fails when the protocol gains a field the schema does not classify.
- **Telemetry modes** (`NEURA_AGENT_TELEMETRY_MODE`):
  - **standard** sends everything listed. That includes the agent's own human-readable details (failure messages, preflight findings), which can name paths or the customer's registry URLs.
  - **minimal** sends only what Neura needs to deploy and operate the site: health, versions, identifiers, states and reason codes. Free text, host names, GPU UUIDs, operator names, session metrics, telemetry batches and capacity-estimate inputs stay at the site. Capacity estimates arrive without their `assumptions`.
  - The whole journey (enroll, prepare, local approval, task-boundary switch, outage) is tested in both modes.
- **The outbound ledger** (`<state>/outbound`) records every message as it is sent: when, on which channel, its kind, its size and SHA-256 as serialized, its content as JSON, and which fields the mode withheld.
  - Entries are hash-chained: each stores the previous entry's chain and its own, so an edited, removed or reordered entry is detected.
  - Files rotate by size (`NEURA_AGENT_OUTBOUND_LEDGER_MAX_MIB`, 256 MiB by default).
  - The one-time enrollment token is kept as its digest only.
  - Read it with `neura-agent outbound [--kind K] [--summary] [--verify]`, or `GET /v1/local/outbound` with the operator credential.

**Monitoring with the customer's own systems**

- Every component serves Prometheus metrics for the customer's monitoring; none of them go to Neura:
  - the gateway on its status listener (`/metrics`: sessions, per-robot chunks, round trip, rejections and faults, scheduler queues, deadline misses, drops, batch sizes, inference latency, worker health);
  - the agent on its local API (`/metrics`: connection, offline authorization, desired state, approvals, operations, artifact checks, what was sent and withheld, certificate expiry);
  - the control plane on a listener of its own (`NEURA_CONTROL_METRICS_LISTEN`), apart from the public API. It reports requests by route template (never by identifier), latency, agent streams, enrollments, site messages, certificates issued and sealed bytes served.
- `deploy/observability` holds:
  - a Prometheus configuration;
  - alert rules on those metrics (deadline misses, faults, worker restarts, disconnection, offline authorization, certificate expiry, unpublished outbound fields, API errors);
  - two Grafana dashboards, generated by `tools/gen_dashboards.py`.
- The dev stack runs them with `docker compose --profile observability up` (Grafana on port 3001). Tests check that every dashboard query and alert uses only metrics the components export.

**The worker sandbox**

- Policy workers run in the artifact's runtime image, pinned by digest (`NEURA_AGENT_WORKER_SANDBOX=container`, the default). Customer model code is treated as untrusted (§12).
  - **Network:** none. The scheduler reaches the worker over a Unix socket in a mounted directory.
  - **Filesystem:** a read-only root with a small tmpfs. Contract documents and the weight checkout are mounted read-only. The one writable mount is a directory made for that run alone (mode 0700, removed with it): a serving worker's socket, a job's output. Every container runs as the same user, so no two runs share a writable directory: a worker that could reach another's socket could answer for another model's robots. *Audit fix:* runs used to mount the parent of their output, which was the gateway's one worker directory for every socket and the agent's directory of every revision's profiles.
  - **Privileges:** no capabilities, no privilege escalation, and the gateway's own unprivileged user.
  - **Limits:** memory (`NEURA_AGENT_WORKER_MEMORY_MIB`), CPU (`..._CPUS`) and processes (`..._PIDS_LIMIT`).
  - **Devices:** only the GPU it was placed on (`--gpus device=N`; podman takes CDI arguments instead).
- **Pull, verify, then run.** Prepare pulls the image by digest (`WORKER_IMAGE_UNAVAILABLE` if it cannot). Workers start with `--pull never`, so nothing is fetched at serve time. The image's digest is inside the publisher-signed artifact, so the signature covers which image runs.
- Profiling at prepare runs in the same sandbox and writes nothing: it prints the profile, and the agent keeps it with the other revisions' profiles.
- A worker container whose supervisor was killed (a hung worker) is removed by name. The gateway removes leftover worker containers when it starts.
- `deploy/docker/Dockerfile` builds two worker runtime images:
  - `neura-worker`, the framework with the mock adapter;
  - `neura-worker-gpu`, with PyTorch and LeRobot. Publishers extend it and pin its digest.
- Tests run a containerized worker against Docker in CI. They:
  - serve robots;
  - check the container's settings;
  - kill the container and check it is replaced;
  - check cleanup;
  - from inside the container, check that there is no network, no writable root and no root user.
- `process` mode runs workers as plain child processes, for development and tests only.

**Installation and signed upgrades**

- **Releases.** `tools/build_agent_release.py` builds a release bundle: the agent's wheels and its dependencies, the launcher, and `release.json` listing every file's SHA-256. It signs `release.json` with Neura's Ed25519 release key and prints the bundle's SHA-256.
- **Installer.** `deploy/install/install.sh` verifies a bundle before installing anything: the signature with `openssl` against the release keys given, then every file against its signed digest. It then:
  - installs the release in its own virtual environment (`/opt/neura/agent/releases/<version>`), with `current` pointing at it;
  - pins the release keys (`/etc/neura/release-keys.pem`);
  - writes `/etc/neura/agent.env` and a hardened systemd unit (dedicated user, `NoNewPrivileges`, `ProtectSystem=strict`, writes only to the state and install directories);
  - enrolls the server and starts the agent.

  Running it again with a newer bundle is a manual upgrade. The install and state directories belong to the agent's user, which stages its own upgrades there, so the installer does everything inside them as that user: root never runs code from them or writes through them, and its own Python runs isolated (`-I`) from the directory it is started in. A test installs as root in a container and checks every Python process it starts.
- **Remote upgrades** are opt-in at the site: `upgrade_agent` is not among the default allowed operations. `POST /v1/servers/{id}/agent-upgrades` (admin role) sends the server a version, an https URL and the bundle's SHA-256. The agent then:
  1. fetches the bundle and checks its SHA-256;
  2. verifies it against the pinned release keys, so the control plane cannot push code Neura did not sign;
  3. stages it in a new virtual environment;
  4. waits until no robot is armed, running or waiting (up to `NEURA_AGENT_UPGRADE_WAIT_S`, else `AGENT_UPGRADE_DEFERRED`);
  5. points `current` at the new release, records the upgrade and restarts.
- **Automatic rollback.** systemd starts the agent through `neura-agent-launch`, a standard-library script outside every release.
  - It counts the new release's start attempts. After three failed starts it points `current` back at the old release.
  - The new agent commits the upgrade once it has reconnected to the control plane. If it does not within `NEURA_AGENT_UPGRADE_HEALTH_TIMEOUT_S`, it rolls itself back.
  - The old agent then reports `AGENT_UPGRADE_ROLLED_BACK` with the reason.
  - Tests build real bundles, install them with the installer (openssl, venv, pip), and exercise commit, both rollbacks and every refusal: wrong digest, version or key, tampered file, not installed.
- **Offline authorization and the clock.** Time since the last verified contact is the larger of the wall clock's measure and the time the agent itself has counted with the monotonic clock, persisted across restarts. Setting the server's clock back no longer extends offline authorization.

**Chaos and soak testing**

- **An independent invariant monitor** (`neura_runtime.invariants`) watches a site from outside. It sees:
  - what each robot sent and received;
  - what the gateway emitted and which leases it granted;
  - every command each controller was given.

  It keeps its own model of what each robot was allowed to accept and execute, and never asks the runtime whether a decision was right. It records a violation, the class of failure §21 calls Sev-1, when:
  - a chunk is accepted that is not for the robot's own session, lease generation and task epoch;
  - a chunk is accepted that answers an older request than the latest, or is accepted twice;
  - a chunk is accepted whose observation was too old, or taken before the robot started waiting;
  - a chunk is accepted with the wrong anchor, digest or revision, or while the robot was inactive;
  - a chunk was granted to another client, or not emitted by the gateway under a live lease;
  - an action is dispatched out of order or out of range, while not running, or while a fault is latched;
  - an action is dispatched that no accepted chunk produced (for replace merges, its exact value; for blend, within the merged rows);
  - a latched fault clears without an explicit local reset, or its record changes;
  - lease generations do not strictly increase per robot across gateway restarts;
  - the gateway emits a chunk without a live lease or for an old generation.

  The fault suite asserts the monitor on every scenario. Mutation tests break one rule each (a runtime that accepts duplicates, a reconnect that clears a fault, a skipped or altered action, no freshness limit, an expired or never-granted lease) and require the monitor to report it.
- **The soak** (`neura-sim-soak`, `make soak`) runs the simulated site for 72 simulated hours: the production sans-IO cores in virtual time, about 40 times faster than real time.
  - A seeded campaign injects a fault about once a minute, at random times and on random robots. It draws from the §7 fault suite: delay spikes, short partitions and long outages, stream loss, duplication and reordering, site-wide degradation, worker crashes (short and longer than the starvation grace), out-of-memory, invalid and out-of-range output, gateway restarts and host freezes, missing and skewed cameras, unavailable sensors, operator takeovers, a second client with the same credential, controller failures, lost actuator authority, oversized messages and revoked credentials.
  - An operator stand-in finishes tasks, hands robots back after a takeover and resets a latched fault after 2 to 30 s.
  - The soak tracks availability and resources every simulated hour: gateway sessions, leases, admitted sessions, queued requests, the event queue and memory. It requires every robot to be running after a fault-free cooldown.
  - The same seed reproduces the same run, and violations carry their simulated time.
- **Chaos against real processes** (`neura-chaos`, `make chaos`).
  - It starts a gateway process with its policy worker processes behind a TCP fault proxy, with a supervisor that restarts the gateway as systemd would.
  - It serves simulated robots over gRPC, each with its own connection and each watched by the monitor. Against a real gateway only the robot side is visible; grants are taken from what robots receive.
  - Faults: kill or hang a worker; kill, terminate, freeze (SIGSTOP the process group) or reload the gateway; stall every connection (TCP then delivers the backlog late); drop every connection; add latency; operator takeover; a second process with the same credential.
- **Where they run.**
  - Every CI run includes the fault suite with the monitor, the mutation tests, a five-minute soak with a fault every two seconds, and 45 s of real-process chaos.
  - The `Soak and chaos` workflow runs weekly and on demand: the 72-hour soak for ACT with replace merge, SmolVLA with replace merge and ACT with blend merge, plus 30 minutes of chaos.
- **The 72-hour result (M7 exit criterion).** Three soaks of 72 simulated hours each, on the code at `30ac115`. Since then nothing the simulator runs has changed except the generated list of reason codes. Reports and the commands to reproduce them are in `soak/reports/`.

  | Run | Faults (22 kinds) | Latched faults reset | Gateway restarts | Chunks accepted | Actions checked | Violations | Availability |
  | --- | --- | --- | --- | --- | --- | --- | --- |
  | ACT, replace merge | 4,339 | 1,350 | 102 | 1,006,921 | 30,226,689 | 0 | 96.8–97.4% |
  | SmolVLA, replace merge | 4,425 | 1,479 | 114 | 2,006,850 | 30,150,473 | 0 | 96.7–97.1% |
  | ACT, blend merge | 4,264 | 1,402 | 106 | 1,005,181 | 30,175,256 | 0 | 96.9–97.1% |

  - Every robot was running after the fault-free cooldown, and no resource check fired.
  - Gateway sessions, leases and admitted sessions stayed at the number of robots, and the event queue stayed under 20.
  - Peak memory levelled off at 130–142 MiB within the first hours.
  - The worst single hour for any robot still dispatched 92.6% of its control periods.
  - Availability counts the time robots spent faulted and waiting for the operator stand-in. A fault arrived about once a minute, far above what a site sees.
  - Real-process chaos ran for 3 minutes with 28 faults, among them 3 gateway kills, 2 terminations, 2 freezes and 3 reloads: no violation, every robot recovered, and lease generations kept increasing across 6 gateway starts.
- **What they found, now fixed.**
  - *A frozen gateway host locked the site out.* The latency profile adapts upward from observed batch times and, with few samples, used the worst one. A batch in flight while the host was frozen recorded a 3 s latency, and admission then refused every session. No session meant no new batch of that size, so the estimate never came back down.

    Now a single outlier never raises a prediction (the observed tail is the latency at most 1% of recent samples exceed, and never fewer than one), and observations expire after two minutes. A repeated slowdown still raises predictions. The simulator now models host freezes, with a regression test.
  - *A chunk could be emitted under an expired lease* between two gateway sweeps. The robot rejected it, since its own lease view had already run out, but the gateway now checks the lease is live when it emits.
  - *A late rejection addressed to an earlier session could tear down the current one.* A robot now ignores rejections for any session but its own.
  - The simulator's own controller log and route table grew without bound; both are bounded, so a soak runs for days in under 100 MiB.

**Supply chain**

- **Pinned inputs.**
  - Python and JavaScript dependencies are locked (`uv.lock`, `pnpm-lock.yaml`) and installed frozen.
  - Base images are pinned by digest in `deploy/docker/Dockerfile`, and so is every other image production runs (the edge, the database, certbot, the Cloud SQL proxy), in the compose files under `deploy/gcp`: none is pinned in a script, where no updater would see it.
  - Every GitHub Action is pinned to a commit SHA, with its version in a comment.
  - Dependabot proposes updates for all of them weekly, through CI (`.github/dependabot.yml`: the Dockerfile, the compose files of `deploy/gcp` and of the dev stack).
- **Dependency audit** (`tools/audit_dependencies.py`, the `supply-chain` CI job). It checks every package in `uv.lock` (all groups, PyTorch and LeRobot included) with `pip-audit`, and the console's and docs site's lockfiles with `pnpm audit`. A finding fails CI unless it is fixed or listed in `security/vulnerability-exceptions.yaml` with the reason it does not affect Neura and an expiry date; an expired entry fails too.
  - The first run found six advisories in the docs site's build toolchain (fixed by overriding to patched versions) and one in the console's `yaml` (upgraded).
  - Three are accepted until 2027-01-15, each because the latest compatible upstream release cannot take the fix:
    - `setuptools`: an sdist-building issue; Neura does not build sdists.
    - `torch`: TorchScript memory corruption. Neura never calls TorchScript, and PyTorch runs only inside the worker sandbox.
    - `braces`: in the docs build only, on our own glob patterns; no fixed version exists.
- **Releases** (`.github/workflows/release.yml`, on a `vX.Y.Z` tag, after all of CI passes on it):
  - Every image is pushed to GHCR and deployed by digest.
  - Each image is signed keyless with Sigstore (cosign, bound to the release workflow's GitHub identity), with a CycloneDX SBOM as a signed attestation and SLSA build provenance.
  - The site-agent bundle is signed with Neura's Ed25519 release key (a repository secret). Sites pin that key at install and verify every upgrade against it. The tag must equal the packages' version.
  - The release notes list every digest and the `cosign verify` and `gh attestation verify` commands.

**Known limits, scheduled**

- Signatures are checked at prepare. A revision prepared before its publisher was distrusted keeps running until it is replaced or prepared again.
- The registry stores files in a directory: local disk, or a shared filesystem when several control-plane replicas run. An object-storage backend is not built yet.
- Traces (OpenTelemetry) are not exported yet; metrics and the local audit and outbound logs are.
- An upgrade restarts the gateway with the agent, so robots reconnect after the switch; the switch waits for a task boundary instead. The console has no upgrade screen yet (the API and SDK have the operation).
- The container sandbox needs the agent on the host (the installer's systemd service). The bind-mounted paths are the agent's own, so an agent inside a container would need its state directory at the same path on the host. A seccomp or AppArmor profile beyond the runtime's default is not shipped yet.
- The control plane does not check incoming messages against the outbound schema; it relies on the agent's filter.
- Real-process chaos covers the gateway, its workers and the network. The site agent and control plane are covered by the smoke test's control-plane outage and their own fault tests; a chaos run that restarts them under load on the Compose stack is not built yet.
- Against real processes the monitor sees only the robot side: grants and generations as robots receive them, not the gateway's emissions.
- The soak's 72 hours are simulated time on the production cores; wall-clock aging (certificate expiry, clock drift, disk growth over days) is covered by unit tests, not by a 72-hour run of real processes.

### 17.8 Security review (M7)

The review covered every component against the controls below.

**Method.**

1. An automated reviewer audited the code for each control and cited the files and tests that implement it.
2. Each gap it found was fixed or decided here.
3. Each fix landed with a regression test.

**Status words.**

- **Implemented**: present before the review.
- **Fixed**: changed in this review.
- **Accepted**: a residual risk kept on purpose, with the reason given.
- **Scheduled**: owned work after M7.

An external penetration test is scheduled before the first customer pilot. This review does not replace it.

| Area | In place (evidence) | Fixed in this review | Residual risk and decision |
| --- | --- | --- | --- |
| **Action safety** (§1 goal 1) | Robot runtime fencing, freshness, latched faults; fault suite; independent invariant monitor; 72-hour soak; real-process chaos (§17.7) | Gateway serves a chunk only under a live lease; robots ignore late rejections for an earlier session; a frozen host can no longer lock admission out | Real-process chaos does not yet restart the agent or control plane under load: **scheduled** |
| **API authentication** | OIDC: signature, issuer, audience, expiry, asymmetric algorithms only. API tokens: SHA-256 at rest, scoped, expiring in 1–365 days, constant-time compare (`test_api_conventions`) | Optional authorized-party (`azp`) and `amr` checks. Verification off the event loop. Tests for another issuer, a forged signature, HMAC and unsigned tokens | — |
| **Secrets at rest** | Token hashes; keys 0600 (agent, TLS, site CA, gateway, KEK, robot); operator token hash only; ledger redacts the enrollment token | Idempotency records held new API and enrollment token secrets in plaintext for 24 h. They are now sealed with a key derived from the client's Idempotency-Key, stored by hash, and old rows were purged (migration 0006, `test_security`). The agent's audit log is 0600 | The enrollment token is passed on the installer's command line: it is one-time, expires within an hour, and is visible only to local users while install runs. **Accepted.** Key-file permission checks at control-plane startup: **scheduled** |
| **Authorization and tenancy** | Roles and token scopes on every route; 404 for invisible resources; deploying and approving motion are separate, and activation is approved only at the site | A token limited to some projects could administer the organization and mint an organization-wide admin token. It can now only read there. A site's report can update only its own robots' sessions. A site's acknowledgement of an activation or rollback switched whatever revision and robots it named, even another organization's; it now switches only the revision its activation request asked for, and only that request's robots, and the writer refuses a revision or robots outside the operation's site (`test_a_site_cannot_switch_robots_it_was_not_asked_to`). Tested with two real organizations | No row-level security in Postgres; isolation is enforced in queries and tested. **Accepted** |
| **Abuse limits** | Pagination bounds; metric query caps; event streams end after 5 min; one agent stream per server; gateway admission | Rate limits per credential and per client address, with LRU eviction (no reset by key flooding). Forwarded addresses trusted only from configured proxies. 413 before a body is buffered. Caps on requests in progress, event streams, agent and gateway RPCs. Small messages on the enrollment listener | Limits are per process; with several replicas, add them at the ingress. **Accepted** for M7. Sealed-weight storage quotas per project: **scheduled** |
| **Input validation** | Request models forbid unknown fields; merge patches re-validated; ORM only (no SQL built from strings); observation decoding checks names, shapes, finiteness and sizes | — | — |
| **Errors** | RFC 9457 problems with catalog codes; no tracebacks to clients (`test_unexpected_errors_do_not_leak_details`) | — | `/v1/meta` is public by design: the console needs the sign-in configuration and versions. **Accepted** |
| **Audit** | Records written in the same transaction as each change; site-local audit log | Audit records are append-only: a database trigger refuses UPDATE and DELETE (`test_security`) | Organization-level records cannot be listed through the API; denials are not recorded; local approvals are not mirrored to the cloud audit. **Scheduled** |
| **Site agent channel** | Single-use enrollment tokens; key generated on the server; EdDSA stream tokens valid at most 5 min; mTLS with certificate pinning and renewal; signed desired state with increasing generations; signed contact proofs | Insecure settings require `NEURA_AGENT_DEVELOPMENT=true`: plaintext, robots without TLS, the simulated policy, workers outside the sandbox, unsigned artifacts, development credentials, plain-http weight sources | Commands are authenticated by the channel, not signed one by one. Activation still needs local approval, and upgrades are signed. **Accepted.** Rotating the manifest key without re-enrolling: **scheduled** |
| **Local approvals** | Operator credential compared by SHA-256 in constant time; approvals disabled without it | Tests for missing, wrong and non-bearer credentials | One shared credential per site; no lockout; the local API is plain HTTP, on loopback by default. Per-operator credentials: **scheduled** |
| **Gateway** | Robot mTLS with fingerprint pinning and robot/site identity; tokens only without TLS; 16 MiB messages; strict observation decoding | RPC concurrency cap; robots without TLS only in development | The status listener is unauthenticated, on loopback by default. **Accepted** |
| **Worker sandbox** | No network; read-only root; no capabilities; no new privileges; pids limit; image pinned by digest, never pulled at run time; non-root user; its GPU only | Workers outside the sandbox only in development | Memory and CPU limits are off by default, because model needs vary; operators set them. **Accepted.** The agent's Docker group is root-equivalent on the host; rootless Podman is recommended. **Accepted.** Custom seccomp and AppArmor profiles: **scheduled** |
| **Console** | Same-origin API; CSP without inline scripts; frame-ancestors none; tokens in session storage; bearer tokens only, so no CSRF surface; no CORS | Headers on every response (nginx dropped some per location), plus HSTS, COOP and CORP. nginx runs as non-root. Large uploads stream through the proxy. Verified on the built image | Inline styles are allowed in the CSP. **Scheduled** |
| **TLS** | Staging and production require TLS for agents and Postgres (`verify-full`, now parsed properly); automatic renewal of agent and gateway certificates | `sslmode` is parsed from the URL instead of matched as text | gRPC uses BoringSSL defaults (TLS 1.2+); the management API sits behind the operator's TLS terminator. **Accepted.** The control plane's listener certificate is read at start (restart to rotate). Site CA rotation and robot certificate renewal: **scheduled** |
| **Supply chain** | Locked dependencies; ruff security rules | Base images pinned by digest; actions pinned by SHA; Dependabot; dependency audit in CI with reviewed, expiring exceptions; release images scanned (critical stops the release), signed, with SBOM and provenance; signed agent bundles (§17.7) | Three accepted advisories, until 2027-01-15 (§17.7) |
| **Upgrades** | Signature and per-file digests; no extra files; contained paths; launcher and health rollback | A remote upgrade never goes back to an older release | Release keys are pinned at install from the installer argument, so the installer's copy must be checked. Publishing their fingerprints out of band: **scheduled**. The install directory is writable by the agent, which stages releases there. **Accepted**: the installer works inside it only as the agent's user, so a compromised agent cannot use it to become root at the next install |
| **Artifacts and weights** | Publisher signatures checked at prepare; sealed weights (AES-256-GCM, header-bound); the registry holds ciphertext only | Weight-source tokens are sent over TLS only | No size cap on a single download beyond the digest check and the disk preflight. **Accepted** |
| **What leaves the site** | Outbound allowlist with telemetry modes; hash-chained ledger | — | The chain has no key: the customer verifies it, and truncating the newest entries is not detectable. Upgrade, weight and health fetches carry no site data and are not in the ledger. **Scheduled**: record them, and anchor the chain head with the control plane |
| **Support access** | Grants are customer-created, time-boxed, scoped and audited | — | No support access path exists, so support has no access at all. The break-glass session that consumes grants: **scheduled** |
| **MFA** | Delegated to the customer's identity provider | Optional enforcement through `oidc_required_amr` | API tokens do not use MFA: they are scoped and expiring. **Accepted** |
| **Logging** | No secrets in logs; the outbound gate logs field paths only | The control plane and agent configure logging (INFO messages were dropped before) | Logs are plain text. Structured JSON logs: **scheduled** |
| **Database** | TLS required outside development; migrations reviewed | Append-only audit trigger | One role migrates and serves. A separate migration role: **scheduled** |

### 17.5 Other connection modes (later, on demand)

- **Air-gapped:** signed offline bundles (manifest plus images) imported from an internal registry or removable media; telemetry exported to files.
- **Kubernetes:** an operator and Helm chart for customers who already run clusters, with the same agent semantics.
- **Customer cloud account:** the same agent in the customer's VPC on their GPU instances.

---

## 18. Data privacy

### 18.1 Data classes and where they live by default

| Class | Examples | Default location | Leaves the site? |
| --- | --- | --- | --- |
| Payload | Camera images, robot state, actions, language instructions | Site memory; recorded episodes (§28.2) in the site's episode store, when the deployment records and the site allows it | **Never**: recordings stay at the site; only their counts are reported |
| Model | Weights, normalization statistics, adapter code | Customer registry or site cache | Only if the customer chooses Neura's registry, then encrypted with a customer-managed key |
| Operational metadata | Digests, ids, state transitions, reason codes, timings | Site store | Yes, under a published schema (§18.2) |
| Aggregate metrics | Latency percentiles, deadline misses, utilization | Site; exportable | Yes, or only to the customer's own Prometheus/OTel |

### 18.2 Technical controls

- **Telemetry schema allowlist.** The agent only sends fields defined in a published, versioned schema. No payload fields, no file contents; in minimal mode no free text either. A local **outbound ledger** shows exactly what was sent, so customers can verify instead of trusting.
- **What sandboxed code prints stays at the site.** A profiler or a job runs the customer's code, and what it prints can quote that code or the customer's data. Of a run that failed, the agent reports a reason code and how it ended ("the train job exited with status 1"); its stderr and its own account of the failure go to the site agent's log (`neura_server.sandbox.failed_run`). *Audit fix:* a failed job used to send its stderr's last lines, or its own error text, as the operation's message.
- **Customer-routed telemetry.** Metrics and traces can go only to the customer's own systems, with Neura receiving nothing beyond health and version status.
- **Customer-managed keys.** If weights are stored with Neura, they are encrypted with the customer's KMS key and decrypted only on the site agent. Neura staff cannot read them. Revoking the key revokes access.
- **mTLS everywhere**; encrypted local storage recommended and checked in preflight.
- **No standing remote access.** Support uses break-glass sessions that the customer approves, are time-boxed and are fully audited. No permanent remote shell.
- **Tenancy.** Each customer's site is dedicated. The control plane isolates organizations at the data layer; a dedicated or self-hosted control plane is offered for customers who require it.
- **Supply chain.** Signed images, SBOMs and an open robot runtime (§14), so customers can inspect what runs on their robots and servers.
- **No training on customer data by Neura.** Stated in the contract, and also structurally true, because payloads do not reach Neura by default. The improvement loop (§28) trains the customer's models on the customer's episodes, at the customer's site, for the customer: episodes are recorded and stored there and never sent to Neura.

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

- Semantic HTML, labelled controls and named regions throughout. Native `<dialog>` for modals, so focus is trapped and returned. Live regions announce results. Links in running text differ from it by at least 3:1 contrast as well as color (axe's `link-in-text-block`); links in tables are the row's bold name.
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

### 19.8 The console's structure and the NucleiLabs site

The console was rebuilt in October 2026 after a walkthrough of every screen against the running stack. It takes its structure from developer-infrastructure dashboards such as Modal's: a dark workspace, a sidebar with organization and project switchers, list pages with search and filters, and resource pages with tabs. The behavior of §19.1–§19.4 is unchanged: the same API calls, permissions, state vocabulary and approval at the site.

**How the sections map to the API** (the sidebar, in order)

| Section | Shows | API |
| --- | --- | --- |
| Overview | Robots running, live sessions, servers and open faults; what needs attention; the setup checklist; deployments with rollout progress; action-buffer and round-trip charts; deadline misses; a live activity feed | collections, `metrics/query`, `events` |
| Deployments (was Releases) | List with search and phase filters. A deployment page: lifecycle strip (revision → prepared → requested → approved at the site → running), then tabs: Overview, Revisions, Activations, Sessions, Faults, Activity | `deployments`, `revisions`, `activation-requests`, `rollbacks`, `operations` |
| Robots | List with search, site filter and state counts. A robot page: Overview, Sessions, Faults | `robots`, `sessions`, `faults` |
| Sites & servers | Site cards. A site page: Overview, Servers (GPU allocation, preflight), Robots; connecting a server is a guided flow | `sites`, `servers`, `enrollment-tokens` |
| Live sessions | State counts, filter, per-robot buffer trend | `sessions` (polled every 5 s) |
| Faults | Open or all, with the robot and its local response | `faults` |
| Models & contracts | Tabs: Model artifacts, Robot profiles, Execution profiles, Compatibility; a contract page with its document | contract collections, `compatibility-checks`, `signatures` |
| Capacity | Use against capacity per prepared revision, with assumptions | revisions' `capacity` |
| Activity | Live events (the stream, as received) and the audit log (search, failed only) | `events`, `audit-records` |
| Projects, Settings | Organization: members, API tokens, support access, sign-in, as tabs | organization endpoints |

Earlier addresses (`/releases`, `/execution-profiles`, `/audit`) redirect. Tabs live in the URL (`?tab=`), so every view can be linked to.

**What the walkthrough found and fixed**

- Projects and Settings sent requests with empty identifiers (`/v1/organizations//projects`, `/v1/projects//sites`) while the identity loaded. Scoped queries now wait for their identifier.
- Support access offered only the first project's sites; it now lists every site of the organization.
- A robot that reconnected can still have an older session reported until it expires. Counts of live sessions now count each robot once.

**New in the console**

- **Command palette** (⌘K or Ctrl+K): jump to any page, deployment, robot, site or contract, switch theme, sign out. It searches what is already loaded and calls no API itself.
- **Charts.** Aggregate metrics from `metrics/query` (deadline misses), and live series sampled from polled sessions (buffers, round trips). Live series are labeled as what this console observed since it opened; nothing is invented between samples.
- **Live activity.** The event stream the console already followed for cache invalidation, shown as a feed. Repeated updates of one resource collapse into one line.
- **Themes.** Dark by default, light, or the operating system's. The brand accent is iris. Amber stays reserved for "waiting", so the brand never reads as a warning.

**The NucleiLabs site** (`landing/`)

- A static site with the same design tokens: hero, what Neura does, how it works, safety, shared serving, the console, security and privacy, and a call for design partners.
- Every claim on it is something the product does today or is labeled as in progress. The numbers come from the published reports: 13,028 injected faults across three 72-hour soaks with no invariant violation, and 21 hardware fault scenarios passing on the emulated arm.
- The fleet panel in the hero is a labeled illustration simulated in the browser. The console image is a screenshot of the real console on the development stack.
- The console, docs and contact links are set at build time (`VITE_CONSOLE_URL`, `VITE_DOCS_URL`, `VITE_CONTACT_URL`).
- Served by nginx (`neura-landing` image) with a strict content security policy and no third-party requests.

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
- The log is read in commit order (`services/event_log.py`). A cursor is taken when an event is written, but the event is seen only when its transaction commits, and transactions commit in any order: reading "cursors after the last one seen" skipped an event whose transaction committed after a later cursor's. Each event records its transaction (`xid`), and the stream reads in transaction order, only up to the oldest transaction still running (`pg_snapshot_xmin`), so whatever becomes visible later sorts after everything already sent.
- A stream's position moves past other projects' events too, and its keepalive carries it, so a quiet project's cursor stays recent. A trigger keeps the newest event ever deleted (`events_pruned`): a cursor has expired exactly when an event after it was deleted, which holds when every event of the project was pruned too. A cursor whose own event was pruned, with nothing after it, resumes. `test_events.py` proves the three cases.
- The activity list (`/activity`) is a page, newest first, by cursor. Following it by polling is not a feed and can miss an event that commits late; follow the stream instead.
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

### 20.10 Hosting the cloud side (pilots)

The cloud side is small: the control plane, the console, the NucleiLabs site and Postgres. GPUs and robots stay on customer servers, so hosting cost does not grow with robots. For the first design partners it runs on Google Cloud as one VM (`deploy/gcp`), which costs about $8–15 a month (an e2-micro is in Google's free tier in three US regions; the disks and the address are not):

| Piece | Google Cloud | Why |
| --- | --- | --- |
| Containers | One Compute Engine VM (e2-micro by default, Debian 12, shielded boot, a 1 GB swap file), running `deploy/gcp/compose.yaml` | The whole stack uses about 300 MB with Postgres |
| Postgres | PostgreSQL 17 in a container (`compose.postgres.yaml`), its data on the data disk, listening only on the containers' network | Audit records and signed site state survive the VM: they are on the data disk, snapshotted daily (14 days), so the data-loss bound is a day, not the ≤ 5 min of §21.2 |
| Postgres, when a partner needs §21.2 | `NEURA_DATABASE=cloudsql`: Cloud SQL, shared core, daily backups and point-in-time recovery (14 days), deletion protection, through the Cloud SQL proxy on the VM with the VM's service account. The instance accepts only connector clients (`TRUSTED_CLIENT_CERTIFICATE_REQUIRED`) and has no authorized networks | About $10 a month more. Releasing `postgres` over a Cloud SQL deployment moves the database: the VM copies it into the empty container once, in one transaction, before any migration, then `deploy.sh delete-cloudsql` removes the instance |
| Secrets | Secret Manager: database password, manifest signing key, agent CA certificate and key. The VM reads them into tmpfs; it can add, but not read, the bootstrap admin token | Created once, never rotated by the deploy script: sites pin the signing key and agent CA at enrollment |
| Images | Artifact Registry, pinned by digest in each release. Docker Hub images through `mirror.gcr.io`, also by digest | No unpinned or rate-limited pulls on the VM |
| Sealed weights | A separate persistent disk with daily snapshots (14 days) | The registry (§18.2) is a directory |
| TLS | Let's Encrypt for `<domain>`, `console.`, `agents.` and `enroll.`; renewed daily by a timer | `<ip>.sslip.io` works with no DNS setup, but its Let's Encrypt rate limits may be shared with other users of the service, so pilots use their own domain |

One public port serves everything. Agents need only outbound 443 (§17.1), so the edge nginx routes port 443 by TLS server name before decrypting: `agents.` and `enroll.` pass through to the control plane, which checks the agent's client certificate itself (mutual TLS ends at the control plane, never at a proxy). The edge terminates TLS for the site and the console. Every hop carries the PROXY protocol, and the control plane trusts forwarded addresses only from the Compose network, so rate limits see the real client.

The VM keeps no configuration of its own. `deploy.sh release` builds the images of the current commit, pushes them, and writes the release (compose file, edge config, digests, domain) into the instance metadata. The VM's `neura-apply` watches the metadata, converges the containers, and reports progress as guest attributes, which `deploy.sh` waits on. No SSH is needed to release. The same script runs from a workstation signed in with `gcloud`, or from the manual Deploy workflow with a service account key held as a GitHub secret (`GCP_SA_KEY`); the workflow never prints the admin token. SSH goes only through Identity-Aware Proxy with OS Login. A rebuilt VM converges to the same state from the metadata, Secret Manager and the data disk.

`deploy.sh pause` stops everything billed by the hour (the VM, Cloud SQL and the staging site's VM, and deletes any GPU benchmark VM); disks, the address, secrets and images stay, about $15 a month, and `deploy.sh resume` starts Neura again, on `NEURA_MACHINE_TYPE`. The Deploy workflow runs both, so pausing needs no signed-in workstation.

Verified locally: the same compose file and edge config with Postgres in place of the proxy and a test CA in place of Let's Encrypt. Onboarding and a sealed-weights upload go through the console origin; an agent enrolls through `enroll.<domain>:443` and its mutual-TLS stream through `agents.<domain>:443` connects the site. Not verified here: anything that needs the Google Cloud project itself (provisioning, Cloud SQL, Let's Encrypt).

What this does not give: high availability. A VM restart or a release that replaces the control plane interrupts the console and API for seconds to minutes, which falls short of the illustrative 99.9% of §21.2 only if it happens often; robots are unaffected (§13). When a partner's contract needs more, the same images move to a regional managed instance group or Cloud Run behind a TCP proxy load balancer (still passing TLS through for agents), with Cloud SQL high availability.

### 20.11 The site kit: a customer's site on any machine

`deploy/site-demo` runs a customer's side on any machine with Docker, connected to a Neura (the cloud of §20.10 or a development stack). It is for people evaluating Neura, and for the team to use Neura the way a customer does. No GPU is needed.

**What the kit runs.**
- **The ML team's release** (`./site.sh publish`). It takes a LeRobot ACT checkpoint, by default the published `act_aloha_sim_transfer_cube_human`. It writes the three contracts, signs the artifact with a publisher key it makes, and registers the contracts in the project.
- **The site's server** (`./site.sh connect`, with the console's install command). The site agent enrolls outbound, trusts the publisher key and imports the weights, each file checked against the artifact.
- **The robots.** `neura-aloha-cell` runs two simulated ALOHAs (`neura_robots.aloha`: gym-aloha's scene on plain MuJoCo, step for step identical to gym-aloha) behind the robot runtime and `AssignedArm`, each on its own thread as on its own computer, with mutual TLS to the gateway. A cell controller does what a PLC and an operator do:
  - when a robot is ready, it resets the scene and arms a task;
  - when the cube is handed over or the time is up, it ends the task;
  - it starts no task while a new revision waits to be taken.

  A fault latches until someone resets it on the page.
- **The cell's page** (`127.0.0.1:8095`). It shows each robot live (its own renderer, without shadows, on its own thread) with its state, revision, tasks and successes, and the round trip. It also shows the site agent's connection and the activations waiting for the site operator, which the page approves with the operator credential. Actions need a header that another site's page cannot send, and requests must name the page's own host.

Everything else is done in the console: sites, robots, deployments, revisions, prepare and activate. The kit is one image, holding the agent, the cell and the publisher tools. It uses the models group's locked versions with PyTorch's CPU build, and Mesa for the cameras.

**Development settings.**
- The agent issues the simulated robots their certificates (`dev_robot_certificate_dir`).
- The policy workers run as processes in the agent's container (`worker_sandbox=process`).
- The execution profile is tuned for CPU serving: an observation may be 600 ms old when it reaches the model, and the next chunk is requested with 50 actions left, a one-second budget. On the GPU profile (100 ms, 30 actions), admission control refuses a second robot on a laptop's CPU, by design: its observation would wait behind the first robot's pass, longer than 100 ms.

**Verified.**
- **In this repository's sandbox**, against a local control plane with a random-weights checkpoint of the published one's shape: the published flow, enrollment, signature trust, weight import, prepare, approval from the page, tasks on two robots, and switching revisions mid-task.
- **On a GitHub runner** (the Site kit workflow, `.github/workflows/site-kit.yml`, manual; `tools/smoke_site_kit.py`): the kit's image builds, and `./site.sh` runs the whole flow against the development stack with the published checkpoint from the Hub. A runner's four shared vCPUs take 280 to 550 ms per forward pass and serve one robot (refill at 65 actions). That robot handed the cube over in 2 of 3 tasks, and the control plane saw its session. Runners vary: a later run (Site kit run 5, two robots, after the fix of §9.3) drew one at 629 ms per pass (p99), and the site refused even one robot. At 65 actions left a robot asks every 700 ms, which keeps that CPU 90% busy, past admission's 80% ceiling (`throughput`). That check does not depend on the running batch. Two robots do not fit a four-vCPU runner at this chunk size.

Building and running the kit found these defects, all fixed:
1. **Overlapping activations.** A robot switched to a revision whose activation had not finished, because another robot never switched. A newer approval then replaced that revision, and the gateway stopped serving it under the robot mid-task: the robot starved and faulted. The fix is in §17 (Activation).
2. **Backbone weights.** The LeRobot adapter built ACT from the checkpoint's configuration, which fetches ImageNet backbone weights from the internet before the checkpoint's own weights overwrite them. A worker without network could never load a published ACT checkpoint. The adapter no longer fetches pretrained backbone weights.
3. **Repeated refusals.** The robot process repeated a session refusal every half second, because its detail names each attempt's session. It now reports each refusal reason once.
4. **Activations beyond capacity.** An activation for more robots than the server serves could only time out after ten minutes and revert. It now fails at once (§17, Activation).
5. **Relative paths in the local site.** The local site harness (evidence and qualification) passed relative paths to a gateway running in another directory, so its workers never found the checkpoint. It now resolves them.
6. **Builds and caches.** Compose builds through bake, which refuses to read the CA-bundle secret at `/dev/null`, outside the project. The default is now an empty file in the repository. The kit image's user has no writable home, so its caches live in `/tmp`.

### 20.12 A staging site in the cloud

`deploy/gcp/site.sh` runs the site kit (§20.11) as a customer's site in Google Cloud, against the deployed Neura (§20.10). The robots and the site server are where a customer's are, and the cloud is across the internet.

**The site.** The server is a Spot VM (`e2-standard-8` by default) in another region (`us-east1`; Neura is in `us-central1`) and in its own VPC. No firewall rule lets traffic in except SSH from Identity-Aware Proxy, so the site only dials out, as one behind a customer's firewall. Its service account can only pull the kit's image, which `up` builds and pins by digest in Artifact Registry. Its startup script installs Docker and lays out the kit from the instance metadata. The VM stops itself after a few hours (`--max-run-duration`). Google can also reclaim it at any time, as a site can lose power. It costs about $0.07 an hour while it runs.

**The test.** `site.sh test` (the Staging site workflow, `tools/staging_site_test.py`) does the customer's whole flow:
1. The console's calls go through the API, with the admin token read from Secret Manager and never printed.
2. The site operator's commands run on the server over SSH through IAP: `publish`, `connect`, `approve`.
3. Two robots work, then the site's uplink is impaired: 150 ms ± 30 ms of latency and 2% loss (netem on the server's interface).
4. Then the site is partitioned from Neura (iptables drops every packet to Neura's address), then restored.

The robots must keep working throughout: their gateway is at the site, and the site runs on its signed configuration until its offline authorization expires. Neura must show the site offline during the partition, and the site must reconnect afterwards. The test saves each phase's tasks, faults and Neura's view of the site, and the cameras.

**Verified** (Staging site run 3, against the deployed Neura). The test ran on the default `e2-standard-8` with two robots. Prepare measured 270 ms per forward pass (555 ms for a batch of two), which serves two robots with the next chunk requested at 65 actions left.

| Phase | Tasks (robot 1 / 2) | Succeeded | Robot faults | Neura's view of the site |
| --- | --- | --- | --- | --- |
| As is, 90 s | 10 / 10 | 5 / 5 | none | connected (9 of 9 samples) |
| 150 ms ± 30 ms, 2% loss, 90 s | 11 / 10 | 4 / 6 | none | connected (4 of 4) |
| Partitioned from Neura, 120 s | 12 / 13 | 6 / 4 | none | offline from 64 s in (sampled every 10 s) |
| Restored, 90 s | 11 / 10 | 8 / 2 | none | connected at the first sample |

The robots never stopped and never faulted, whatever the uplink did: their gateway is at the site. Neura saw the partition and the reconnection.

The success rate, 40 of 87 tasks, is below lerobot-eval's 70% for this checkpoint. Two causes are likely, neither measured apart yet. On these CPUs a chunk is computed from an observation a quarter to half a second old. And the robot executes 35 of each chunk's 100 actions before replacing it. On a GPU a forward pass takes milliseconds. The evidence workflow measures the same policy served by Neura against LeRobot alone.

**Liveness of the agent's stream.** Planning the partition test found a gap. Neura showed a site connected for as long as its gRPC stream was open. A partition that drops packets, rather than resetting connections, would leave the stream "open" until TCP gave up (about 15 minutes of retransmissions) or the edge closed it as idle (10 minutes). Both ends now send HTTP/2 keepalive pings every 20 s and close the stream when one goes unanswered for 10 s. The site shows offline about 30 s after a silent partition, and the agent starts reconnecting.

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
| Sharing GPUs across robots saves meaningful money on real workloads | Step 0 (§16) shows little or no robots-per-GPU gain for SmolVLA- and π0-class models at realistic timing, in all four cases of §9.5. *First evidence (§9.5, L4):* 24 to 96 ACT robots per GPU instead of 1, but from overlapping passes more than from batching, so plain co-location captures much of it. With a profile a 0.4 s pass can meet, the VLAs gain less, and Neura's single worker gains least: one process per robot serves 12 SmolVLA robots in simulation, one Neura worker 1 to 4, and both 2 π0 robots. Re-simulated with a batching window sized to the pass, Neura reaches 12 SmolVLA and 4 π0 robots for synchronized arrivals, where π0's memory limits separate processes to 2. That is unbuilt and unvalidated |
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
- Hosted with the NucleiLabs site, at `/docs/`, in its design, with a **preview build for every pull request**.
- Search built into the static build; no third-party tracking scripts.

### 26.7 CI checks for the docs

- The site builds without warnings.
- No broken internal or external links.
- Every page has a valid status label in its front matter.
- Wording lint: pages labeled Proposed or Experimental cannot contain unqualified claims such as "supports", "guarantees" or "production-ready".
- Once a quickstart exists, its commands are executed in CI against the simulator.

### 26.8 Versioning

Until the first release, the site shows a single "next" version with a site-wide banner: *Neura is in design and early development. Nothing described here is a released product.* From the first release, docs are versioned per release, and "next" remains the in-development version.

### 26.9 Implementation notes (M7)

- **The site** is Docusaurus 3 under `site/` (docs only, static, `make docs`). It has local search (no third-party scripts), Mermaid diagrams, and a site-wide banner: *Neura is in early development. Nothing described here is a released product.*
- **Labels.** Each page's `status` front matter is shown as a banner by a wrapper around the docs page component.
  - Pages describing implemented software are Experimental. No page is Supported, and the Compatibility page says so.
  - The Supported table is a component reading `site/data/supported.json`, which only qualification runs write.
- **Pages.** The six initial pages (§26.3), plus pages whose conditions (§26.4) are met:
  - Quickstart: it runs in simulation and CI runs its commands.
  - Fault suite, soak and chaos: anyone can run them.
  - Troubleshooting: from real failures.
  - Generated references: management API, Python SDK, protocol, reason codes and the outbound schema.

  Installation and Benchmarks wait for their conditions (a clean-machine install from the docs; the GPU report). The Development status page says so.
- **Generated references.** `tools/gen_docs.py` writes them from `api/openapi.yaml`, the SDK, the protobuf schema (through `protoc`, with its comments) and the two catalogs. `make gen` writes them, and `make check-generated` fails when they are stale.
- **Checks** (`tools/check_docs.py`, the `docs` CI job):
  - every page has one valid label, and a Deprecated page a removal date;
  - outside code, a page not labeled Supported cannot say "supports", "supported", "guarantees", "production-ready" and similar (generated references quote product strings and are exempt);
  - the Supported data is complete;
  - external links answer, and links into this repository must name a path in the checkout;
  - the quickstart's blocks marked `quickstart` run in one shell and must succeed;
  - the build fails on any broken link, anchor or image.
- **Hosting.** The site's image (`neura-landing`) builds the docs and serves them at `/docs/`, next to the home page and in its design: the console's tokens, the site's navigation and footer. Its scripts are held to the site's policy: no inline script runs except the few Docusaurus writes, allowed by their hashes (`site/scripts/csp.mjs`). Every pull request uploads the built docs as a downloadable preview (the CI `docs` job).

---

## 28. The improvement loop: capture, evaluate, improve

### 28.1 Decision and scope

Decision (October 17, 2026): Neura grows from running policies into improving them, in this order:
capture what deployed robots do, evaluate candidate models against the one running, and fine-tune
on captured episodes. This reverses two non-goals of §1 (training, evaluation as a product). The
reasons:
- **The wedge stays.** Running policies safely at the site (§1–§17) is what customers adopt first.
- **The data is where Neura already sits.** Every observation and chunk passes through the robot
  runtime and the gateway, so the runtime layer is the one place that can record what deployed
  robots saw and did, including failures and takeovers.
- **The loop is the product.** Captured failures, a fine-tune that addresses them, evidence that
  it is no worse, and a staged rollout through the existing release flow (§13): only a vendor on
  the action path can sell that end to end.

What does not change:
- **Payloads stay at the site** (§18.1). Episodes are payload: they are recorded, stored and
  trained on at the site, on the customer's servers. The cloud sees counts and identifiers through
  the published allowlist (§18.2).
- **The action path comes first.** Recording, upload, evaluation and training never share a thread,
  a queue or an event loop with serving. Where they compete for a resource (disk, GPU, network),
  they lose: dropped and counted, never waited for.
- **The site's operator sets the boundary** (§17.4). The cloud can ask a deployment to record; the
  site records no more than its operator allows, and by default nothing.

Training at the site is the default. A cloud training backend is possible later, opt-in per
customer, behind the same job interface, and never the default.

### 28.2 Capture (M9, built)

**What an episode is.** One task: from arming until the task stops, the operator takes over, a
fault latches or the task epoch advances. A task longer than its deployment's limit continues in a
new episode (`part` 1, 2, ...). Format `neura-episode/1` (`neura_runtime.capture.episode`), one
directory per episode:

| File | Content |
| --- | --- |
| `episode.json` | Robot, revision and the three contract digests; timing; cameras; why it ended; its outcome; chunk and fault events; every file's SHA-256 and size. Written last |
| `steps.npz` | Per control period: time, action index, the commanded action and the robot's state (robot order and units), and which camera frame was recorded then |
| `camera-<role>.frames` | That camera's frames, raw HxWx3 in the camera's color order, each zlib-compressed (level 1, cheap enough for a robot computer) |

Robot units and order throughout: mapping to a model's units happens at export, through the same
execution-profile mappings serving uses.

**On the robot** (`neura_runtime.capture`):
- **Recorder.** The runtime has observers (`RuntimeObserver`: command, chunk, epoch, fault), called
  on the control loop. An observer that raises is detached and counted; the robot carries on.
  `EpisodeRecorder` only takes note:
  - the commanded action and the state the control loop last read, every period (no extra bus
    read on an SO-101);
  - camera frames every `image_stride` periods.
- **Spool.** A writer thread behind a queue bounded in bytes (64 MiB). When the queue is full, the
  frame or step is dropped and counted, and the episode is marked incomplete; recording never
  waits. When the spool directory is full (2 GiB by default), new tasks go unrecorded and are
  counted. Layout `recording/` → `ready/` → `uploading/`, moved by atomic renames. A crash leaves
  a partial recording, which is discarded, and an interrupted upload, which is retried.
- **Uploader.** A thread of its own, with a gRPC connection of its own, so it never shares the
  gateway stream. It uploads one episode at a time, oldest first, under an optional bandwidth
  limit, and deletes an episode only after the site acknowledges it; retries back off.
- **Outcomes.** An integration labels an episode (a task sensor, the simulator's reward), or the
  operator does (`outcome success|failure NOTE`). Without a label, a fault or takeover is a
  failure (`source: rule`) and anything else is `unknown`. A closed episode waits five seconds for
  its label before upload; after upload, labels are set at the site.
- **Failures mode** uploads only failures (fault, takeover, or a failure label) and deletes the
  rest on the robot.

**What to record** comes with each robot's `Assignment` (`CaptureSettings`): mode, endpoint, image
stride, cameras, maximum episode length, spool size and upload bandwidth. It flows from the
deployment's `capture` spec (API), through the signed manifest, to the agent. The agent clamps it
to the site's boundary (`NEURA_AGENT_CAPTURE_ALLOWED`: `off` by default, then `failures` or `all`)
and passes it to the gateway's configuration. A robot records only if it has a spool directory
(`capture_dir` in `neura-arm`'s configuration, `--capture-dir` for the ALOHA cell). A change takes
effect at the robot's next task boundary.

**At the site** (`neura_server.capture`, hosted by the agent, not the gateway):
- **Capture service** (`Capture.UploadEpisode`, its own port, 50052 by default). Robots
  authenticate as they do to the gateway (site-CA certificates, or development tokens). A robot
  may upload only its own episodes, only for a revision that records, and only within a size
  limit. Each file is checked against the manifest's size as it arrives and against its digest
  before the episode is stored. An episode already held is acknowledged again, not stored twice.
- **Episode store.** Read-only episode directories and a SQLite index (WAL). It keeps labels with
  source and note, and dataset snapshots: immutable lists of episodes chosen by a filter (robot,
  revision, artifact, outcome, end reason, time). Each snapshot is named by a digest over its
  episodes' manifests and labels, so a training run names exactly what it used. Retention by size
  (200 GiB by default) and age deletes the oldest episodes first, never one a dataset in use holds.
- **Operator access.** The local API and CLI list, label and delete episodes, and create and
  release datasets (`neura-agent episodes | label | delete-episode | datasets | dataset |
  release-dataset`). Each change is audited.
- **What leaves the site.** `SiteStatus.capture`: whether recording is allowed, episode and step
  counts, bytes, outcomes per revision and datasets in use. These are allowlisted in
  `catalog/outbound.yaml` (counts are withheld in minimal mode), recorded in the outbound ledger
  like everything else, and shown in the API as `Site.status.capture` and
  `Deployment.status.capture`.

**Export for training** (`neura_server.capture.export`) writes episodes as a LeRobot dataset
(v3.0, LeRobot 0.6.1's `LeRobotDataset`). It uses the model's state inputs, camera inputs and
actions, in the model's order and units. Frames recorded every few periods are held between
recordings. Episodes that cannot train the model are left out with the reason: incomplete, another
control period, other commands or cameras, no instruction for a language model. The dataset loads
offline.

**Evidence.**
- Unit tests:
  - the recorder follows the task;
  - a full writer queue drops frames, never commands;
  - a full spool records nothing;
  - a failing observer is detached;
  - uploads are kept until acknowledged;
  - failures mode.
- Store tests: same-robot rule, damaged files, out-of-order and oversized parts, labels, datasets
  pinning against retention, age retention, restart mid-upload.
- gRPC tests: authenticated, refused when the revision does not record, refused across robots.
- Export test: a real LeRobot dataset, read back in model units.
- **End to end on the emulated SO-101** (`packages/neura-robots/tests/test_capture_arm.py`): the
  site's assignment turns recording on, the operator arms, labels the task and stops it. The
  verified episode reaches the site's store with its label, its steps and its frames, while the
  arm is served as usual.

Not built yet: recording the operator's corrective motions during a takeover (it needs the
customer's teleoperation to feed Neura its commands), and video encoding on the robot. The
episode viewer is in §28.5.

### 28.3 Evaluate (M10, built)

An evaluation compares a **candidate** revision with a **baseline**: by default the revision most
of the deployment's robots run, the one the candidate would replace. Both run at the site, in the
worker sandbox (`neura-learn evaluate`, `packages/neura-learn`), and never on a GPU that serves
robots (§28.4, *Jobs at the site*). Three suites:

- **Simulation.** Both models on the same seeded scenes of a task the site can simulate (ALOHA
  transfer-cube and insertion, §25), in lockstep: the world waits for the policy, as LeRobot
  evaluates its published policies, so the comparison measures the models and not the computer.
  The verdict is a paired non-inferiority test on the success rates (`neura_contracts.stats`): the
  difference of paired proportions with a Newcombe hybrid-score interval, one-sided at 95%,
  against a margin agreed before the run (0.10 by default):
  - `superior`: the lower bound is above zero;
  - `non_inferior`: the lower bound is above minus the margin;
  - `inferior`: the upper bound is below minus the margin;
  - `inconclusive`: anything else (too few scenes to tell).

  Each model's success rate comes with its Wilson interval, and every scene's seed and outcome is
  in the report.
- **Replay.** Both models on observations the site's robots recorded (§28.2). At every *n*-th
  recorded frame each model predicts a chunk, compared with the actions the robot then executed,
  in the model's units and normalized by each action's range. Errors are reported by episode
  outcome. An episode the model cannot use is left out and counted by why, by the same rule as
  a dataset export (`usable_frames`: incomplete, another robot or camera, a missing state
  reading). The candidate passes (`non_inferior`) when its mean error on successful episodes is at
  most the baseline's × (1 + 0.25) + 0.01, and is `inconclusive` without successful episodes. A
  replay is a regression check on real data: it can catch a candidate that drifts from behavior
  that worked, but it cannot show a candidate is better, since the baseline produced those actions.
- **Shadow.** The candidate on the live observations of the robots running the baseline, for a set
  time (`neura_server.shadow`), its actions compared with the running model's and never sent to a
  robot. The running model comes first, by construction:
  - the candidate has a batch group and a worker of its own, never a robot's scheduler, and runs
    on a device no worker serves on (as jobs do);
  - it takes one observation at a time; one offered while it is busy, or while its worker is
    still starting, is skipped and counted, never queued, and its results have no deadline;
  - on the robot's path the gateway only takes note (which observation, which chunk).

  Each model's observation is decoded through its own execution profile, and both chunks are
  compared in robot command units, over the actions a robot executes between refills, as a share
  of each command's range. The site reports how many observations it offered and compared, the
  mean, p95 and maximum divergence, what was skipped and why, the candidate's failures and
  invalid outputs, and commands outside the robot's ranges. The verdict is `inferior` when the
  candidate failed, produced unusable actions or asked for more out-of-range commands than the
  running model; `inconclusive` below `min_samples` compared; otherwise `non_inferior` when the
  mean divergence is within the tolerance (0.05 by default), `inferior` when not. Like a replay,
  a shadow says how far the candidate is from the running model, not which is better.

**API** (tag *Improvement*). `POST /v1/deployments/{id}/evaluations` with the candidate, an
optional baseline and the suite creates an `Evaluation` and an `Operation` (`kind: evaluate`), and
sends the site a `RunEvaluation` command. A replay's episode filter is translated for the site:
robots by the names the site knows them by, and only the deployment's own robots and revisions.
The site snapshots the episodes it selects into a dataset (released when the evaluation ends, so
it holds nothing from retention) and reports the verdict, the suite, both models' rates or errors,
the comparison and, for a replay, counts and the dataset's digest. All of it is allowlisted
(`catalog/outbound.yaml`, `evaluation.*`); no episode leaves the site. The control plane records a
report only if it is of the kind asked for (`EVALUATION_REPORT_INVALID` otherwise).

A shadow needs the baseline to be what the deployment's robots run, and both to be for the same
robot profile and control period. It runs as a site job (one at a time) for its duration, or
until `max_samples` are compared, and takes the candidate out of the gateway when it ends,
fails or is cancelled.

**Release gates.** A deployment's `release_gates.evaluations` lists the kinds of evaluation a
revision needs before robots switch to it: for each robot targeted, a `Succeeded` evaluation of
each kind, of the candidate against the revision that robot runs now, with a `superior` or
`non_inferior` verdict. An activation request without it is refused (409,
`API_RELEASE_GATE_UNMET`, naming what is missing). Robots that run nothing yet, or already run the
candidate, need no evidence; neither does a rollback.

**Evidence.**
- Statistics tests: intervals stay within [0, 1] and contain the estimate, pairing is used, each
  verdict, and the margin is a difference in success rate.
- Replay tests on real LeRobot models (synthetic weights): a model that predicts what the robot
  did, and one 10% of each action's range off, give the expected errors and verdicts.
- A simulation suite run on the ALOHA scene with two checkpoints.
- Agent tests: an evaluation of the site's own episodes, reported within the allowlist, its
  dataset released; a job refused when every GPU serves robots (`AGENT_JOB_DEVICE_BUSY`).
- Control-plane tests: the commands sent, the filter translated, the default baseline, reports of
  the wrong kind refused, the gate refusing an `inferior` verdict and accepting `non_inferior`, a
  first activation and a rollback passing without evidence.

- Shadow tests at the gateway: the candidate is offered the robot's observation and the robot gets
  only its own chunk; one request queued in the robot's scheduler; a busy candidate skips and the
  robot is served every time; sampling, invalid outputs, a failed worker, a lost pair; a reload
  keeps an unchanged shadow's statistics; the verdict's cases.
- Shadow with real workers (`tests/integration/test_shadow_worker.py`): two ACT checkpoints
  with different weights, each in its own worker process. The candidate shadows a robot served
  by the base: its chunks differ, none reaches the robot, the robot is refused nothing while it
  shadows, and removing the shadow stops its worker only. A candidate whose weights are damaged
  fails to load and never touches the robot; the evaluation fails with the worker's reason.
- **Shadow, end to end** (`packages/neura-control/tests/test_shadow_e2e.py`): two simulated robots
  run the base revision over mutual TLS. Activation of a candidate is refused for want of a shadow
  evaluation. The shadow evaluation serves the candidate on the robots' live observations until
  five are compared, reports `non_inferior`, and leaves the gateway as it was; the robots were
  served throughout with no rejection. Then the activation is accepted.

### 28.4 Improve (M11, built)

A training job fine-tunes a revision's model on episodes its site recorded, at the site
(`neura-learn train`).
1. **Data.** The site snapshots the episodes the job's filter selects into a dataset (§28.2) and
   exports it as a LeRobot dataset in the model's units.
2. **Training.** LeRobot's own training loop (`lerobot_train.train`), called with a configuration
   built from the base checkpoint and a recipe: steps, batch size, learning rate (the policy's own
   preset by default), seed, data loader workers and logging. By default the fine-tune keeps the
   base model's normalization statistics: a few hundred recorded episodes should adjust a policy,
   not move every input it was trained on (`normalization: dataset` uses the new data's). Losses
   are read from LeRobot's log and reported as progress.
3. **A new artifact.** The new checkpoint becomes a `ModelArtifact`: the base's contract with the
   new weight files and a `lineage`, part of the digest:
   - the base artifact's digest;
   - the dataset's digest and episode count, and the site it is at;
   - the recipe, the steps, the job, and the software versions that trained it.

   The base's evidence is dropped: it is not this model's. The weights go into the site's artifact
   store and never leave the site. The artifact is signed with the site's training key
   (`training-key.pem` in the agent's state directory), which the site trusts for its own models;
   the key's creation and every job are in the site's audit log. The base's execution profile is bound to the new digest, so the
   fine-tune is served as its base was.

The training job's dataset stays pinned: the lineage names it.

**The control plane** (`POST /v1/deployments/{id}/training-jobs`) sends `RunTraining` with the
filter (translated as for a replay), the recipe and the new artifact's name and version. By
default these are the base's name and `<version>-ft.<job>`; a name and version already registered,
or claimed by a running job, is refused. On success it checks what the site sent before it
registers anything:
- the artifact's digest is the one reported and its name and version are the ones asked;
- its lineage names the base revision's artifact, this job and the dataset reported;
- it changes nothing but its weights, evidence and lineage;
- the profile is the base's, bound to the new digest, and the three are compatible;
- the site's signature is valid.

It then registers the artifact (with the site's signature) and the profile, and adds a candidate
revision to the deployment. That revision reaches the site in its manifest, to prepare, evaluate
and activate like any other. A result that does not verify registers nothing and fails the
operation (`TRAINING_RESULT_REJECTED`).

**Jobs at the site** (`neura_agent.jobs`). Jobs run one at a time, as `python -m neura_learn`
through the worker sandbox's launcher (a process, or the pinned worker image with entrypoint
`neura-learn`), under a timeout (6 hours by default). They report progress as JSON lines every few
seconds and can be cancelled with their operation. They take a GPU no worker serves on; a site
whose GPUs all serve robots refuses the job unless its operator allows sharing
(`NEURA_AGENT_JOB_SHARE_GPUS`), and `NEURA_AGENT_JOB_DEVICE` pins one. Remote evaluation and
training are site operations its operator can turn off, like any other (§17.4).

**Evidence.**
- A fine-tune of a real LeRobot ACT model (synthetic weights) on recorded episodes produces a
  checkpoint the serving adapter loads, with the base's normalization kept.
- The agent's job, end to end: the artifact verifies, its lineage names its base and its
  dataset, its signature is the site's and trusted there, and its weights are in the site's store.
- Control-plane tests: every tampering is refused and registers nothing (another base, another
  dataset, a changed profile, a changed adapter, a bad signature, missing documents); one name and
  version names one model.
- **The loop, end to end** (`packages/neura-control/tests/test_improvement_e2e.py`): a real agent
  holds four recorded episodes. The control plane asks it to fine-tune the running model on the
  three successful ones; the agent trains and reports. The control plane registers the signed
  artifact and adds a candidate revision, which reaches the site. Activation is refused until a
  replay evaluation of the candidate against the running model, on the site's episodes, returns
  `non_inferior`; then it is accepted.

### 28.5 The loop (M12, in progress)

Built:
- **API resources** for evaluations, training jobs and release gates (§28.3, §28.4), in the
  generated SDK and the API reference.
- **Console.** A deployment's *Improve* tab shows:
  - the episodes its robots recorded, as counts by outcome;
  - the fine-tuning jobs: base, new model, episodes trained on, loss, state and candidate revision;
  - the evaluations: suite, candidate and baseline, the numbers behind the verdict, the verdict;
  - the release gates, which can be changed there.

  *Fine-tune* and *Evaluate* start a job; every value the site will use is sent explicitly.
- **Docs.** The improvement-loop guide on the docs site: recording, labeling, fine-tuning,
  evaluating and gating, what stays at the site, and the limits.
- **The loop, end to end** through the control plane and a real agent on a synthetic-weight ACT
  model (§28.4, *Evidence*).

- **The loop's data path on the simulated ALOHA cell**
  (`packages/neura-learn/tests/test_aloha_loop.py`): the cell's robot, served by a site gateway,
  records its tasks (two arms, fourteen joints, its cameras), each labeled by the simulator; the
  recordings fine-tune the served ACT model; the result is evaluated against its base on the same
  simulated scenes and on the recordings. With synthetic weights nothing is solved, so this shows
  each stage works on the cell's data, not that the loop improves a policy.

- **The episode viewer at the site** (`neura_agent.episode_viewer`, the agent's local API at
  `/episodes`): the operator signs in with the operator credential, lists the episodes, steps
  through one's camera frames (PNG, decoded from the store) and commands, and labels or deletes
  it. The page is static, loads only itself (a strict content security policy) and sets data as
  text; its data comes from the local API behind the same credential, and never leaves the site.
  Tests: the API (credential, steps, frames compared pixel for pixel, labels, deletion) and the
  page in Chromium, with the accessibility checks the console passes.

Not built yet: the same run with the published pretrained ALOHA checkpoint (it needs the model
hub, or a GPU runner, to show a policy improving).

---

## 29. The product: sites, robots, policies, rollouts

### 29.1 Decision and scope

Decision (October 23, 2026): Neura is a deployment company. The wedge is that policies are **safe to
run and safe to change** on real robots. The product makes that as simple as Modal made running code
on GPUs. Everything earlier (§1–§28) stays underneath. What changes is what a person sees:

- **Four nouns.** A **site** is where robots are, either a **bench** for trying things or
  **production**. A **robot** is found by the site's server, not typed in. A **policy** has numbered
  **versions**. A **rollout** changes what robots run (deploy, try, rollback). Revisions, execution
  profiles, operations and activation requests still exist, but are made and driven by Neura.
- **One command line, one console, one API** over the same objects (`neura`, §29.2; the console's
  four pages; `/v1/workspace`, `/v1/projects/{id}/policies|rollouts|activity`).
- **Every kind of customer.** Individuals sign up on their own and get a workspace. Companies bring
  SSO, add people, and keep the cloud to records only (§18). One declaration model covers LeRobot
  checkpoints, a team's own Python models and proprietary stacks in containers (§29.3).

The invariant that does not change: **no cloud credential and no role can approve an activation.**
Only the site can (§17.4). A bench may approve its own activations only if both sides say it is a
bench: its server was installed as one (`NEURA_AGENT_BENCH=true`) and its site is a bench in the
signed manifest. Production sites wait for a person at the site (`neura approve`).

### 29.2 The `neura` command

```
neura login                                  browser sign-in (device flow), or a token on stdin
neura site create lab --bench                a site
neura site join lab --simulate 1             this machine becomes its server
neura robots                                 what the sites found, and what each runs
neura policy push ./ckpt --name pick         a version: checked, signed, its files to the sites
neura check ./ckpt --run                     the same checks without pushing, timed here
neura try pick --on sim-1                    on a bench, right away
neura deploy pick --to arm-1,arm-2           check, prepare, shadow, approve, switch
neura approve                                at the site: what waits to switch
neura status | logs | rollback | cancel | doctor
```

The rules that keep it clean:
- One header saying what is being done, and one line per step with its mark and reason.
- A failure says what happened, then why and what to type next.
- Colour only marks state, and never appears with `NO_COLOR` or off a terminal.
- `--json` anywhere prints the API's objects for scripts.

Sign-in follows RFC 8628. The CLI shows a code, the person approves it in the console, and the CLI
exchanges its secret for a 30-day token with that person's highest permission. The CLI never sees
the person's credentials.

`neura site join` makes this machine a development bench. The agent runs as the current user
(`~/.local/share/neura/sites/<site>/`): enrolled with a single-use token, started detached, with
its own operator credential. It listens on loopback only, runs policy workers as processes, and
trusts this machine's publisher key.

Production sites run the agent as a system service. Its administrator installs it with one line,
which the console shows with a single-use token:

```
curl -fsSL https://<neura>/install.sh | sudo sh -s -- --token nen_...
```

`GET /install.sh` serves a short script with the agent release this control plane offers pinned
in it (`NEURA_CONTROL_AGENT_RELEASE_*`): the bundle's address and SHA-256, and the release keys.
- The script downloads the bundle and refuses it unless its SHA-256 matches.
- It runs the installer the bundle carries (`bin/install.sh`, signed with the release). That
  installer verifies the signature against the pinned keys before installing anything, then
  enrolls the server.
- A bench gets `NEURA_AGENT_BENCH=true`. The workspace's publisher keys are trusted, as with
  `neura site join`.

A control plane with no release configured shows the enroll command for an agent installed with
`deploy/install/install.sh`, and `/install.sh` answers 404.

`<neura>` is the control plane's public URL, which on the cloud deployment is the console's
address: the console's nginx passes `/install.sh` to the API with `/v1/`. A test runs that nginx
and asks it for every route the API serves.

### 29.3 Policy sources, one declaration

A `PolicyDeclaration` says what a policy reads and returns:
- adapter, chunk length and executed actions;
- the cameras it reads, at which size;
- state, language;
- deterministic, stateful, batchable.

Three sources fill it in:

- **LeRobot**: a checkpoint's `config.json` (ACT, Diffusion, SmolVLA, pi0, pi0.5).
- **Python**: `@neura.policy(chunk_length=..., cameras=...)` on a class with `act(observation)`
  (and optionally `act_batch`, `reset`). The file's directory ships with the version. A generated
  `neura-policy.json` names the class and its weights; the site's `python` adapter imports it in
  the worker sandbox.
- **Container**: an image pinned by digest, built from Neura's worker image with the customer's
  package installed. Its declaration file names the class (`entry: acme.policy:Picker`); the
  site's `container` adapter imports it from the image. Proprietary code and weights stay in the
  customer's registry.

From the declaration and the robot's profile Neura writes the model artifact: inputs in the robot's
order and units, outputs as the robot's commands. A policy that does not fit is refused with the
reason ("the policy reads camera 'top'; arm-1 has front, wrist"). Cameras are read at the robot's
size: sites pass frames as robots send them, and a policy that wants smaller images resizes them
itself. The worker checks every chunk: its shape (chunk length × the robot's commands) and that
every value is a finite number. `--image` runs a Python version in a worker image with the team's
dependencies added.

A version is named by its content (`sha-<12 hex>`), so pushing the same files again returns the same
version. It is signed with the pushing machine's publisher key, which is registered with the
workspace and put in the install commands. Its files go to the sites this machine serves (a
directory the agent reads by digest), or sealed to Neura's registry (`--seal-key`). Neura never
reads them.

### 29.4 Rollouts

A deploy goes through these steps:
- **check**: does it fit the robots?
- **prepare**: a provisional revision; the site verifies the weights and times the model.
- **shadow**: the new version runs beside the current one. By default this lasts 10 minutes on
  production sites when robots already run something, and is skipped on benches.
- **approve**: an activation request to the site.
- **switch**: each robot changes at its next task boundary.

A rollback skips to approve and switch, back to what each robot ran before its last live rollout.
The driver advances on every operation report and on a periodic sweep. Its states are checking,
preparing, shadow_testing, awaiting_approval, switching, live, stopped and cancelled.

Timing comes from the site's measurement, never from a guess:
- `refill = ceil((2·pass + rtt + 20 ms) / period) + 1`
- `dispatch = max(100 ms, pass + rtt + 20 ms)`
- `acceptance = max(150 ms, dispatch + 2·pass + rtt + 20 ms)`
- `timeout = acceptance + 50 ms`

Here `pass` is the p99 at a batch the size of the robot count. Until the site has measured, the
revision is prepared with the nominal pass time for the policy's type. When that would not fit the
chunk, it uses the slowest pass that would, so an estimate never refuses a policy; the measurement
decides. If the measured profile differs, the revision is prepared again.

Rollouts on overlapping robots are refused. Robots are shared by deployment, with subset
activation. A site's rollouts are planned one at a time, under its row lock (`db/locks.py`): the
overlap check and a new deployment's name are decided from rows a concurrent plan would change, so
two deploys sent at once start one rollout and refuse the other, never two
(`test_concurrent_deploys_start_one_rollout_per_robot`).

### 29.5 Robots a site finds

The agent reports the robots it drives (`RobotInventory`):
- simulated arms (`sim-N`);
- a robots file (name, driver, port, cameras, or a customer command);
- plugged-in SO-101 adapters (`/dev/serial/by-id/*1a86*`), reported as needing setup.

The control plane registers their robot profiles and creates the robots. A name that is taken gets
the site's name as a prefix. A robot that disappears is shown as missing (`present: false`), never
deleted. One its server still reports, with nothing assigned, is ready. The agent issues each
robot's credentials and supervises its process, with backoff.

A site may have several servers. Each robot records the server that reports it
(`hardware.server_id`), and only that server's inventory can mark it missing. The site is connected
while any of its servers has a stream open: one server's stream ending changes only that server
(`test_site_servers.py`). Two servers that give robots the same local name still share one robot:
the manifest names robots by local name only, so telling them apart needs the server in the
manifest and in the agent's match.

### 29.6 Workspaces

A workspace is an organization and its default project. `GET /v1/workspace` tells a client which
one it is in. With `self_signup` on (Neura's cloud for individuals), a first SSO sign-in gets its
own workspace. Off (a company's deployment), only people an admin added get in. Activity names
people by their verified email and tokens by their name.

### 29.7 The console

Four pages in the sidebar, over the same objects as the CLI:

- **Fleet** (the home page): each site with its robots, what each runs and its state, in the words
  `neura robots` prints (running, ready, needs setup, missing, offline). A banner lists rollouts
  waiting for a person at a site. A site is made here. Its server is connected with the
  `neura site join` command, or the agent's enroll command for a production server, and a
  single-use token that lasts an hour.
- **Policies**: each policy and its versions, with their source (LeRobot, Python, container),
  whether they are signed, and the robots running each. "Deploy vN" picks robots by site, the
  shadow test and a note, then opens the rollout.
- **Rollouts**: every deploy, try and rollback. Each shows the steps `neura deploy` prints, with
  their reasons, and what happened. A rollout waiting for approval says to run `neura approve` at
  the site: the console cannot approve it.
- **Settings**: members, API tokens, the publisher keys that sign versions, support access and
  sign-in.

`/activate` is where a signed-in person approves a `neura login` code. The earlier pages
(overview, deployments, robots, sites and servers, sessions, faults, models, capacity, activity)
are one link away and in the command palette (⌘K), not in the sidebar.

### 29.8 ROS 2 robots

A robot driven through ROS 2 is served like any arm: `neura-arm` with `arm: type: ros2`
(`examples/robots/ros2-arm.yaml`), the same runtime, session and gateway. Two processes, because
the robot's ROS Python (Humble's 3.10, Jazzy's 3.12) is not Neura's 3.13:

- the relay node (`neura_robots/ros2/relay.py`) runs in the robot's ROS environment, with rclpy and
  the standard library only. It subscribes to the joint states and camera images, publishes
  position commands as `Float64MultiArray` for ros2_control's forward position controller, and
  serves them over a local Unix socket: a 4-byte length, a JSON header, then raw bytes;
- `Ros2Arm` (`neura_robots/ros2/arm.py`) is the arm `ArmProcess` drives. It maps ROS joints to the
  robot profile's (`profile = ros * scale + offset`) and back, and delivers images in the profile's
  declared color order.

The runtime stays the execution authority. The relay stamps each reading on arrival with the
robot computer's monotonic clock, the one the runtime judges freshness by. The arm has authority
only while the relay is connected and its joint states are younger than `state_timeout_ms`
(100 ms). Older, and the runtime faults (`ACTUATOR_LOCK_LOST`) and sends nothing: a hold sent on
stale states would hold a position the robot may have left. Holding commands the positions last
read, which a position controller keeps; commands are published reliable with depth 1, so a lost
command is retransmitted and one that waited behind a newer one is dropped. A fault stays latched
when the states come back: driving again takes the operator's `reset` and `arm`.

`test_ros2.py` runs the simulated arm in a ROS 2 Jazzy container and its cameras with the relay in
another, with `neura-arm`'s process on the host. Nothing moves until it is armed. The policy moves
the arm. With a fifth of the packets dropped each way on the arm's link, it is still driven: the
joint states arrive at about 80 a second instead of 100, with no gap longer than about 60 ms. Cut
the link and the runtime faults within the timeout and sends nothing more; restore it, and it
drives again only after `reset` and `arm`. The invariant monitor sees no violation. The drops
are nftables rules in the arm's container (`numgen random`), which need no netem module.

Not covered: DDS security (SROS 2), action interfaces (`FollowJointTrajectory`), and robots whose
controllers take velocity or effort commands.

### 29.9 Tests, and what is not built

Tests:
- The quickstart runs end to end against a live control plane and a real agent started by the CLI
  (join, push, try, deploy, rollback, logs, doctor). Its pushed `@neura.policy` class runs in a
  real policy worker, which the site times.
- The browser sign-in.
- The rollout driver: deploy to live, shadow and rollback, an inferior shadow, a failed prepare,
  timing that does not fit, overlap and cancel.
- Robot discovery and self-signup.
- The one-line installer, piped into `sh` as the console shows it: a real signed release
  downloaded, checked, installed and enrolled; another build of the same version refused.
- The console's four pages, end to end in a browser: a production site with a simulated arm, a
  version pushed with the CLI, deployed from Policies, approved at the site, and live.

- A ROS 2 arm through the relay, under packet loss and a cut link (§29.8).

Not built yet: stateful policies (refused, COMPAT_STATEFUL_POLICY_UNSUPPORTED).

---

## 30. Stage 1: the open runtime (`neura-robot`)

### 30.1 Decision

Neura's wedge is the layer between a robot policy and the robot, wherever the model runs: on the
robot computer, on a GPU server at the site, or hosted. The robot runtime (§5 to §8) is published
as open source, Apache-2.0, as `neura-robot` on PyPI (`neura` is taken). The site layer
(gateway and scheduler, control plane, agent, console, rollouts, shadow, capture and fine-tuning)
stays closed and is what is sold. GPU sharing is not the lead: the L4 benchmark (§9.5) shows one
process per robot serving as many SmolVLA robots.

Open: `neura-contracts`, `neura-proto`, `neura-runtime` (with the invariant monitor, the test
oracle for every safety claim) and `neura-robots` (SO-101, ROS 2, the ALOHA simulation, the
robot-side process). The list is `[tool.neura] open-source` in pyproject.toml, and
`tools/check_boundary.py` (in `make lint` and verify) fails on any import or dependency from them
into closed code, a type-only or lazy one included. The head-to-head evidence and hardware
qualification moved to the closed `neura-bench`: they drive the gateway.

### 30.2 Build, in order

1. **The boundary** (done): open packages import only each other. Their tests still use
   the closed gateway as a policy source; they move to an open reference policy server in step 3.
2. **One policy transport interface**: send an observation with a deadline, get an action chunk
   or a typed failure. Freshness, task epochs, merging and faults are the runtime's and behave the
   same over every transport: `LocalConnection` plays the gateway's part for one robot and a
   `PolicyClient` reaches the policy; the observation decoder and the chunk builder are shared
   with the gateway (`neura_runtime.serving`). Custom models come first, since every team has one:
   an `@neura.policy` class runs in the robot's process (`InProcessPolicy`, inference on the robot
   computer) or on another machine with `neura-robot serve`, which speaks the policy worker
   protocol the site gateway already uses with its workers; the robot checks what is served
   against its contracts first. Over a network the server requires a token and refuses to listen
   beyond loopback without one; traffic is not encrypted, so TLS in front of it is the operator's
   for now. Then adapters for servers teams already run, each a thin translator held to the same
   suite: openpi's websocket server (done: `OpenPIKeys` maps Neura's inputs to the policy's names;
   openpi's wire has no request ids, so a request given up on drops the connection rather than
   let its answer be taken for the next one's), then LeRobot's async gRPC server (done: the robot
   tells it which checkpoint to load; every observation is sent must-go, and an answer whose first
   timestep is not the request's is refused. Its replies are pickled, and pickle would load their
   tensors with an unrestricted `torch.load`: they are unpickled only as LeRobot's `TimedAction`,
   with storage loaded weights-only, and only from servers the robot's owner runs). All four pass
   one scenario (`test_local.py`). Hosted APIs when their protocols are public.
3. **Running without a site** (done): `neura-arm run CONFIG --policy URI`, or `policy:` in the
   configuration in place of `gateway:`, with the contracts pinned under `revision:`. The URI is
   a `.py` file (an `@neura.policy` class in this process), `neura://`, `openpi://` or
   `lerobot://` (`neura_robots.policies`). The policy is opened and checked against the
   contracts before the arm is connected: a mismatch refuses to start. `ArmProcess` then runs
   `LocalConnection` instead of the gateway's; episode capture, which the site asks for, is off.
4. **The incident record**: a rolling buffer of observation metadata, every chunk and the
   runtime's decision on it with its reason, operator commands and faults, written as MCAP (it
   opens in Foxglove and Rerun) on a fault or on request. Images off by default; nothing leaves
   the machine. `neura replay` runs a record back through the runtime and must reach the same
   decisions, so each incident becomes a regression test.
5. **Robot-side platforms**: Python 3.10 to 3.13 (JetPack and ROS 2 Humble are 3.10), Linux on
   x86-64 and aarch64, installed with pip.
6. **Extraction**: `git filter-repo` keeps the moved files' history; this repository then depends
   on a released `neura-robot`.

### 30.3 Proof before launch

- One conformance suite run against every transport: nothing moves until armed; a stale chunk is
  refused, a late one dropped; a wrong shape, NaN or out-of-range reply faults; a server crash
  mid-task holds, faults and recovers only after reset; takeover stops commands; the invariant
  monitor sees no violation. A transport is supported only when it passes all of it.
- Against the real openpi and LeRobot servers in Docker with small real checkpoints, not mocks.
- Network faults: latency, jitter, stalls, resets and blackouts on TCP and websockets; nftables
  for UDP; swept over 40 to 200 ms per chunk.
- Property-based tests of merging and freshness over random arrival orders and timings.
- A 24-hour soak per transport; the head-to-head against LeRobot async under the same faults,
  reproducible from one script and published with its method.
- One real SO-101 through in-process, openpi and LeRobot.
- CI: Python 3.10 to 3.13 on x86-64 and arm64; Docker conformance on every pull request; the soak
  nightly.

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
