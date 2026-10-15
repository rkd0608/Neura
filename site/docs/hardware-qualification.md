---
title: Hardware qualification
sidebar_position: 9
description: Connect an SO-101-class arm through Neura, and qualify it with the hardware fault suite, parity and closed-loop episodes.
status: Experimental
---

# Hardware qualification

A model, robot and execution profile combination becomes **Supported** only after it passes three
things on the real robot: the hardware fault suite, open-loop parity, and closed-loop episodes
that show Neura is not worse than running the policy locally. `neura-qualify` runs all three and
writes reports; `neura-qualify promote` adds the combination to the
[Supported table](compatibility) only when every report passes on hardware.

The first integration is an **SO-101-class arm** (six Feetech STS3215 servos) through
[LeRobot](https://github.com/huggingface/lerobot): its bus driver, its calibration
(`lerobot-calibrate`) and its cameras. Everything on this page also runs on an **emulated arm**,
which LeRobot's real bus driver opens like the arm's USB adapter, so you can try it without
hardware. An emulated report can never be promoted.

## What the integration adds to LeRobot

- **One actuator authority.** An exclusive lock on the serial device and an exclusive serial
  port: a second process, Neura's or not, cannot drive the same arm. Losing either faults the
  robot (`ACTUATOR_LOCK_LOST`).
- **No jump on connect.** Every goal is set to the present position before torque is enabled, so
  a goal left in the servos by an earlier program is never executed.
- **The contract matches the arm.** The robot profile's command ranges are checked against the
  arm's calibrated ranges before anything moves; a profile that allows more is refused.
- **Confirmed holds.** A hold sets every goal to the present position, then reads the goals and
  the torque back. The runtime reports a stop only when that succeeds. Torque is never dropped as
  a stop: an SO-101 falls without it.
- **A joint that gives up fails the command.** If a servo's overload protection drops its torque,
  the next torque check fails the command and the robot faults, holding the other joints.
- **Nothing moves without the operator.** `neura-arm` never arms on its own.

## Try it on the emulated arm

You need a source checkout with the models group (`make models-setup`).

```bash
uv run --group models neura-qualify run --emulate --out qualify.json
uv run --group models neura-qualify episodes --emulate --pairs 2 --scores yyyy --out episodes.json
```

The first runs every scenario below against a synthetic ACT checkpoint and the emulated arm, and
measures parity; it takes a few minutes. The second runs the closed-loop protocol with scripted
scores: two episodes per arm prove nothing, and its verdict says so.

## Connect a real arm

1. Calibrate the arm with LeRobot, as for any LeRobot use:
   `lerobot-calibrate --robot.type=so101_follower --robot.port=<port> --robot.id=<name>`.
2. Make the robot's key and certificate (`neura-robot keygen <name>`; the site operator signs it
   with `neura-agent robot-certificate`).
3. Write a `neura-arm` configuration: start from
   [examples/robots/so101-arm.yaml](https://github.com/rkd0608/neura/blob/main/examples/robots/so101-arm.yaml).
4. Check it without moving anything:
   `neura-arm check so101-arm.yaml --robot-profile <robot profile>`. It connects, checks the
   calibration and the profile against the arm, prints where every joint is, and lets go.
5. Serve it: `neura-arm run so101-arm.yaml`. Type `arm` to start a task, `stop` to end it,
   `takeover` to take the arm back at once, `reset` after a fault once it is safe, `status`, and
   `quit`.

## Qualify it

Qualification runs the site's gateway and policy workers on the robot computer (or a machine
next to it) behind a fault proxy, so faults can be injected into the gateway, the workers and the
network. Use the trained checkpoint you will deploy, on the device you will deploy it on: the
arm moves under the policy.

**Safety.** An operator stands at the arm's power switch for the whole run, with the workspace
clear of people and fragile objects. Keep `max_relative_target` small (the default 10 degrees
per command) and the robot profile's command ranges inside the calibrated ranges (the
integration refuses them otherwise). Use `on_exit: hold`. The harness asks the operator to
confirm before anything moves.

```bash
neura-qualify run --arm-config so101-arm.yaml \
  --artifact act.artifact.yaml --robot-profile so101.robot.yaml \
  --execution-profile act-30hz.profile.yaml --checkpoint ./checkpoint \
  --device cuda:0 --manual --out qualify.json
```

### The fault suite

Each scenario arms the robot, lets it run, injects one fault, and checks both what the runtime
decided and what the arm physically did, from its joint readings: after a fault it **held still
within tolerance** (1 degree by default), it received **no command** while faulted or starved, a
latched fault **stayed latched** until reset, and the independent
[invariant monitor](fault-suite) saw no violation. Then it resets, as an operator would.

| Scenario | Fault | Expected |
| --- | --- | --- |
| `nominal` | none, for 20 s | runs at the control rate, control loop on time |
| `network_jitter` | 20 ms extra latency each way | keeps running |
| `network_delay` | 400 ms extra latency each way | stale replies are refused; it holds |
| `partition` | 5 s without network | starvation fault, hold confirmed, still latched after the network returns |
| `reset_connections` | every connection dropped | holds or keeps running |
| `gateway_kill` | the gateway killed and restarted | holds; serves again after reset |
| `gateway_freeze` | the gateway stopped for 2.5 s | holds |
| `worker_kill` | the policy worker killed mid-batch | holds; no old chunk is replayed |
| `operator_takeover` | the operator takes over | Ready at once, hold confirmed, no command after |
| `second_client` | a second process with the same credential | no command from the other session |
| `camera_frozen`, `camera_unplugged` | one camera stops | observations refused; holds |
| `invalid_action` | an out-of-range action with a valid digest | `ACTION_OUT_OF_RANGE` fault before any command |
| `nan_action` | a NaN action with a valid digest | chunk refused |
| `duplicate_chunk`, `old_epoch_chunk` | a reply delivered twice, or from an earlier task | dropped |
| `controller_failure` | the controller refuses a command | `CONTROLLER_COMMAND_FAILED`, hold confirmed |
| `torque_loss` | a servo's overload protection trips (emulated arm only) | fault; the stop is not claimed |
| `bus_unplugged` | the servo bus is unplugged (`--manual` on hardware: the operator does it) | fault; the stop is not claimed |
| `second_runtime_blocked` | a second runtime tries to open the arm | refused; the first keeps running |
| `actuator_lock_lost` | the actuator lock is taken away | `ACTUATOR_LOCK_LOST`; no command; reset refused |

### Parity

During `nominal`, the harness keeps the raw readings behind some observations (joint positions
and camera frames as captured) and the action chunks the site returned for them. Afterwards it
runs the checkpoint through LeRobot alone on the same readings (LeRobot's
`prepare_observation_for_inference`, the checkpoint's processors and `predict_action_chunk`) and
compares. None of Neura's code is on that path. The report states the largest difference; the
default tolerance is 0.001 in the model's action units.

### Closed-loop episodes

```bash
neura-qualify episodes --arm-config so101-arm.yaml --artifact ... --robot-profile ... \
  --execution-profile ... --checkpoint ./checkpoint --device cuda:0 --out episodes.json
```

The same task, scored by the operator, served two ways: by Neura (the site's gateway and worker,
as in production) and by LeRobot alone on the robot computer (each control period, read the arm
and `predict_action`, as `lerobot-record --policy.path` does). Episodes alternate in ABBA blocks
after an unscored warm-up of both. The operator resets the scene before each episode and scores
it after, without being told which way it was served.

The verdict is **non-inferiority**: the lower bound of the one-sided 95% confidence interval for
the difference in success rates (Neura minus local, Newcombe's hybrid score interval) must be
above minus the margin. The defaults are 30 episodes each and a margin of 0.15: with both at 90%
success, the lower bound is about -0.14. Ten episodes each cannot show it, even at 100%.

### Promote

```bash
neura-qualify promote --report qualify.json --episodes episodes.json \
  --evidence https://<where the reports are published> --gpu "<GPU model>"
```

It refuses an emulated report, a report with any scenario missing or failed, parity not measured
or not passed, episodes without non-inferiority, and episodes of a different artifact.

## Known limitations

- One arm type so far: SO-101-class arms on LeRobot's Feetech driver.
- The emulator is a test double for the servo bus, not a model of the arm's mechanics; it does
  not move like a loaded arm.
- Camera faults, controller failures and corrupted chunks are injected at the integration
  boundary, not by breaking hardware. A servo overload is only induced on the emulated arm.
- The exclusive serial port does not bind a privileged process; the lock file still does.
- After the servo bus is unplugged, restart `neura-arm`: LeRobot keeps the port marked open.
- The robot's control loop runs on an asyncio event loop; the report states how many ticks were
  late.
