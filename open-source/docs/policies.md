# Policies

The runtime reaches a policy through a `PolicyClient`: send one observation's inputs, get one
action chunk back, or a typed failure. Four come with neura-robot, and each passes the same
end-to-end test (`packages/neura-runtime/tests/test_local.py`).

## An `@neura.policy` class

Any model, any framework. The class reads LeRobot's names and returns a
`(chunk_length, action features)` array, in the robot's command order and units:

```python
import numpy as np
import neura_contracts.authoring as neura


@neura.policy(chunk_length=50, cameras={"front": (640, 480)}, weights="checkpoint/")
class Picker:
    def __init__(self, weights, device):
        self.model = load_my_model(weights, device)  # weights: the declared directory

    def act(self, observation):
        # observation["observation.state"]           float32, the robot's joints
        # observation["observation.images.front"]     uint8 (480, 640, 3)
        # observation["task"]                         the instruction, if the policy reads one
        return self.model(observation)
```

Run it in the robot's process — inference on the robot computer, a Jetson for example:

```bash
neura-arm run robot.yaml --policy picker.py
```

`act` runs on a worker thread, so the control loop keeps its period. An exception becomes a failed
request with the reason; a wrong shape or a non-finite value is refused.

## `neura-robot serve`: the same class on a GPU machine

```bash
# on the GPU machine
export NEURA_SERVE_TOKEN=$(openssl rand -hex 16)
neura-robot serve picker.py --device cuda --listen 0.0.0.0:50061

# on the robot (same token)
NEURA_SERVE_TOKEN=... neura-arm run robot.yaml --policy neura://gpu-box:50061
```

The server refuses to listen beyond loopback without a token; requests without it are refused.
Traffic is not encrypted yet: on an untrusted network, put it behind TLS or a VPN. The robot checks
the served chunk length against its contracts before it starts.

## openpi's policy server

Unmodified `scripts/serve_policy.py` from [openpi](https://github.com/Physical-Intelligence/openpi);
the robot maps its inputs to the names the policy reads (see [Configuration](configuration.md)).

openpi's requests carry no identifiers, so when the runtime gives up on a request (superseded by a
newer one, fenced by a new task, or too late), the connection is dropped and reopened: its late
answer is never taken for the next request's. Needs the `openpi` extra.

## LeRobot's async inference server

Unmodified `lerobot.async_inference.policy_server`. The robot tells it which checkpoint to load and
sends every observation as one that must be processed; an answer whose first timestep isn't the
request's is refused.

LeRobot's replies are pickled Python objects. neura-robot decodes them only as LeRobot's
`TimedAction` with tensors, loading tensor storage weights-only — plain unpickling would run
`torch.load` unrestricted. Point robots only at servers you run. Needs the `lerobot` extra
(Python 3.12+).

## Latency

A chunk is accepted only if its observation is younger than the execution profile's
`max_observation_age_at_acceptance_us` when it arrives. A policy slower than that — a large model
on a CPU, a slow link — is refused every time, and the robot never moves. That is the runtime doing
its job: either make the policy faster or declare, in the execution profile, the latency the task
can tolerate.

## Writing a new transport

Implement `PolicyClient` (`packages/neura-runtime/src/neura_runtime/policy.py`): `infer(inputs)`
returns one chunk or raises `PolicyError` with a reason, and `close()`. Add it as a case of the
scenario in `test_local.py`. If the wire has no request identifiers, make sure an abandoned
request's late answer can never be read as the next one's.
