# Neura GPU-sharing benchmark: NVIDIA L4

Generated 2026-11-03T06:18:55+00:00 from commit `b867bc4` with `neura-bench run --preset vla --device cuda:0 --size base --out /tmp/neura-bench-jcy0vt41/report` (torch 2.11.0, LeRobot 0.6.1). Regenerate the Markdown from `results.json` with `neura-bench report`.

Models: synthetic checkpoints with the real LeRobot architectures and random weights (ACT, Diffusion Policy, SmolVLA; size `base`, cameras 640 x 480, bf16). Latency, memory and batching depend on architecture and shapes, not on what weights learned; closed-loop task success needs trained weights and is not measured here. SmolVLA reads its backbone's configuration and processor from the Hub, not its weights; pi0 is LeRobot's (PaliGemma with Gemma 2B, and a 300M action expert), kept in bfloat16 as LeRobot runs it, its instruction tokenized by a public tokenizer in place of PaliGemma's gated one and padded to the same 48 tokens, so the model computes the same shapes. Their profile here: chunks of 50 actions, the next requested at 30 left, an observation at most 600 ms old when it reaches the model and 1500 ms when the robot accepts its chunk (the example profile's 100 and 150 ms cannot be met by a pass of about 0.4 s).

Not measured: pi0 (measuring it: PolicyError: worker could not start: {'code': 'WORKER_LOAD_FAILED', 'message': "TypeError: create_causal_mask() got an unexpected keyword argument 'cache_position'"}).

## Robots per device at the SLO

SLO: at most 1.0 deadline misses per 1,000 requests, at most 0.0 starvation events per robot-hour, no robot faults, and no more device memory than the device has (23034 MiB: every worker process's peak allocation plus its measured overhead, the CUDA context and caches). A configuration's capacity is the largest robot count at which it and every smaller count measured met the SLO (simulation with measured latency and contention, below).

| Case | Workload | One device per robot | One process per robot | Best Neura |
| --- | --- | --- | --- | --- |
| Same weights on every robot | phased arrivals | 1 | 16, fails at 24 | 8, fails at 12 (Neura, lanes + lined-up requests) |
| Same weights on every robot | synchronized (bursty) arrivals | 1 | 16, fails at 24 | 8, fails at 12 (Neura, lanes + lined-up requests) |
| Same weights on every robot | phased, with a background GPU job | 1 | 16, fails at 24 | 8, fails at 12 (Neura, lanes + lined-up requests) |
| Same weights on every robot: SmolVLA | phased arrivals | 1 | 8, fails at 12 | 16, fails at 24 (Neura, lanes + lined-up requests) |
| Same weights on every robot: SmolVLA | synchronized (bursty) arrivals | 1 | 8, fails at 12 | 12, fails at 16 (Neura, lanes + lined-up requests) |

- **Same weights on every robot:** sharing a device pays, but here one process per robot serves as many or more (16 against 8): on this device batching does not beat co-location. Neura, lanes + lined-up requests serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Same weights on every robot: SmolVLA:** sharing pays, and Neura's batching serves the most robots. Neura, lanes + lined-up requests serves 16 robots per device (0.062 device-hours per robot-hour, against 1 with a device per robot); at 24 robots it misses the SLO, so the crossover where one device stops paying is between 16 and 24.

## Measurements on the device

Forward-pass latency through a worker process, ACT:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 34.15 | 37.71 | 198 | 292 |
| 2 | 44.29 | 48.15 | 228 | 224 |
| 4 | 65.00 | 75.71 | 338 | 153 |
| 8 | 125.09 | 132.80 | 553 | 80 |
| 16 | 266.10 | 299.44 | 986 | 38 |

ACT: a worker process holds 807 MiB beyond its peak allocation (CUDA context, workspaces, cache).

Forward-pass latency through a worker process, Diffusion Policy:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 121.96 | 126.64 | 1051 | 83 |
| 2 | 134.14 | 138.89 | 1051 | 75 |
| 4 | 150.90 | 154.46 | 1051 | 67 |
| 8 | 186.79 | 202.38 | 1051 | 54 |
| 16 | 328.24 | 367.27 | 1118 | 31 |

Diffusion Policy: a worker process holds 703 MiB beyond its peak allocation (CUDA context, workspaces, cache).

Forward-pass latency through a worker process, SmolVLA:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 370.38 | 375.58 | 1165 | 27 |
| 2 | 389.91 | 418.94 | 1165 | 26 |
| 4 | 424.29 | 429.25 | 1165 | 24 |
| 8 | 510.02 | 517.73 | 1165 | 20 |
| 16 | 773.84 | 810.16 | 1381 | 20 |

SmolVLA: a worker process holds 779 MiB beyond its peak allocation (CUDA context, workspaces, cache).

Co-location: k workers on the device, each running batches of one back to back. Alone: p50 34.677 ms. A background job (matrix multiplications) slows a lone worker by 1.092x.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 34.68 | 39.94 |
| 2 | 1.03 | 35.61 | 39.97 |
| 4 | 1.11 | 38.38 | 44.75 |
| 8 | 1.81 | 62.88 | 70.82 |
| 16 | 3.62 | 125.34 | 148.81 |
| 24 | 5.39 | 186.99 | 234.78 |

Co-location of SmolVLA workers (simulated with this, not ACT's). Alone: p50 367.045 ms.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 367.05 | 375.10 |
| 2 | 1.01 | 371.96 | 382.34 |
| 4 | 1.04 | 383.07 | 408.30 |
| 8 | 1.43 | 525.60 | 565.09 |

Simulated link, calibrated from one robot alone in real time: observation age at dispatch p50 7.423 ms, p99 9.704 ms (robot encoding, gRPC and the gateway's checks), simulated as 5.095 ms plus up to 4.655 ms.

Multi-adapter batching: 16 rank-16 LoRA adapters on 13 linear layers of the ACT base model; every request in a batch uses a different adapter. Parity with each adapter merged and run alone: max relative error 6.69e-05 (tolerance 1e-04), passed.

| Batch | Base p50 (ms) | Mixed adapters p50 (ms) | Overhead |
| --- | --- | --- | --- |
| 1 | 20.22 | 23.37 | 1.16 |
| 2 | 25.71 | 30.13 | 1.17 |
| 4 | 45.08 | 52.67 | 1.17 |
| 8 | 94.54 | 106.81 | 1.13 |
| 16 | 209.84 | 230.05 | 1.10 |

Admission control's estimate for ACT on this device: 3 robots (limited by acceptance_freshness), against 4 measured with synchronized arrivals. Admission assumes the worst case (every robot asks at once, behind a batch already running), so it must not exceed the synchronized capacity: it does not (3 <= 4).

For SmolVLA: 6 robots (limited by deadline), against 8 measured with synchronized arrivals: it does not (6 <= 8).

## Results

Per 1,000 requests: deadline misses (served after the predicted starvation time or rejected by the robot as too old, too old to dispatch, or failed). Per robot-hour: starvation events and faults. Age: from the robot's capture to the start of the forward pass.

### Same weights on every robot: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 9.7 / 9.7 | 1.00 | 3% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 9.7 / 9.7 | 1.00 | 3% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 7.5 / 9.7 / 9.7 | 1.00 | 7% | 0.500 | 2010 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 9.7 / 9.7 | 1.00 | 14% | 0.250 | 4020 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.4 | 7.6 / 9.7 / 9.8 | 1.00 | 27% | 0.125 | 8040 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 9.7 / 9.7 | 1.00 | 41% | 0.083 | 12060 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 9.7 / 9.7 | 1.00 | 53% | 0.062 | 16080 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 9.7 / 9.8 | 1.00 | 68% | 0.042 | 24120 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 9.7 / 9.7 | 1.00 | 78% | 0.031 | 32160 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 3% | 1.000 | 1005 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 66.5 | 40.1 / 42.6 / 43.2 | 1.95 | 4% | 0.500 | 1035 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 73.3 | 23.2 / 60.1 / 63.4 | 3.66 | 7% | 0.250 | 1145 |
| Neura, FIFO | 8 | 174.55 | 0.0 | 0.0 | 77.3 | 25.0 / 77.9 / 87.9 | 2.67 | 15% | 0.125 | 1198 |
| Neura, FIFO | 12 | 304.01 | 0.0 | 0.0 | 79.4 | 22.0 / 91.5 / 95.7 | 2.77 | 23% | 0.083 | 1252 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 3% | 1.000 | 1005 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 66.5 | 40.1 / 42.6 / 43.2 | 1.95 | 4% | 0.500 | 1035 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 73.4 | 23.2 / 60.1 / 63.4 | 3.66 | 7% | 0.250 | 1145 |
| Neura, round robin | 8 | 173.13 | 0.0 | 0.0 | 77.2 | 24.9 / 77.7 / 84.0 | 2.64 | 16% | 0.125 | 1198 |
| Neura, round robin | 12 | 304.01 | 0.0 | 0.0 | 79.0 | 24.0 / 85.1 / 94.5 | 2.76 | 23% | 0.083 | 1145 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 3% | 1.000 | 1005 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 66.5 | 40.1 / 42.6 / 43.2 | 1.95 | 4% | 0.500 | 1035 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 73.3 | 23.2 / 60.1 / 63.4 | 3.66 | 7% | 0.250 | 1145 |
| Neura, deadline + fairness | 8 | 168.11 | 0.0 | 0.0 | 77.5 | 24.5 / 77.7 / 91.6 | 2.58 | 16% | 0.125 | 1198 |
| Neura, deadline + fairness | 12 | 300.63 | 0.0 | 0.0 | 79.8 | 28.6 / 81.0 / 94.7 | 2.70 | 23% | 0.083 | 1198 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 3% | 1.000 | 2010 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 66.5 | 40.1 / 42.6 / 42.7 | 1.97 | 4% | 0.500 | 2070 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 72.8 | 22.9 / 41.3 / 42.5 | 3.72 | 7% | 0.250 | 2290 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 70.5 | 15.4 / 40.2 / 42.8 | 4.00 | 9% | 0.125 | 2396 |
| Neura, lanes + lined-up requests | 12 | 1.39 | 0.0 | 0.0 | 78.1 | 9.3 / 79.8 / 90.0 | 3.01 | 12% | 0.083 | 2396 |
| Neura, lanes + lined-up requests | 16 | 165.58 | 0.0 | 0.0 | 78.7 | 13.6 / 75.2 / 96.4 | 3.17 | 18% | 0.062 | 2396 |

### Same weights on every robot: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 9.7 / 9.7 | 1.00 | 3% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 9.7 / 9.7 | 1.00 | 3% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 7.4 / 9.7 / 9.7 | 1.00 | 4% | 0.500 | 2010 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 9.7 / 9.7 | 1.00 | 7% | 0.250 | 4020 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 50.4 | 7.6 / 9.7 / 9.7 | 1.00 | 8% | 0.125 | 8040 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 63.4 | 7.5 / 9.7 / 9.7 | 1.00 | 10% | 0.083 | 12060 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 96.9 | 7.5 / 9.7 / 9.7 | 1.00 | 13% | 0.062 | 16080 |
| Dedicated: one process per robot, one device | 24 | 484.33 | 0.0 | 0.0 | 85.0 | 7.5 / 9.7 / 9.7 | 1.00 | 40% | 0.042 | 24120 |
| Dedicated: one process per robot, one device | 32 | 999.77 | 0.0 | 0.0 | 0.0 | 43.9 / 91.5 / 96.1 | 1.00 | 100% | 0.031 | 32160 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 3% | 1.000 | 1005 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 64.4 | 40.0 / 43.0 / 44.1 | 1.76 | 5% | 0.500 | 1035 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 79.3 | 38.4 / 65.1 / 69.5 | 2.81 | 8% | 0.250 | 1145 |
| Neura, FIFO | 8 | 200.67 | 0.0 | 0.0 | 76.0 | 8.9 / 81.8 / 89.7 | 2.63 | 16% | 0.125 | 1198 |
| Neura, FIFO | 12 | 287.48 | 0.0 | 0.0 | 81.6 | 38.7 / 89.3 / 92.2 | 2.46 | 24% | 0.083 | 1198 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 3% | 1.000 | 1005 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 64.4 | 40.0 / 43.0 / 44.1 | 1.76 | 5% | 0.500 | 1035 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 79.7 | 38.6 / 64.6 / 69.5 | 2.79 | 8% | 0.250 | 1145 |
| Neura, round robin | 8 | 194.63 | 0.0 | 0.0 | 76.1 | 23.1 / 79.0 / 87.4 | 2.63 | 15% | 0.125 | 1145 |
| Neura, round robin | 12 | 281.08 | 0.0 | 0.0 | 80.9 | 24.6 / 88.8 / 93.8 | 2.73 | 23% | 0.083 | 1198 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 3% | 1.000 | 1005 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 64.4 | 40.0 / 43.0 / 44.1 | 1.76 | 5% | 0.500 | 1035 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 79.7 | 38.6 / 64.6 / 69.5 | 2.79 | 8% | 0.250 | 1145 |
| Neura, deadline + fairness | 8 | 203.32 | 0.0 | 0.0 | 75.6 | 22.3 / 79.9 / 90.7 | 2.65 | 15% | 0.125 | 1145 |
| Neura, deadline + fairness | 12 | 280.72 | 0.0 | 0.0 | 79.4 | 18.1 / 86.8 / 91.2 | 2.76 | 23% | 0.083 | 1198 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 3% | 1.000 | 2010 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 64.4 | 39.9 / 43.0 / 43.1 | 1.76 | 5% | 0.500 | 2070 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 77.6 | 38.7 / 42.3 / 42.8 | 2.65 | 7% | 0.250 | 2290 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 67.1 | 8.5 / 40.7 / 42.9 | 3.95 | 7% | 0.125 | 2290 |
| Neura, lanes + lined-up requests | 12 | 4.84 | 0.0 | 0.0 | 75.5 | 8.8 / 81.1 / 85.5 | 3.08 | 12% | 0.083 | 2290 |
| Neura, lanes + lined-up requests | 16 | 178.08 | 0.0 | 0.0 | 76.3 | 8.3 / 86.0 / 90.9 | 2.85 | 19% | 0.062 | 2290 |

### Same weights on every robot: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 9.6 / 9.7 | 1.00 | 4% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 9.6 / 9.7 | 1.00 | 4% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 7.5 / 9.7 / 9.7 | 1.00 | 7% | 0.500 | 2010 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 9.7 / 9.7 | 1.00 | 15% | 0.250 | 4020 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 9.7 / 9.7 | 1.00 | 30% | 0.125 | 8040 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 7.5 / 9.7 / 9.7 | 1.00 | 45% | 0.083 | 12060 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 9.7 / 9.7 | 1.00 | 56% | 0.062 | 16080 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 7.5 / 9.7 / 9.8 | 1.00 | 73% | 0.042 | 24120 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 9.7 / 9.8 | 1.00 | 81% | 0.031 | 32160 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 4% | 1.000 | 1005 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 67.9 | 40.1 / 42.6 / 46.6 | 1.95 | 5% | 0.500 | 1035 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 81.0 | 23.2 / 68.1 / 74.7 | 3.61 | 7% | 0.250 | 1145 |
| Neura, FIFO | 8 | 224.56 | 0.0 | 0.0 | 81.5 | 24.6 / 86.3 / 89.7 | 2.67 | 17% | 0.125 | 1198 |
| Neura, FIFO | 12 | 345.75 | 0.0 | 0.0 | 80.7 | 21.9 / 91.2 / 95.3 | 2.45 | 27% | 0.083 | 1252 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 4% | 1.000 | 1005 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 67.9 | 40.1 / 42.6 / 46.6 | 1.95 | 5% | 0.500 | 1035 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 80.8 | 23.2 / 68.1 / 74.7 | 3.61 | 7% | 0.250 | 1145 |
| Neura, round robin | 8 | 213.12 | 0.0 | 0.0 | 79.7 | 23.0 / 84.2 / 88.6 | 2.62 | 17% | 0.125 | 1198 |
| Neura, round robin | 12 | 328.98 | 0.0 | 0.0 | 80.3 | 25.1 / 86.7 / 93.2 | 2.51 | 26% | 0.083 | 1198 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 4% | 1.000 | 1005 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 67.9 | 40.1 / 42.6 / 46.6 | 1.95 | 5% | 0.500 | 1035 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 80.8 | 23.2 / 68.1 / 74.7 | 3.61 | 7% | 0.250 | 1145 |
| Neura, deadline + fairness | 8 | 213.76 | 0.0 | 0.0 | 79.7 | 22.5 / 83.0 / 88.8 | 2.54 | 17% | 0.125 | 1198 |
| Neura, deadline + fairness | 12 | 324.58 | 0.0 | 0.0 | 80.0 | 22.2 / 85.9 / 92.2 | 2.62 | 26% | 0.083 | 1198 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 41.2 / 43.0 / 43.1 | 1.00 | 4% | 1.000 | 2010 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 67.6 | 40.2 / 42.4 / 42.6 | 1.92 | 5% | 0.500 | 2070 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 80.2 | 22.9 / 42.0 / 42.5 | 3.66 | 7% | 0.250 | 2290 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 74.6 | 15.1 / 47.4 / 69.0 | 3.79 | 9% | 0.125 | 2396 |
| Neura, lanes + lined-up requests | 12 | 38.08 | 0.0 | 0.0 | 80.1 | 9.4 / 86.9 / 94.7 | 2.68 | 15% | 0.083 | 2396 |
| Neura, lanes + lined-up requests | 16 | 221.41 | 0.0 | 0.0 | 77.3 | 12.6 / 82.6 / 91.6 | 2.90 | 20% | 0.062 | 2396 |

### Same weights on every robot: SmolVLA: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 550.0 | 7.6 / 9.6 / 9.7 | 1.00 | 56% | 1.000 | 1944 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 550.0 | 7.6 / 9.6 / 9.7 | 1.00 | 56% | 1.000 | 1944 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 550.0 | 7.9 / 9.7 / 9.7 | 1.00 | 100% | 0.500 | 3888 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 550.1 | 7.4 / 9.7 / 9.7 | 1.00 | 100% | 0.250 | 7776 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 648.8 | 7.6 / 9.7 / 9.7 | 1.00 | 100% | 0.125 | 15552 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 938.9 | 7.6 / 9.7 / 9.7 | 1.00 | 100% | 0.083 | 23328 |
| Dedicated: one process per robot, one device | 16 | 905.36 | 0.0 | 0.0 | 959.9 | 7.5 / 9.7 / 9.7 | 1.00 | 100% | 0.062 | 31104 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 600.0 | 41.0 / 42.9 / 43.0 | 1.00 | 56% | 1.000 | 1944 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 861.9 | 334.9 / 365.9 / 378.5 | 1.05 | 96% | 0.500 | 1944 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 915.8 | 341.6 / 399.9 / 414.8 | 2.00 | 100% | 0.250 | 1944 |
| Neura, deadline + fairness | 8 | 239.93 | 892.5 | 0.0 | 464.4 | 315.3 / 564.1 / 590.4 | 2.67 | 100% | 0.125 | 1944 |
| Neura, deadline + fairness | 12 | 282.66 | 1092.5 | 5.0 | 444.1 | 287.2 / 542.8 / 586.6 | 5.52 | 100% | 0.083 | 2052 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 600.0 | 41.0 / 42.9 / 43.0 | 1.00 | 56% | 1.000 | 3888 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 628.8 | 40.0 / 42.8 / 43.0 | 1.98 | 58% | 0.500 | 3888 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 683.7 | 39.3 / 42.7 / 43.1 | 3.23 | 63% | 0.250 | 3888 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 749.7 | 25.0 / 42.9 / 43.1 | 4.80 | 74% | 0.125 | 3888 |
| Neura, lanes + lined-up requests | 12 | 0.00 | 0.0 | 0.0 | 919.5 | 38.5 / 299.2 / 341.7 | 5.17 | 97% | 0.083 | 4104 |
| Neura, lanes + lined-up requests | 16 | 0.00 | 0.0 | 0.0 | 938.2 | 173.0 / 353.2 / 402.1 | 5.34 | 100% | 0.062 | 4050 |
| Neura, lanes + lined-up requests | 24 | 0.55 | 2.5 | 0.0 | 951.8 | 215.7 / 441.7 / 559.1 | 8.00 | 100% | 0.042 | 4158 |
| Neura, lanes + lined-up requests | 32 | 314.65 | 890.6 | 215.6 | 461.9 | 244.3 / 572.0 / 591.7 | 8.18 | 100% | 0.031 | 4320 |

### Same weights on every robot: SmolVLA: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 550.0 | 7.6 / 9.6 / 9.7 | 1.00 | 56% | 1.000 | 1944 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 550.0 | 7.6 / 9.6 / 9.7 | 1.00 | 56% | 1.000 | 1944 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 550.0 | 7.4 / 9.6 / 9.7 | 1.00 | 57% | 0.500 | 3888 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 581.5 | 7.7 / 9.7 / 9.7 | 1.00 | 62% | 0.250 | 7776 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 791.0 | 7.6 / 9.7 / 9.7 | 1.00 | 83% | 0.125 | 15552 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 936.5 | 7.5 / 9.7 / 9.7 | 1.00 | 100% | 0.083 | 23328 |
| Dedicated: one process per robot, one device | 16 | 1000.00 | 0.0 | 0.0 | 960.1 | 7.6 / 9.7 / 9.7 | 1.00 | 100% | 0.062 | 31104 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 600.0 | 41.0 / 42.9 / 43.0 | 1.00 | 56% | 1.000 | 1944 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 895.8 | 336.9 / 363.1 / 366.3 | 1.00 | 100% | 0.500 | 1944 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 915.1 | 342.5 / 401.6 / 415.5 | 2.00 | 100% | 0.250 | 1944 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 800.1 | 38.9 / 40.5 / 41.1 | 8.00 | 76% | 0.125 | 1944 |
| Neura, deadline + fairness | 12 | 277.19 | 1082.5 | 2.5 | 418.9 | 285.5 / 545.5 / 590.9 | 5.22 | 100% | 0.083 | 2052 |
| Neura, deadline + fairness | 16 | 313.43 | 1198.1 | 65.6 | 362.6 | 302.1 / 580.0 / 589.0 | 7.30 | 100% | 0.062 | 2160 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 600.0 | 41.0 / 42.9 / 43.0 | 1.00 | 56% | 1.000 | 3888 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 623.9 | 40.0 / 43.0 / 43.0 | 1.71 | 59% | 0.500 | 3888 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 669.7 | 39.6 / 42.6 / 43.1 | 2.76 | 63% | 0.250 | 3888 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 800.1 | 38.9 / 40.5 / 41.1 | 8.00 | 76% | 0.125 | 3888 |
| Neura, lanes + lined-up requests | 12 | 0.00 | 0.0 | 0.0 | 870.5 | 39.4 / 303.4 / 443.1 | 5.06 | 95% | 0.083 | 4104 |
| Neura, lanes + lined-up requests | 16 | 5.52 | 24.4 | 0.0 | 927.5 | 158.0 / 363.5 / 477.9 | 5.41 | 100% | 0.062 | 4320 |
| Neura, lanes + lined-up requests | 24 | 17.62 | 57.5 | 0.0 | 920.0 | 143.3 / 441.8 / 503.2 | 7.93 | 100% | 0.042 | 4320 |

## Real-time validation

The production gateway with real worker processes on the device and simulated robots over gRPC, next to the simulation of the same configuration (same robot count, warmup and window). Each cell is real / simulated. With a few hundred requests a p99 is close to the maximum: one collision moves it, so read it with the median and the miss rate. Late ticks: robot control ticks the harness itself ran late (high values mean the harness, not Neura, was overloaded).

| Case | Configuration | Robots | Requests | Misses / 1k | Starvation / robot-h | Age p50 ms | Age p99 ms | Mean batch | Device busy | Late ticks | Worker restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Same weights on every robot: phased arrivals | Dedicated: one device per robot | 1 | 60 / 60 | 0.00 / 0.00 | 0.0 / 0.0 | 6.8 / 7.8 | 8.8 / 9.6 | 1.00 / 1.00 | 4% / 3% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 1 | 60 / 60 | 0.00 / 0.00 | 0.0 / 0.0 | 40.4 / 41.2 | 43.1 / 42.9 | 1.00 / 1.00 | 4% / 3% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 4 | 240 / 240 | 0.00 / 0.00 | 0.0 / 0.0 | 40.7 / 22.9 | 50.1 / 51.3 | 4.00 / 3.81 | 7% / 7% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 8 | 608 / 583 | 210.53 / 176.67 | 0.0 / 0.0 | 31.9 / 24.7 | 107.3 / 77.6 | 3.50 / 2.60 | 15% / 16% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 16 | 1761 / 1529 | 461.10 / 372.14 | 22.5 / 0.0 | 40.7 / 33.9 | 136.4 / 92.0 | 2.30 / 2.17 | 39% / 34% | 1.4% | 0 |
| Same weights on every robot: phased arrivals | Neura, FIFO | 16 | 1966 / 1539 | 518.82 / 376.22 | 105.0 / 0.0 | 44.6 / 28.4 | 154.2 / 93.1 | 2.85 / 2.56 | 42% / 32% | 2.0% | 0 |
| Same weights on every robot: phased arrivals | Dedicated: one process per robot, one device | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 6.6 / 7.6 | 8.5 / 9.7 | 1.00 / 1.00 | 57% / 53% | 0.0% | 0 |
| Same weights on every robot: synchronized (bursty) arrivals | Neura, deadline + fairness | 16 | 1854 / 1559 | 494.07 / 384.22 | 56.2 / 0.0 | 41.9 / 20.5 | 136.2 / 85.9 | 2.79 / 2.72 | 39% / 31% | 1.4% | 0 |
| Same weights on every robot: phased, with a background GPU job | Neura, deadline + fairness | 16 | 2465 / 1561 | 656.79 / 385.01 | 543.7 / 0.0 | 47.9 / 29.5 | 127.6 / 83.5 | 2.04 / 2.27 | 59% / 36% | 0.5% | 0 |
| Same weights on every robot: SmolVLA: phased arrivals | Neura, deadline + fairness | 4 | 352 / 308 | 36.93 / 0.00 | 45.0 / 0.0 | 46.5 / 341.4 | 446.5 / 399.0 | 3.59 / 1.99 | 69% / 101% | 0.0% | 0 |
| Same weights on every robot: SmolVLA: phased arrivals | Neura, lanes + lined-up requests | 4 | 360 / 360 | 0.00 / 0.00 | 0.0 / 0.0 | 44.7 / 39.4 | 60.0 / 42.8 | 4.00 / 3.30 | 64% / 63% | 0.0% | 0 |
| Same weights on every robot: SmolVLA: phased arrivals | Dedicated: one process per robot, one device | 2 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 6.9 / 8.0 | 9.4 / 9.7 | 1.00 / 1.00 | 99% / 100% | 0.0% | 0 |

## Method and limits

- Configurations follow design §10: the dedicated baselines run batch size 1 with the same model, precision, observation encoding and harness; Neura runs one worker per batch group with the scheduler policy named.
- Latency is measured through `neura-worker` processes over their Unix sockets, so serialization and IPC are included. Simulation draws forward-pass times from these measurements (lognormal fitted to p50 and p99) and shares the device between running passes by the measured co-location slowdown; beyond 24 concurrent workers the slowdown is extrapolated linearly.
- Multi-adapter batching is measured at the forward pass and simulated at the serving level; the data plane does not group robots by base model yet (design §9.5: built only if this benchmark shows it pays). Measured overheads below 1 are timing noise and are not credited.
- Simulated robots tick with no timing noise, and the simulated gateway decodes an observation in no time. Real robots drift by milliseconds, so near-simultaneous requests sometimes miss the batching window: the simulation is somewhat optimistic for synchronized arrivals near a freshness limit (compare the validation table).
- On a CPU host the background job and the worker share the CPU with the gateway and the simulated robots, so real ages there include the harness slowing down; the simulation models the device only. On a GPU the background job competes for the GPU alone.
- Admission control is set aside in the benchmark so it can measure what N robots get; production refuses sessions beyond the admission estimate.
- Robots that latch a fault are reset by a simulated operator after one second and counted.

## Reproduce

```
neura-bench run --preset vla --device cuda:0 --size base --out /tmp/neura-bench-jcy0vt41/report
```
