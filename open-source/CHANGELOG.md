# Changelog

All notable changes to neura-robot. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/), with breaking changes possible before 1.0.

## [Unreleased]

### Added

- The robot-side runtime: freshness checks, task epochs, chunk merging (replace, blend, real-time
  chunking), fault handling with latched faults, and operator authority (arm, stop, takeover,
  reset).
- Four ways to reach a policy, one runtime path: in-process `@neura.policy` classes,
  `neura-robot serve` on another machine, openpi's policy server and LeRobot's async server.
- `neura-arm`: run an SO-101 or a ROS 2 arm against any of them, the policy checked against the
  robot's contracts before the arm is connected.
- Incident records: the last moments of a robot's runtime as MCAP, written on a fault or on
  request, and `neura-robot replay` to run one back through a fresh runtime.
- The ROS 2 relay node, an SO-101 servo-bus emulator, and the ALOHA tasks in MuJoCo.
- Python 3.10 to 3.13, on x86-64 and arm64.
- Simulated arms from MuJoCo Menagerie (Franka Panda, UR5e, Kinova Gen3, xArm7, SO-ARM100) as
  `neura-arm` arms (`type: mujoco`), their contracts inferred from the model and the policy.
- Recorded robots (`type: recorded`): an episode of a real arm's LeRobot dataset, from the Hugging
  Face Hub or a local copy, played to the runtime and the policy, with the commands compared to the
  recorded actions.
- Universal Robots arms through Universal Robots' official ROS 2 driver, tested against URSim (the
  real controller software): `make test-ursim`.
- `make doctor`, `make setup-all` and `make assets`: check a machine, install everything, fetch
  the simulated arms' models.

### Fixed

- A camera the policy does not read no longer holds observations back when its frames lag.
- The MuJoCo tests skip with what to install, instead of failing, when libEGL is missing.
- The ROS 2 relay keeps accepting connections after one ends abruptly (it stopped for good).
- `neura-arm` closing a ROS 2 arm ends its connection to the relay (it stayed open, so the next
  connection was never served).

## Roadmap

Planned before 1.0, roughly in order:

- One conformance suite every policy transport must pass, with network-fault sweeps (latency,
  jitter, stalls, blackouts) and a 24-hour soak per transport.
- A published, reproducible comparison against LeRobot's async inference under the same faults.
- Runs on real hardware: an SO-101 through each transport, and a Jetson.
- Built-in TLS for `neura-robot serve`.
- Hosted policy APIs as transports, as their protocols are published.
