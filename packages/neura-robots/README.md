# neura-robots

Robot integrations for Neura (design §7.1, §16.1 M8):

- `neura_robots.so101`: the SO-101 arm over LeRobot (its Feetech bus driver, calibration and
  cameras), with one actuator authority, no jump on connect, contract checks against the
  calibration and confirmed holds;
- `neura-arm-emulator`: six emulated STS3215 servos behind a pseudo-terminal, which LeRobot's
  real bus driver opens like the arm's USB adapter, for development and CI;
- `neura-arm`: the robot-side process for a real arm, driven by a local operator
  (`examples/robots/so101-arm.yaml`);
- `neura-qualify`: the hardware fault suite, open-loop parity against LeRobot alone,
  closed-loop non-inferiority episodes, and promotion to the docs' Supported table
  (`qualification/reports/`).

The SO-101 integration needs LeRobot: `uv sync --group models`.
