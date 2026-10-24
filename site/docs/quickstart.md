---
title: Quickstart
sidebar_position: 7
description: Serve two simulated robots from a local gateway, watched by the invariant monitor, in a few minutes.
status: Experimental
---

# Quickstart

Run a site gateway on your machine and serve two simulated robots from it, each watched by Neura's
independent invariant monitor. Everything runs in simulation: the policy is a mock with simulated
GPU latency and the robots are simulated arms. CI runs the commands on this page as written.

You need Linux (CI runs these commands on Ubuntu), Git, [uv](https://docs.astral.sh/uv/) and `curl`.

## 1. Get the code

```bash
git clone https://github.com/rkd0608/neura.git
cd neura
```

```bash quickstart
make setup
```

## 2. Check a model against a robot

A model artifact, a robot profile and an execution profile must agree before anything is deployed:

```bash quickstart
uv run neura-contracts check \
  --artifact examples/contracts/act-so101.artifact.yaml \
  --robot examples/contracts/so101.robot.yaml \
  --profile examples/contracts/act-so101-30hz.profile.yaml
```

## 3. Serve two simulated robots

Start a gateway with the development configuration in `examples/site/gateway.yaml` (device tokens
instead of certificates, leases in memory), then two simulated robots for 20 seconds. While they
run, look at what the gateway sees:

```bash quickstart
uv run neura-gateway --config examples/site/gateway.yaml &
gateway=$!
uv run neura-sim-robots --robots 2 --duration 20 --check-invariants &
robots=$!
sleep 10
curl -s http://127.0.0.1:8091/v1/local/dataplane | python3 -m json.tool
wait $robots
kill $gateway
```

The robots print a status line every few seconds (state, actions dispatched, queue length) and,
at the end, the invariant monitor's summary: `"violations": 0`. The command fails if there is any
violation.

## 4. Inject faults

The simulated site can run for hours in seconds. This runs six simulated minutes with a fault
every few seconds (network partitions, worker crashes, gateway restarts and freezes, camera
dropouts, operator takeovers and more), with an operator resetting latched faults:

```bash quickstart
uv run neura-sim-soak --hours 0.1 --mean-fault-interval 5
```

It prints a summary with the faults injected, the recoveries and `"violations": 0`.

## Next

- [Deploy a policy](./deploy-a-policy.md): the same platform through the `neura` command, from
  a fresh workspace to a simulated arm running your policy.
- [Fault suite, soak and chaos](./fault-suite.md): the full fault suite, the 72-hour soak and
  chaos against real processes.
- The whole platform (control plane, site agent, console) runs with Docker Compose: `make dev`,
  then `uv run python tools/smoke_api.py` onboards a customer and deploys to the simulated robots.
