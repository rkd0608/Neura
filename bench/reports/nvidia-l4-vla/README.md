# Neura GPU-sharing benchmark: NVIDIA L4

Generated 2026-11-03T16:20:53+00:00 from commit `7a2dbb3` with `neura-bench run --preset vla --device cuda:0 --size base --out /tmp/neura-bench-3j3_ne8w/report` (torch 2.11.0, LeRobot 0.6.1). Regenerate the Markdown from `results.json` with `neura-bench report`.

Models: synthetic checkpoints with the real LeRobot architectures and random weights (ACT, Diffusion Policy, SmolVLA, pi0; size `base`, cameras 640 x 480, bf16). Latency, memory and batching depend on architecture and shapes, not on what weights learned; closed-loop task success needs trained weights and is not measured here. SmolVLA reads its backbone's configuration and processor from the Hub, not its weights; pi0 is LeRobot's (PaliGemma with Gemma 2B, and a 300M action expert), kept in bfloat16 as LeRobot runs it, its instruction tokenized by a public tokenizer in place of PaliGemma's gated one and padded to the same 48 tokens, so the model computes the same shapes. Their profile here: chunks of 50 actions, the next requested at 30 left, an observation at most 600 ms old when it reaches the model and 1500 ms when the robot accepts its chunk (the example profile's 100 and 150 ms cannot be met by a pass of about 0.4 s).

## Robots per device at the SLO

SLO: at most 1.0 deadline misses per 1,000 requests, at most 0.0 starvation events per robot-hour, no robot faults, and no more device memory than the device has (23034 MiB: every worker process's peak allocation plus its measured overhead, the CUDA context and caches). A configuration's capacity is the largest robot count at which it and every smaller count measured met the SLO (simulation with measured latency and contention, below).

| Case | Workload | One device per robot | One process per robot | Best Neura |
| --- | --- | --- | --- | --- |
| Same weights on every robot | phased arrivals | 1 | 16, fails at 24 | 8, fails at 12 (Neura, lanes + lined-up requests) |
| Same weights on every robot | synchronized (bursty) arrivals | 1 | 16, fails at 24 | 8, fails at 12 (Neura, lanes + lined-up requests) |
| Same weights on every robot | phased, with a background GPU job | 1 | 16, fails at 24 | 8, fails at 12 (Neura, lanes + lined-up requests) |
| Same weights on every robot: SmolVLA | phased arrivals | 1 | 8, fails at 12 | 12, fails at 16 (Neura, lanes + lined-up requests) |
| Same weights on every robot: SmolVLA | synchronized (bursty) arrivals | 1 | 8, fails at 12 | 12, fails at 16 (Neura, lanes + lined-up requests) |
| Same weights on every robot: pi0 | phased arrivals | 1 | 2, fails at 4 | 4, fails at 8 (Neura, lanes + lined-up requests) |
| Same weights on every robot: pi0 | synchronized (bursty) arrivals | 1 | 2, fails at 4 | 4, fails at 8 (Neura, lanes + lined-up requests) |

- **Same weights on every robot:** sharing a device pays, but here one process per robot serves as many or more (16 against 8): on this device batching does not beat co-location. Neura, lanes + lined-up requests serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Same weights on every robot: SmolVLA:** sharing pays, and Neura's batching serves the most robots. Neura, lanes + lined-up requests serves 12 robots per device (0.083 device-hours per robot-hour, against 1 with a device per robot); at 16 robots it misses the SLO, so the crossover where one device stops paying is between 12 and 16.
- **Same weights on every robot: pi0:** sharing pays, and Neura's batching serves the most robots. Neura, lanes + lined-up requests serves 4 robots per device (0.250 device-hours per robot-hour, against 1 with a device per robot); at 8 robots it misses the SLO, so the crossover where one device stops paying is between 4 and 8.

## Measurements on the device

Forward-pass latency through a worker process, ACT:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 34.91 | 38.03 | 198 | 286 |
| 2 | 44.89 | 48.28 | 228 | 219 |
| 4 | 65.08 | 73.45 | 338 | 153 |
| 8 | 125.79 | 199.01 | 553 | 78 |
| 16 | 321.28 | 357.32 | 986 | 32 |

ACT: a worker process holds 807 MiB beyond its peak allocation (CUDA context, workspaces, cache).

Forward-pass latency through a worker process, Diffusion Policy:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 123.09 | 130.53 | 1051 | 82 |
| 2 | 133.47 | 143.97 | 1051 | 75 |
| 4 | 150.85 | 155.78 | 1051 | 67 |
| 8 | 189.88 | 203.89 | 1051 | 53 |
| 16 | 297.55 | 334.75 | 1118 | 34 |

Diffusion Policy: a worker process holds 703 MiB beyond its peak allocation (CUDA context, workspaces, cache).

Forward-pass latency through a worker process, SmolVLA:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 375.75 | 381.59 | 1165 | 27 |
| 2 | 397.31 | 404.08 | 1165 | 26 |
| 4 | 429.17 | 458.29 | 1165 | 24 |
| 8 | 509.36 | 522.06 | 1165 | 20 |
| 16 | 759.98 | 792.96 | 1381 | 20 |

SmolVLA: a worker process holds 778 MiB beyond its peak allocation (CUDA context, workspaces, cache).

Forward-pass latency through a worker process, pi0:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 367.56 | 373.64 | 8482 | 28 |
| 2 | 439.09 | 451.63 | 8482 | 23 |
| 4 | 572.72 | 583.57 | 8482 | 20 |
| 8 | 873.98 | 887.34 | 8482 | 20 |
| 16 | 1681.60 | 1718.76 | 9084 | 20 |

pi0: a worker process holds 2115 MiB beyond its peak allocation (CUDA context, workspaces, cache).

Co-location: k workers on the device, each running batches of one back to back. Alone: p50 34.791 ms. A background job (matrix multiplications) slows a lone worker by 1.145x.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 34.79 | 39.39 |
| 2 | 1.05 | 36.46 | 43.78 |
| 4 | 1.11 | 38.61 | 50.44 |
| 8 | 1.77 | 61.58 | 69.03 |
| 16 | 3.55 | 123.41 | 168.30 |
| 24 | 5.38 | 187.12 | 272.42 |

Co-location of SmolVLA workers (simulated with this, not ACT's). Alone: p50 372.083 ms.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 372.08 | 392.84 |
| 2 | 1.00 | 372.45 | 401.79 |
| 4 | 1.04 | 387.08 | 410.91 |
| 8 | 1.37 | 511.09 | 566.99 |

Co-location of pi0 workers (simulated with this, not ACT's). Alone: p50 366.738 ms.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 366.74 | 373.06 |
| 2 | 1.17 | 429.62 | 441.45 |

Simulated link, calibrated from one robot alone in real time: observation age at dispatch p50 7.466 ms, p99 12.311 ms (robot encoding, gRPC and the gateway's checks), simulated as 2.522 ms plus up to 9.888 ms.

Multi-adapter batching: 16 rank-16 LoRA adapters on 13 linear layers of the ACT base model; every request in a batch uses a different adapter. Parity with each adapter merged and run alone: max relative error 6.69e-05 (tolerance 1e-04), passed.

| Batch | Base p50 (ms) | Mixed adapters p50 (ms) | Overhead |
| --- | --- | --- | --- |
| 1 | 21.54 | 24.69 | 1.15 |
| 2 | 26.44 | 30.75 | 1.16 |
| 4 | 45.12 | 52.61 | 1.17 |
| 8 | 93.95 | 105.66 | 1.12 |
| 16 | 205.46 | 226.24 | 1.10 |

Admission control's estimate for ACT on this device: 3 robots (limited by acceptance_freshness), against 4 measured with synchronized arrivals. Admission assumes the worst case (every robot asks at once, behind a batch already running), so it must not exceed the synchronized capacity: it does not (3 <= 4).

For SmolVLA: 4 robots (limited by deadline), against 4 measured with synchronized arrivals: it does not (4 <= 4).

For pi0: 2 robots (limited by deadline), against 2 measured with synchronized arrivals: it does not (2 <= 2).

## Results

Per 1,000 requests: deadline misses (served after the predicted starvation time or rejected by the robot as too old, too old to dispatch, or failed). Per robot-hour: starvation events and faults. Age: from the robot's capture to the start of the forward pass.

### Same weights on every robot: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.3 / 12.4 / 12.4 | 1.00 | 4% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.3 / 12.4 / 12.4 | 1.00 | 4% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 12.3 / 12.4 | 1.00 | 7% | 0.500 | 2010 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.5 / 12.4 / 12.4 | 1.00 | 14% | 0.250 | 4020 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 7.6 / 12.3 / 12.4 | 1.00 | 28% | 0.125 | 8040 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 12.3 / 12.4 | 1.00 | 42% | 0.083 | 12060 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 12.3 / 12.4 | 1.00 | 54% | 0.062 | 16080 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 12.3 / 12.4 | 1.00 | 69% | 0.042 | 24120 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 12.4 / 12.4 | 1.00 | 79% | 0.031 | 32160 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.9 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 1005 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 66.8 | 38.9 / 45.2 / 45.2 | 1.92 | 5% | 0.500 | 1035 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 74.0 | 21.8 / 60.7 / 68.7 | 3.69 | 7% | 0.250 | 1145 |
| Neura, FIFO | 8 | 183.67 | 0.0 | 0.0 | 76.8 | 26.8 / 76.3 / 100.3 | 2.62 | 16% | 0.125 | 1145 |
| Neura, FIFO | 12 | 304.35 | 0.0 | 0.0 | 79.9 | 19.3 / 94.6 / 99.8 | 2.69 | 23% | 0.083 | 1198 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.9 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 1005 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 66.8 | 38.9 / 45.2 / 45.2 | 1.92 | 5% | 0.500 | 1035 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 73.9 | 21.8 / 60.7 / 68.7 | 3.69 | 7% | 0.250 | 1145 |
| Neura, round robin | 8 | 161.57 | 0.0 | 0.0 | 77.7 | 26.9 / 77.3 / 101.4 | 2.64 | 16% | 0.125 | 1145 |
| Neura, round robin | 12 | 302.66 | 0.0 | 0.0 | 79.9 | 21.3 / 90.5 / 100.4 | 2.77 | 23% | 0.083 | 1145 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.9 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 1005 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 66.8 | 38.9 / 45.2 / 45.2 | 1.92 | 5% | 0.500 | 1035 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 74.2 | 21.8 / 60.7 / 68.7 | 3.69 | 7% | 0.250 | 1145 |
| Neura, deadline + fairness | 8 | 168.11 | 0.0 | 0.0 | 76.9 | 24.3 / 74.2 / 95.7 | 2.55 | 16% | 0.125 | 1145 |
| Neura, deadline + fairness | 12 | 305.36 | 0.0 | 0.0 | 80.6 | 26.1 / 81.8 / 96.9 | 2.81 | 23% | 0.083 | 1145 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.9 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 2010 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 66.8 | 38.9 / 45.2 / 45.2 | 1.92 | 5% | 0.500 | 2070 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 74.7 | 21.3 / 42.3 / 44.4 | 3.81 | 7% | 0.250 | 2290 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 69.7 | 14.0 / 28.0 / 49.9 | 3.97 | 9% | 0.125 | 2290 |
| Neura, lanes + lined-up requests | 12 | 4.15 | 0.0 | 0.0 | 78.6 | 11.2 / 80.0 / 89.2 | 2.97 | 13% | 0.083 | 2290 |
| Neura, lanes + lined-up requests | 16 | 172.41 | 0.0 | 0.0 | 78.7 | 15.0 / 77.6 / 101.8 | 3.11 | 19% | 0.062 | 2290 |

### Same weights on every robot: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.3 / 12.4 / 12.4 | 1.00 | 4% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.3 / 12.4 / 12.4 | 1.00 | 4% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 8.2 / 12.3 / 12.4 | 1.00 | 5% | 0.500 | 2010 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 12.3 / 12.4 | 1.00 | 7% | 0.250 | 4020 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 37.0 | 7.7 / 12.3 / 12.4 | 1.00 | 8% | 0.125 | 8040 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 64.0 | 7.6 / 12.3 / 12.4 | 1.00 | 10% | 0.083 | 12060 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 92.2 | 7.7 / 12.3 / 12.4 | 1.00 | 13% | 0.062 | 16080 |
| Dedicated: one process per robot, one device | 24 | 494.29 | 0.0 | 0.0 | 79.2 | 7.8 / 12.3 / 12.4 | 1.00 | 41% | 0.042 | 24120 |
| Dedicated: one process per robot, one device | 32 | 1000.06 | 0.0 | 0.0 | 0.0 | 45.1 / 93.2 / 100.8 | 1.00 | 100% | 0.031 | 32160 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.9 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 1005 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 64.6 | 39.2 / 45.8 / 47.1 | 1.69 | 5% | 0.500 | 1035 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 75.0 | 12.2 / 63.4 / 66.0 | 3.00 | 7% | 0.250 | 1145 |
| Neura, FIFO | 8 | 203.98 | 0.0 | 0.0 | 75.9 | 10.8 / 80.6 / 87.2 | 2.62 | 16% | 0.125 | 1145 |
| Neura, FIFO | 12 | 282.87 | 0.0 | 0.0 | 82.0 | 36.7 / 88.9 / 94.4 | 2.43 | 24% | 0.083 | 1198 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.9 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 1005 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 64.6 | 39.2 / 45.8 / 47.1 | 1.69 | 5% | 0.500 | 1035 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 75.0 | 12.2 / 63.4 / 66.0 | 3.00 | 7% | 0.250 | 1145 |
| Neura, round robin | 8 | 192.60 | 0.0 | 0.0 | 76.0 | 9.9 / 79.1 / 87.5 | 2.59 | 16% | 0.125 | 1145 |
| Neura, round robin | 12 | 284.65 | 0.0 | 0.0 | 80.3 | 20.8 / 88.6 / 95.0 | 2.73 | 23% | 0.083 | 1198 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.9 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 1005 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 64.6 | 39.2 / 45.8 / 47.1 | 1.69 | 5% | 0.500 | 1035 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 73.2 | 11.8 / 61.9 / 64.8 | 3.16 | 7% | 0.250 | 1145 |
| Neura, deadline + fairness | 8 | 214.40 | 0.0 | 0.0 | 76.0 | 10.8 / 78.6 / 89.5 | 2.64 | 16% | 0.125 | 1145 |
| Neura, deadline + fairness | 12 | 282.87 | 0.0 | 0.0 | 79.9 | 31.2 / 88.4 / 98.5 | 2.69 | 23% | 0.083 | 1198 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.9 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 2010 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 64.4 | 39.5 / 45.6 / 45.7 | 1.69 | 5% | 0.500 | 2070 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 73.8 | 12.0 / 43.7 / 44.8 | 3.08 | 7% | 0.250 | 2290 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 67.7 | 9.5 / 43.4 / 67.6 | 3.87 | 8% | 0.125 | 2290 |
| Neura, lanes + lined-up requests | 12 | 8.95 | 0.0 | 0.0 | 76.5 | 10.4 / 81.4 / 87.3 | 3.01 | 13% | 0.083 | 2290 |
| Neura, lanes + lined-up requests | 16 | 187.47 | 0.0 | 0.0 | 77.4 | 9.6 / 86.3 / 92.3 | 2.74 | 19% | 0.062 | 2290 |

### Same weights on every robot: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.4 / 12.3 / 12.4 | 1.00 | 4% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.4 / 12.3 / 12.4 | 1.00 | 4% | 1.000 | 1005 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 12.3 / 12.4 | 1.00 | 8% | 0.500 | 2010 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 12.4 / 12.4 | 1.00 | 16% | 0.250 | 4020 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.4 | 7.7 / 12.3 / 12.4 | 1.00 | 32% | 0.125 | 8040 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.4 | 7.8 / 12.3 / 12.4 | 1.00 | 48% | 0.083 | 12060 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 12.3 / 12.4 | 1.00 | 60% | 0.062 | 16080 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.4 | 7.7 / 12.3 / 12.4 | 1.00 | 78% | 0.042 | 24120 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.5 | 7.8 / 12.3 / 12.4 | 1.00 | 85% | 0.031 | 32160 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.7 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 1005 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 77.2 | 38.9 / 45.8 / 48.3 | 1.92 | 5% | 0.500 | 1035 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 82.3 | 21.7 / 70.4 / 76.8 | 3.31 | 8% | 0.250 | 1145 |
| Neura, FIFO | 8 | 245.88 | 0.0 | 0.0 | 84.9 | 20.6 / 87.6 / 98.1 | 2.53 | 18% | 0.125 | 1145 |
| Neura, FIFO | 12 | 360.00 | 0.0 | 0.0 | 83.1 | 21.8 / 94.0 / 100.0 | 2.27 | 30% | 0.083 | 1198 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.7 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 1005 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 77.2 | 38.9 / 45.8 / 48.3 | 1.92 | 5% | 0.500 | 1035 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 82.2 | 21.7 / 70.4 / 76.8 | 3.31 | 8% | 0.250 | 1145 |
| Neura, round robin | 8 | 232.61 | 0.0 | 0.0 | 83.8 | 21.9 / 87.2 / 96.3 | 2.35 | 19% | 0.125 | 1145 |
| Neura, round robin | 12 | 342.17 | 0.0 | 0.0 | 82.0 | 25.0 / 91.1 / 96.7 | 2.39 | 28% | 0.083 | 1145 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.7 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 1005 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 77.2 | 38.9 / 45.8 / 48.3 | 1.92 | 5% | 0.500 | 1035 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 82.2 | 21.7 / 70.4 / 76.8 | 3.31 | 8% | 0.250 | 1145 |
| Neura, deadline + fairness | 8 | 236.88 | 0.0 | 0.0 | 82.9 | 23.4 / 88.2 / 99.1 | 2.34 | 19% | 0.125 | 1145 |
| Neura, deadline + fairness | 12 | 344.86 | 0.0 | 0.0 | 81.1 | 26.1 / 88.8 / 98.2 | 2.27 | 29% | 0.083 | 1145 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 40.7 / 45.5 / 45.7 | 1.00 | 4% | 1.000 | 2010 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 78.1 | 39.4 / 45.2 / 45.2 | 1.91 | 5% | 0.500 | 2070 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 81.8 | 21.3 / 44.9 / 45.4 | 3.48 | 7% | 0.250 | 2290 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 77.4 | 13.5 / 71.4 / 77.7 | 2.80 | 11% | 0.125 | 2290 |
| Neura, lanes + lined-up requests | 12 | 120.34 | 0.0 | 0.0 | 82.2 | 20.6 / 89.9 / 101.6 | 2.38 | 18% | 0.083 | 2290 |
| Neura, lanes + lined-up requests | 16 | 260.12 | 0.0 | 0.0 | 82.0 | 15.3 / 89.4 / 100.6 | 2.57 | 24% | 0.062 | 2290 |

### Same weights on every robot: SmolVLA: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 551.4 | 7.9 / 12.3 / 12.3 | 1.00 | 56% | 1.000 | 1943 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 551.4 | 7.9 / 12.3 / 12.3 | 1.00 | 56% | 1.000 | 1943 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 552.2 | 8.1 / 12.4 / 12.4 | 1.00 | 100% | 0.500 | 3886 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 556.1 | 7.7 / 12.3 / 12.4 | 1.00 | 100% | 0.250 | 7772 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 645.5 | 7.8 / 12.3 / 12.4 | 1.00 | 100% | 0.125 | 15544 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 906.8 | 7.7 / 12.3 / 12.4 | 1.00 | 100% | 0.083 | 23316 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 955.5 | 7.6 / 12.3 / 12.4 | 1.00 | 100% | 0.062 | 31088 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 601.4 | 40.9 / 45.5 / 45.6 | 1.00 | 56% | 1.000 | 1943 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 856.8 | 337.5 / 371.6 / 381.9 | 1.10 | 93% | 0.500 | 1943 |
| Neura, deadline + fairness | 4 | 1.64 | 7.5 | 0.0 | 919.5 | 349.6 / 409.7 / 418.3 | 1.99 | 100% | 0.250 | 1943 |
| Neura, deadline + fairness | 8 | 307.05 | 963.8 | 0.0 | 423.7 | 328.8 / 485.7 / 577.7 | 2.63 | 100% | 0.125 | 1943 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 601.4 | 40.9 / 45.5 / 45.6 | 1.00 | 56% | 1.000 | 3886 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 647.4 | 39.1 / 45.1 / 45.6 | 1.93 | 60% | 0.500 | 3886 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 678.1 | 37.6 / 44.5 / 45.7 | 3.27 | 64% | 0.250 | 3886 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 745.8 | 25.4 / 45.2 / 45.7 | 4.77 | 73% | 0.125 | 3886 |
| Neura, lanes + lined-up requests | 12 | 0.00 | 0.0 | 0.0 | 922.7 | 27.2 / 215.4 / 354.9 | 7.69 | 92% | 0.083 | 4102 |
| Neura, lanes + lined-up requests | 16 | 12.75 | 48.8 | 0.0 | 921.1 | 156.2 / 375.3 / 491.7 | 5.34 | 100% | 0.062 | 4264 |
| Neura, lanes + lined-up requests | 24 | 70.70 | 262.5 | 6.2 | 855.7 | 202.9 / 527.4 / 595.5 | 7.74 | 100% | 0.042 | 4318 |

### Same weights on every robot: SmolVLA: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 551.4 | 7.9 / 12.3 / 12.3 | 1.00 | 56% | 1.000 | 1943 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 551.4 | 7.9 / 12.3 / 12.3 | 1.00 | 56% | 1.000 | 1943 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 551.1 | 8.3 / 12.3 / 12.4 | 1.00 | 59% | 0.500 | 3886 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 593.5 | 8.0 / 12.3 / 12.4 | 1.00 | 63% | 0.250 | 7772 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 750.7 | 7.9 / 12.3 / 12.4 | 1.00 | 82% | 0.125 | 15544 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 950.0 | 7.7 / 12.3 / 12.4 | 1.00 | 98% | 0.083 | 23316 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 955.9 | 7.7 / 12.3 / 12.4 | 1.00 | 100% | 0.062 | 31088 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 601.4 | 40.9 / 45.5 / 45.6 | 1.00 | 56% | 1.000 | 1943 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 901.4 | 342.9 / 369.5 / 376.0 | 1.00 | 100% | 0.500 | 1943 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 921.1 | 348.9 / 408.0 / 416.6 | 2.00 | 100% | 0.250 | 1943 |
| Neura, deadline + fairness | 8 | 292.90 | 885.0 | 0.0 | 472.7 | 324.7 / 495.9 / 577.2 | 2.84 | 99% | 0.125 | 1943 |
| Neura, deadline + fairness | 12 | 276.33 | 1092.5 | 2.5 | 422.2 | 270.4 / 561.5 / 595.9 | 5.33 | 100% | 0.083 | 2051 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 601.4 | 40.9 / 45.5 / 45.6 | 1.00 | 56% | 1.000 | 3886 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 634.2 | 39.4 / 45.2 / 45.4 | 1.66 | 60% | 0.500 | 3886 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 675.3 | 37.8 / 44.5 / 45.5 | 3.14 | 64% | 0.250 | 3886 |
| Neura, lanes + lined-up requests | 8 | 0.00 | 0.0 | 0.0 | 799.1 | 36.7 / 40.3 / 45.4 | 7.58 | 76% | 0.125 | 3886 |
| Neura, lanes + lined-up requests | 12 | 0.00 | 0.0 | 0.0 | 885.4 | 36.7 / 287.8 / 368.4 | 5.81 | 93% | 0.083 | 4102 |
| Neura, lanes + lined-up requests | 16 | 14.47 | 54.4 | 0.0 | 914.5 | 179.2 / 397.7 / 507.7 | 5.23 | 100% | 0.062 | 4318 |
| Neura, lanes + lined-up requests | 24 | 186.24 | 716.2 | 15.0 | 667.8 | 209.0 / 574.5 / 590.9 | 6.97 | 100% | 0.042 | 4318 |

### Same weights on every robot: pi0: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 550.0 | 8.0 / 12.3 / 12.3 | 1.00 | 55% | 1.000 | 10597 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 550.0 | 8.0 / 12.3 / 12.3 | 1.00 | 55% | 1.000 | 10597 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 565.3 | 7.9 / 12.4 / 12.4 | 1.00 | 100% | 0.500 | 21194 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 741.7 | 7.7 / 12.3 / 12.4 | 1.00 | 100% | 0.250 | 42388 |
| Dedicated: one process per robot, one device | 8 | 662.95 | 0.0 | 0.0 | 958.2 | 7.7 / 12.3 / 12.4 | 1.00 | 100% | 0.125 | 84776 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 600.0 | 40.6 / 45.5 / 45.6 | 1.00 | 55% | 1.000 | 10597 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 889.2 | 336.4 / 363.2 / 367.3 | 1.00 | 100% | 0.500 | 10597 |
| Neura, deadline + fairness | 4 | 240.00 | 772.5 | 0.0 | 536.3 | 326.8 / 513.5 / 573.8 | 1.52 | 99% | 0.250 | 10597 |
| Neura, deadline + fairness | 8 | 307.85 | 1072.5 | 240.0 | 368.4 | 392.2 / 566.5 / 593.4 | 3.51 | 100% | 0.125 | 10597 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 600.0 | 40.6 / 45.5 / 45.6 | 1.00 | 55% | 1.000 | 21194 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 700.1 | 39.1 / 44.7 / 45.5 | 1.94 | 66% | 0.500 | 21194 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 884.9 | 37.6 / 45.0 / 45.7 | 3.14 | 86% | 0.250 | 21194 |
| Neura, lanes + lined-up requests | 8 | 238.89 | 821.2 | 123.8 | 608.1 | 249.8 / 564.9 / 595.9 | 2.34 | 100% | 0.125 | 21194 |
| Neura, lanes + lined-up requests | 12 | 295.47 | 917.5 | 350.0 | 482.7 | 216.2 / 574.5 / 597.8 | 3.39 | 100% | 0.083 | 21344 |

### Same weights on every robot: pi0: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 550.0 | 8.0 / 12.3 / 12.3 | 1.00 | 55% | 1.000 | 10597 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 550.0 | 8.0 / 12.3 / 12.3 | 1.00 | 55% | 1.000 | 10597 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 648.3 | 8.2 / 12.2 / 12.4 | 1.00 | 67% | 0.500 | 21194 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 816.8 | 7.7 / 12.3 / 12.4 | 1.00 | 87% | 0.250 | 42388 |
| Dedicated: one process per robot, one device | 8 | 792.06 | 0.0 | 0.0 | 957.6 | 7.7 / 12.3 / 12.4 | 1.00 | 100% | 0.125 | 84776 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 600.0 | 40.6 / 45.5 / 45.6 | 1.00 | 55% | 1.000 | 10597 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 891.0 | 334.1 / 361.6 / 362.6 | 1.00 | 100% | 0.500 | 10597 |
| Neura, deadline + fairness | 4 | 232.08 | 810.0 | 0.0 | 511.8 | 320.2 / 567.9 / 590.4 | 1.53 | 99% | 0.250 | 10597 |
| Neura, deadline + fairness | 8 | 308.70 | 1027.5 | 285.0 | 375.2 | 345.9 / 566.9 / 588.5 | 3.48 | 99% | 0.125 | 10597 |
| Neura, lanes + lined-up requests | 1 | 0.00 | 0.0 | 0.0 | 600.0 | 40.6 / 45.5 / 45.6 | 1.00 | 55% | 1.000 | 21194 |
| Neura, lanes + lined-up requests | 2 | 0.00 | 0.0 | 0.0 | 696.2 | 39.3 / 45.4 / 45.7 | 1.71 | 66% | 0.500 | 21194 |
| Neura, lanes + lined-up requests | 4 | 0.00 | 0.0 | 0.0 | 882.7 | 37.5 / 44.8 / 45.6 | 3.02 | 86% | 0.250 | 21194 |
| Neura, lanes + lined-up requests | 8 | 246.53 | 866.2 | 120.0 | 595.3 | 261.8 / 570.0 / 592.4 | 2.37 | 100% | 0.125 | 21194 |
| Neura, lanes + lined-up requests | 12 | 270.90 | 902.5 | 355.0 | 503.6 | 229.2 / 575.8 / 599.3 | 3.37 | 100% | 0.083 | 21796 |

## Real-time validation

The production gateway with real worker processes on the device and simulated robots over gRPC, next to the simulation of the same configuration (same robot count, warmup and window). Each cell is real / simulated. With a few hundred requests a p99 is close to the maximum: one collision moves it, so read it with the median and the miss rate. Late ticks: robot control ticks the harness itself ran late (high values mean the harness, not Neura, was overloaded).

| Case | Configuration | Robots | Requests | Misses / 1k | Starvation / robot-h | Age p50 ms | Age p99 ms | Mean batch | Device busy | Late ticks | Worker restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Same weights on every robot: phased arrivals | Dedicated: one device per robot | 1 | 60 / 60 | 0.00 / 0.00 | 0.0 / 0.0 | 6.9 / 7.5 | 14.1 / 12.3 | 1.00 / 1.00 | 4% / 4% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 1 | 60 / 60 | 0.00 / 0.00 | 0.0 / 0.0 | 40.4 / 40.2 | 45.0 / 45.5 | 1.00 / 1.00 | 4% / 4% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 4 | 241 / 240 | 4.15 / 0.00 | 0.0 / 0.0 | 36.9 / 21.3 | 54.5 / 60.5 | 3.93 / 3.64 | 6% / 7% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 8 | 650 / 575 | 261.54 / 165.22 | 0.0 / 0.0 | 32.8 / 23.4 | 107.5 / 73.4 | 2.90 / 2.49 | 16% / 16% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 16 | 1943 / 1513 | 518.27 / 365.50 | 37.5 / 0.0 | 42.4 / 37.3 | 138.4 / 91.0 | 2.49 / 1.98 | 42% / 36% | 1.9% | 0 |
| Same weights on every robot: phased arrivals | Neura, FIFO | 16 | 1973 / 1538 | 519.01 / 375.81 | 67.5 / 0.0 | 44.6 / 16.9 | 130.0 / 95.4 | 2.34 / 2.63 | 43% / 31% | 2.2% | 0 |
| Same weights on every robot: phased arrivals | Dedicated: one process per robot, one device | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 6.7 / 7.7 | 11.5 / 12.3 | 1.00 / 1.00 | 57% / 54% | 0.0% | 0 |
| Same weights on every robot: synchronized (bursty) arrivals | Neura, deadline + fairness | 16 | 1790 / 1584 | 471.51 / 393.94 | 0.0 / 0.0 | 41.3 / 33.7 | 143.7 / 87.4 | 2.47 / 2.64 | 40% / 31% | 1.7% | 0 |
| Same weights on every robot: phased, with a background GPU job | Neura, deadline + fairness | 16 | 2530 / 1578 | 650.99 / 391.63 | 603.7 / 0.0 | 56.0 / 16.4 | 129.2 / 85.5 | 2.21 / 2.34 | 66% / 38% | 0.3% | 0 |
| Same weights on every robot: SmolVLA: phased arrivals | Neura, deadline + fairness | 4 | 360 / 305 | 0.00 / 3.28 | 0.0 / 15.0 | 46.9 / 348.2 | 55.6 / 405.2 | 4.00 / 1.99 | 64% / 100% | 0.0% | 0 |
| Same weights on every robot: SmolVLA: phased arrivals | Neura, lanes + lined-up requests | 4 | 360 / 360 | 0.00 / 0.00 | 0.0 / 0.0 | 44.0 / 37.6 | 50.2 / 44.5 | 4.00 / 3.27 | 65% / 64% | 0.0% | 0 |
| Same weights on every robot: pi0: phased arrivals | Neura, deadline + fairness | 4 | 335 / 301 | 44.78 / 252.49 | 240.0 / 780.0 | 47.2 / 332.3 | 539.4 / 497.2 | 3.19 / 1.47 | 89% / 100% | 0.0% | 0 |
| Same weights on every robot: pi0: phased arrivals | Neura, lanes + lined-up requests | 4 | 360 / 360 | 0.00 / 0.00 | 0.0 / 0.0 | 46.9 / 37.9 | 67.6 / 44.8 | 4.00 / 3.21 | 86% / 86% | 0.0% | 0 |
| Same weights on every robot: SmolVLA: phased arrivals | Dedicated: one process per robot, one device | 2 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 7.0 / 7.7 | 10.6 / 12.3 | 1.00 / 1.00 | 100% / 100% | 0.0% | 0 |
| Same weights on every robot: pi0: phased arrivals | Dedicated: one process per robot, one device | 2 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 7.1 / 7.7 | 10.1 / 12.3 | 1.00 / 1.00 | 100% / 100% | 0.0% | 0 |

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
neura-bench run --preset vla --device cuda:0 --size base --out /tmp/neura-bench-3j3_ne8w/report
```
