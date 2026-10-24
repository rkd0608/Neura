---
title: Bring your policy
sidebar_position: 8
description: LeRobot checkpoints, your own Python models with @neura.policy, and proprietary stacks in containers, all described by one declaration.
status: Experimental
---

# Bring your policy

Neura runs three kinds of policy:
- a **LeRobot** checkpoint;
- a team's own model, as a **Python class**;
- a proprietary stack in a **container image**.

All three are described the same way: one declaration says what the policy reads and what it
returns. From it and the robot's profile, Neura writes the model artifact the site runs, and
refuses a policy that does not fit the robot, with the reason.

```bash
neura policy push ./checkpoint --name pick              # a LeRobot checkpoint
neura policy push ./pick.py --name pick                 # a @neura.policy class
neura policy push registry.example/pick@sha256:... \
  --declare neura.yaml --name pick                      # a container
```

`neura check` takes the same arguments and checks without pushing.

## The declaration

| Field | What it says |
| --- | --- |
| `chunk_length` | Actions returned per call. |
| `executed_actions` | Actions the robot runs from each chunk before asking again (default: worked out from the timing). |
| `cameras` | The camera roles it reads, each at a size: `{front: [640, 480]}`. Leave it out to read every camera of the robot; `{}` reads none. |
| `state` | Whether it reads the robot's state (default true). |
| `language` | Whether it reads a task instruction (default false). |
| `deterministic` | Whether the same observation always gives the same actions. |
| `stateful` | Whether it keeps memory between calls. Stateful policies are refused for now. |
| `batchable` | Whether one call can serve several robots (`act_batch`). |

Policies read LeRobot's names, whatever they are built with:
- `observation.state`: the robot's state, in the order of its profile;
- `observation.images.<role>`: each camera's frame;
- `task`: the instruction.

They return a `(chunk_length, action features)` array, in the robot's command order and units.
The site checks every chunk: its shape, and that every value is a finite number.

Cameras are read at the robot's size. The site passes frames as robots send them and never
resizes them. A policy that wants smaller images resizes them itself. A declared size that differs
from the robot's is refused at push, with the fix.

## A LeRobot checkpoint

Point `neura policy push` at a checkpoint directory, or at a Hugging Face Hub repository
(`owner/name`). Its `config.json` declares the policy: ACT, Diffusion Policy, SmolVLA, π0 and
π0.5 are read. Neura checks its input and output features against the robot's profile.

## A Python class

Decorate a class with `@neura.policy` and give it an `act(observation)` method:

```python title="pick.py"
import numpy as np
import torch

import neura


@neura.policy(chunk_length=50, cameras={"front": (640, 480)}, weights="weights")
class Pick:
    """Picks the part from the tray."""

    def __init__(self, weights, device):
        self.model = torch.jit.load(weights / "pick.pt", map_location=device)

    def act(self, observation):
        state = torch.as_tensor(observation["observation.state"])
        image = torch.as_tensor(observation["observation.images.front"])
        with torch.no_grad():
            return self.model(state, image).numpy()
```

- `__init__` may take `weights` (the directory named by `weights=`, next to the file) and `device`
  (`cpu` or `cuda:N`). It gets only the ones it names.
- `act_batch(observations)`, if the class has it, serves several robots in one call.
- The file's directory ships with the version: its code and its weights. A generated
  `neura-policy.json` names the class. At the site, the `python` adapter imports it inside the
  worker sandbox.
- A file with several decorated classes needs the one to push: `pick.py:Pick`.
- `--image name@sha256:...` runs it in a worker image with your dependencies added. Build that
  image from Neura's worker image.

`neura check pick.py --run` imports the class on your machine. It calls it on made-up
observations of the robot's shape, and checks what comes back and how long it takes.

## A container

For a stack you do not ship as files, build an image from Neura's worker image with your package
installed. Pin it by digest. Then write a declaration that names the class:

```yaml title="neura.yaml"
entry: acme_picker.policy:Picker   # a @neura.policy class installed in the image
chunk_length: 50
cameras:
  front: [640, 480]
  wrist: [640, 480]
state: true
language: false
```

```bash
neura policy push registry.example/acme-picker@sha256:<digest> --declare neura.yaml --name pick
```

The site pulls the image by digest and runs it in the worker sandbox, with no network. The site's
`container` adapter imports the class from the image. Your code and weights stay in your registry.

## Versions, signatures and files

- A version is named by its content (`sha-<12 hex>`), so pushing the same files again gives the
  same version. Each new version gets the next number: `pick v3`.
- It is signed with this machine's publisher key, made on first push in `~/.config/neura/` and
  registered with your workspace. A site runs only versions signed by a key it trusts.
- Its files go to the sites this machine serves, a directory the site agent reads by digest. With
  `--seal-key`, they are also uploaded to Neura's registry, encrypted with your key. Neura records
  the version and never reads its files.

## Not yet

- Stateful policies (`stateful: true`) are refused with `COMPAT_STATEFUL_POLICY_UNSUPPORTED`.
- There is no ROS 2 bridge yet: robots connect through Neura's robot runtime.
