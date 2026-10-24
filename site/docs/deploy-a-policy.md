---
title: Deploy a policy
sidebar_position: 7
description: From a fresh workspace to a simulated arm running your policy, then a new version and a rollback, with the neura command.
status: Experimental
---

# Deploy a policy

Neura has four things in it:
- a **site**, where robots are: a **bench** for trying things, or **production**;
- its **robots**, which the site's server finds;
- **policies**, each with numbered versions;
- **rollouts**, which change what robots run.

This page goes through all four with the `neura` command, on one machine, with a simulated arm. A
test runs the same steps end to end against a live control plane, with a real agent started by the
command:
[packages/neura-control/tests/test_neura_cli.py](https://github.com/rkd0608/neura/blob/main/packages/neura-control/tests/test_neura_cli.py).
The output shown is from a run of these steps against that test's control plane, whose workspace
is called Acme Robotics and whose token is called test admin.

You need what the [quickstart](./quickstart.md) needs, and Docker for the local control plane.

## 1. Sign in

`make dev` starts a control plane on your machine with a development admin token. Sign in with
it:

```bash
make dev
grep -o 'ntk_tok_[A-Za-z0-9_-]*' deploy/compose/compose.yaml \
  | uv run neura login --api http://127.0.0.1:8080 --with-token
```

```
  ✓ Signed in to Acme Robotics  admin, deploy, read
  Neura at http://127.0.0.1:8080
```

Against a hosted Neura, `neura login` opens the browser instead. It shows a code, you approve it in
the console, and the command gets a 30-day token with your permissions. It never sees your
password or your SSO credentials.

## 2. A bench, and this machine as its server

```bash
uv run neura site create lab --bench
uv run neura site join lab --simulate 1 --mock-policies --control-plane 127.0.0.1:8081
```

```
  ✓ Created bench site lab

  Add a computer to it:
    this one        neura site join lab --simulate 1
    another one     neura site install-command lab

  Joining lab

  ✓ Enrolled this machine       ~/.local/share/neura/sites/lab
  ✓ Agent running               pid 1134, log ~/.local/share/neura/sites/lab/agent.log

  ROBOT   SITE   STATE   RUNNING
  sim-1   lab    ready   —
```

`neura site join` enrolls this machine with a single-use token and starts the site agent for your
user. The agent listens on loopback only.
- `--simulate 1` adds a simulated SO-101 arm, `sim-1`.
- An SO-101 plugged in by USB is found too, and shown as needing setup.
- `--mock-policies` answers with a mock policy, so you do not need PyTorch. Without it, the site
  runs your policy in real worker processes and times it.

`neura robots` lists the robots your sites found and what each runs.

## 3. Push a policy

A policy can be a LeRobot checkpoint, a Python class, or a container image (see
[Bring your policy](./policies.md)). Here it is a class that holds the arm where it is:

```python title="hold.py"
import numpy as np

import neura


@neura.policy(chunk_length=10, cameras={})
class Hold:
    """Holds the arm where it is."""

    def act(self, observation):
        return np.tile(observation["observation.state"], (10, 1))
```

```bash
uv run neura policy push hold.py --name hold
```

```
  Pushing hold

  ✓ Fits so101-follower         reads state; returns 10 actions
  ✓ Signed                      ed25519:a1d97c56847be359
  ✓ Files at lab                2 added, 0 already there (this machine)

  hold v1  python · 560 B

  Deploy it:  neura deploy hold --to ROBOT
```

Neura works out what the policy reads and returns, and checks it fits the robot. The version is
signed with this machine's publisher key, which the bench trusts. Its files go where the bench
finds them: Neura records the version, never its files. Pushing the same files again gives the
same version.

`neura check` does the same checks without pushing. With `--run` it also calls the class on
made-up observations, and times it:

```
$ uv run neura check hold.py --run

  Checking hold.py

  ✓ Fits so101-follower         reads state; returns 10 actions
  ✓ Ran on this machine         10x6 actions, under 1 ms
  ✓ Keeps up on this machine    under 1 ms per pass (measured); next chunk asked 3 actions early
```

## 4. Try it

```bash
uv run neura try hold --on sim-1
```

```
  Trying hold v1 on sim-1

  ✓ Fits the robots       fits 1 so101-follower robot: 30 Hz, 10-action chunks
  ◐ At the site           verifying the weights and timing the model
  ✓ At the site           weights verified; timed at the site (its measurements stay there)
  – Shadow test           not asked for
  ✓ Approval              approved by the bench site's agent
  ◐ Switch                at each robot's next task boundary
  ✓ Switch                sim-1 switched

  Live  hold v1
```

Without `--mock-policies`, the site runs the class in a policy worker and says what it measured:
`weights verified; under 1 ms per pass (measured); next chunk asked 3 actions early`.

Every rollout goes through the same steps:
1. **Fits the robots**: the version is checked against the robots.
2. **At the site**: the site verifies the weights and times the model on its own hardware. The
   timing that keeps the arm fed comes from that measurement.
3. **Shadow test**: the new version runs beside the current one on the same observations. Its
   actions are compared, and never sent to a robot.
4. **Approval**: an activation request goes to the site.
5. **Switch**: each robot changes at its next task boundary.

A bench approves its own activations, and `try` skips the shadow test. Only a bench whose server
was installed as one can approve its own; on a production site a person at the site approves it.

## 5. A new version, and a rollback

Change the policy, push it again, and deploy it. Then put the arm back on what it ran before:

```bash
uv run neura policy push hold.py --name hold      # hold v2
uv run neura deploy hold --to sim-1
uv run neura rollback sim-1
```

```
  Rolling back sim-1

  ◐ Approval              the bench site's agent approves it if its operator allowed that, else a person at lab runs: neura approve
  ✓ Approval              approved by the bench site's agent
  ◐ Switch                at each robot's next task boundary
  ✓ Switch                sim-1 switched

  Live  hold v1
```

A rollback skips the checks: each robot goes back to what it ran before its last live rollout.

On a production site, `neura deploy` runs a 10-minute shadow test when the robots already run
something (`--shadow 0` skips it), then waits for approval. On the site's server, a person runs:

```bash
neura approve
```

No cloud credential and no role can approve an activation: only someone at the site.

## 6. What happened

```bash
uv run neura status          # robots and rollouts
uv run neura logs -n 20      # one line per event
uv run neura doctor          # this machine's setup
uv run neura site stop lab   # stop the agent this page started
```

```
$ uv run neura status

  ROBOT   SITE   STATE     RUNNING
  sim-1   lab    running   hold v1

  ROLLOUT   STATE   STARTED            ID
  hold v1   live    2026-10-24 07:04   ro_01M5PBK5GPJ43KKVTP486KGRBS
  hold v2   live    2026-10-24 07:04   ro_01M5PBK0D7N43V2FKJ5WXB2SY6
  hold v1   live    2026-10-24 07:04   ro_01M5PBJW2ZBKDWWH1R8B8TPDH8

$ uv run neura logs -n 20
  2026-10-24 07:04  lab found a simulated so101-follower: sim-1
  2026-10-24 07:04  test admin added hold v1 (python)
  2026-10-24 07:04  test admin started trying hold v1 on sim-1 at lab
  2026-10-24 07:04  hold v1: fits the robots; preparing at the site
  2026-10-24 07:04  hold v1: waiting for approval at lab
  2026-10-24 07:04  hold v1: approved by the bench site's agent; robots switching
  2026-10-24 07:04  sim-1 now runs hold v1
  ...
```

Every command takes `--json`, which prints the API's objects for scripts. The console shows the
same objects on four pages: Fleet, Policies, Rollouts and Settings.

## A production server

On a production site, connect a server with the line the console's "Add a server" shows. It
carries a single-use token:

```bash
curl -fsSL https://<neura>/install.sh | sudo sh -s -- --token nen_...
```

It downloads the agent release your Neura offers, and refuses it unless its SHA-256 is the one
pinned in the script. The installer inside checks the release's signature before installing
anything, then installs the agent as a system service and enrolls the server. Rollouts to its
robots then wait for a person at the site to run `neura approve`.

## Next

- [Bring your policy](./policies.md): LeRobot checkpoints, your own Python models, and
  proprietary stacks in containers.
- [Troubleshooting](./troubleshooting.md).
