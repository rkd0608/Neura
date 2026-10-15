# neura-bench

The GPU-sharing benchmark (design §9.5, §10, §16 step 0): Neura's shared serving against the
dedicated baselines, for each way robots can share a GPU, reproducible from one command.

```
make bench                                    # on the GPU runner: --preset gpu --device cuda:0
uv run neura-bench run --preset cpu           # a development host (CPU numbers, labeled as such)
uv run neura-bench run --preset smoke --out /tmp/bench   # the harness itself, in about 2 minutes
uv run neura-bench report bench/reports/<device>         # regenerate README.md from results.json
```

Needs the models group (`uv sync --group models`). Reports go to `bench/reports/<device>/`:
`results.json` (everything measured) and `README.md` (generated from it).

## What it does

1. **Models.** Synthetic checkpoints with the real LeRobot architectures and random weights:
   ACT (the reference policy), Diffusion Policy (a second architecture), and ACT fine-tunes that
   differ only in their weights. Latency, memory and batching depend on the architecture and
   input shapes, not on what the weights learned. Task success needs trained weights and is not
   measured.
2. **Measurements on the device**, through real `neura-worker` processes over their Unix
   sockets: latency per batch size, contention between k co-located workers, a background job's
   effect, and the cost and parity of multi-adapter (LoRA) batching.
3. **Calibration.** One robot alone in real time sets the simulator's robot-to-gateway latency.
4. **Simulation** of every case against the baselines over a sweep of robot counts, with the
   production robot runtime, gateway and scheduler, measured forward-pass times and processor
   sharing on the device. Each configuration's capacity is the largest robot count meeting the SLO.
5. **Real-time validation:** the production gateway, real workers and gRPC robots for the main
   configurations, next to the simulation of the same configuration.

| Sharing case | Configurations |
| --- | --- |
| Same weights on every robot | one device per robot; one process per robot on one device; Neura FIFO, round robin, deadline + fairness |
| Per-robot LoRA adapters | one device per robot; one process per robot; Neura multi-adapter batching (simulated from the measured forward pass) |
| Different full fine-tunes | one device per robot; one process per robot; Neura co-located groups |
| Different architectures | one device per robot; one process per robot; Neura co-located groups |

Workloads: phased arrivals, synchronized (bursty) arrivals, and a concurrent non-policy GPU job.
Metrics (§10): deadline misses per 1,000 requests, starvation events and faults per robot-hour,
late-trimmed actions, observation age at dispatch (p50/p99/max), GPU-hours per robot-hour, peak
memory, and the crossover point where sharing stops paying.
