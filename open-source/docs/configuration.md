# Configuration

`neura-arm run CONFIG` and `neura-arm check CONFIG` read one YAML file per robot. Relative paths in
it are relative to the file. Full examples: [`examples/robots`](../examples/robots).

```yaml
robot_id: so101-a                 # the robot's name: in logs, statuses and incident records

revision:                         # the robot's contracts (see the concepts page)
  id: local
  artifact: ../contracts/act-so101.artifact.yaml
  robot_profile: ../contracts/so101.robot.yaml
  execution_profile: ../contracts/act-so101-30hz.profile.yaml

policy:                           # where the policy runs; --policy URI overrides `uri`
  uri: ../policies/mid_range.py
  timeout_s: 2.0                  # per request, for policies over the network

arm: { type: so101, ... }         # or ros2, or mujoco (a simulated arm): see below
cameras: { front: {...}, wrist: {...} }
instruction: pick up the red cube # for language-conditioned policies; or type `instruction ...`
incidents_dir: incidents          # where incident records go; null keeps none
```

## `policy`

| `uri` | Policy | Settings |
|---|---|---|
| `file.py` or `file.py:Class` | an `@neura.policy` class in this process | `weights`, `device` |
| `neura://host:port` | `neura-robot serve` on another machine | token from `NEURA_SERVE_TOKEN` |
| `openpi://host:port` | openpi's policy server | `openpi:` (below) |
| `lerobot://host:port` | LeRobot's async inference server | `lerobot:` (below) |

openpi policies name their inputs their own way, so map them:

```yaml
policy:
  uri: openpi://gpu-box:8000
  openpi:
    state: [{key: observation/state}]            # from the robot's state vector
    images: {observation/image: front, observation/wrist_image: wrist}
    prompt: prompt                               # where the instruction goes
    # images_under: images       ALOHA-style: images nested in one dict
    # channels_first: true       (3, height, width) frames
    # api_key_env: OPENPI_KEY    if the server wants an API key
```

LeRobot's server loads the checkpoint the robot names:

```yaml
policy:
  uri: lerobot://gpu-box:8080
  lerobot: {policy_type: act, checkpoint: /models/act-so101, device: cuda}
```

## `arm`

**SO-101** (`type: so101`, needs the `so101` extra): `port` (the servo bus adapter),
`calibration_id` and `calibration_dir` (LeRobot's calibration), `max_relative_target` (the largest
move of any joint in one command), `on_exit` (`hold` or `release`), `stop_command` (an independent
stop path, for the `customer_stop_path` fault response), `lock_dir`.

**ROS 2** (`type: ros2`): `socket` (the relay's), `joints` (each ROS joint mapped to the robot
profile's name and unit: `profile = ros × scale + offset`), `state_timeout_ms` (older joint states
mean the robot can't be driven), `connect_timeout_s`. See [ROS 2](ros2.md).

**A simulated arm** (`type: mujoco`, needs the `sim` extra): `model` (`franka_panda`, `ur5e`,
`kinova_gen3`, `xarm7` or `so_arm100`, from [MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie)).
It describes itself: leave out `revision:` and `cameras:`,
and its robot profile is read from the model (every actuator in its range, a front camera), the
policy's contracts inferred from what its `@neura.policy` declares, with limits from how fast it
runs on this machine. A remote policy (`neura://`, `openpi://`, `lerobot://`) needs `revision:`.
The models are fetched once with git (about 100 MB, into `~/.cache/neura-robot`), or read from
the Menagerie checkout `NEURA_MENAGERIE` names. Example: [`panda-sim.yaml`](../examples/robots/panda-sim.yaml).

**A recording** (`type: recorded`, needs the `lerobot` extra): `dataset` (a LeRobot dataset of a real
arm: a Hugging Face Hub name, or a local copy under `root`), `episode`, `cameras` (each camera role
of the robot profile, and the dataset's camera it is served from). Its joints must be the robot
profile's. The episode plays at the speed it was recorded; the commands change nothing (open
loop); the episode ending is the robot going away. At exit, `neura-arm` prints how far the
commands were from the recorded actions. Example: [`so101-recorded.yaml`](../examples/robots/so101-recorded.yaml).

## `cameras`

By the robot profile's camera roles: `{type: opencv, index_or_path: ..., fps: 30}` for a real
camera, `{type: synthetic}` for a moving test pattern, `{type: ros2}` for frames from the ROS 2 relay.

## Operator commands

`neura-arm run` reads commands on standard input: `arm`, `stop` (end the task at a boundary),
`takeover` (stop the policy now and hold), `reset` (after a fault, once the arm revalidates),
`instruction TEXT`, `incident` (write an incident record now), `status`, `quit`. Commands that
waited too long are ignored rather than acted on late.
