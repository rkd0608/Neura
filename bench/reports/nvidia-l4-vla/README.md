# Neura GPU-sharing benchmark: NVIDIA L4

Generated 2026-10-17T18:44:03+00:00 from commit `9fa7f8e` with `neura-bench run --preset vla --device cuda:0 --size base --out /tmp/neura-bench-08_3ki8a/report` (torch 2.11.0, LeRobot 0.6.1). Regenerate the Markdown from `results.json` with `neura-bench report`.

Models: synthetic checkpoints with the real LeRobot architectures and random weights (ACT, Diffusion Policy, SmolVLA, pi0; size `base`, cameras 640 x 480, bf16). Latency, memory and batching depend on architecture and shapes, not on what weights learned; closed-loop task success needs trained weights and is not measured here. SmolVLA reads its backbone's configuration and processor from the Hub, not its weights; pi0 is LeRobot's (PaliGemma with Gemma 2B, and a 300M action expert), kept in bfloat16 as LeRobot runs it, its instruction tokenized by a public tokenizer in place of PaliGemma's gated one and padded to the same 48 tokens, so the model computes the same shapes. Their profile here: chunks of 50 actions, the next requested at 30 left, an observation at most 600 ms old when it reaches the model and 1500 ms when the robot accepts its chunk (the example profile's 100 and 150 ms cannot be met by a pass of about 0.4 s).

## Robots per device at the SLO

SLO: at most 1.0 deadline misses per 1,000 requests, at most 0.0 starvation events per robot-hour, no robot faults, and no more device memory than the device has (23034 MiB: every worker process's peak allocation plus its measured overhead, the CUDA context and caches). A configuration's capacity is the largest robot count at which it and every smaller count measured met the SLO (simulation with measured latency and contention, below).

| Case | Workload | One device per robot | One process per robot | Best Neura |
| --- | --- | --- | --- | --- |
| Same weights on every robot | phased arrivals | 1 | 96, fails at 128 | 32, fails at 48 (Neura, deadline + fairness) |
| Same weights on every robot | synchronized (bursty) arrivals | 1 | 24, fails at 32 | 24, fails at 32 (Neura, FIFO) |
| Same weights on every robot | phased, with a background GPU job | 1 | 96, fails at 128 | 32, fails at 48 (Neura, deadline + fairness) |
| Same weights on every robot: SmolVLA | phased arrivals | 1 | 12, fails at 16 | 1, fails at 2 (Neura, deadline + fairness) |
| Same weights on every robot: SmolVLA | synchronized (bursty) arrivals | 1 | 12, fails at 16 | 1, fails at 2 (Neura, deadline + fairness) |
| Same weights on every robot: pi0 | phased arrivals | 1 | 2, fails at 4 | 2, fails at 4 (Neura, deadline + fairness) |
| Same weights on every robot: pi0 | synchronized (bursty) arrivals | 1 | 2, fails at 4 | 2, fails at 4 (Neura, deadline + fairness) |

- **Same weights on every robot:** sharing a device pays, but here one process per robot serves as many or more (96 against 32): on this device batching does not beat co-location. Neura, deadline + fairness serves 32 robots per device (0.031 device-hours per robot-hour, against 1 with a device per robot); at 48 robots it misses the SLO, so the crossover where one device stops paying is between 32 and 48.
- **Same weights on every robot: SmolVLA:** sharing does not pay on this device: Neura, deadline + fairness serves 1 robot(s) at the SLO.
- **Same weights on every robot: pi0:** sharing a device pays, but here one process per robot serves as many or more (2 against 2): on this device batching does not beat co-location. Neura, deadline + fairness serves 2 robots per device (0.500 device-hours per robot-hour, against 1 with a device per robot); at 4 robots it misses the SLO, so the crossover where one device stops paying is between 2 and 4.

## Measurements on the device

Forward-pass latency through a worker process, ACT:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 35.84 | 41.75 | 198 | 275 |
| 2 | 45.61 | 55.46 | 228 | 215 |
| 4 | 66.53 | 85.08 | 338 | 148 |
| 8 | 136.58 | 151.36 | 553 | 74 |
| 16 | 320.24 | 356.16 | 986 | 31 |

Forward-pass latency through a worker process, Diffusion Policy:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 125.50 | 134.28 | 1051 | 80 |
| 2 | 135.87 | 171.14 | 1051 | 72 |
| 4 | 161.90 | 171.38 | 1051 | 62 |
| 8 | 193.50 | 221.39 | 1051 | 52 |
| 16 | 302.42 | 344.55 | 1118 | 33 |

Forward-pass latency through a worker process, SmolVLA:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 399.94 | 471.07 | 1165 | 25 |
| 2 | 417.22 | 425.49 | 1165 | 25 |
| 4 | 455.50 | 493.76 | 1165 | 22 |
| 8 | 535.67 | 585.80 | 1165 | 20 |
| 16 | 772.62 | 823.06 | 1381 | 20 |

Forward-pass latency through a worker process, pi0:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 377.58 | 413.35 | 8482 | 27 |
| 2 | 447.77 | 474.06 | 8482 | 23 |
| 4 | 587.54 | 619.57 | 8482 | 20 |
| 8 | 904.76 | 932.35 | 8482 | 20 |
| 16 | 1751.68 | 1769.54 | 9084 | 20 |

The driver reported no device memory for the worker processes of ACT, Diffusion Policy, SmolVLA, pi0, so each process counts here at its peak allocation alone, without its CUDA context and caches: the memory limit of one process per robot is optimistic.

Co-location: k workers on the device, each running batches of one back to back. Alone: p50 37.056 ms. A background job (matrix multiplications) slows a lone worker by 1.068x.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 37.06 | 71.08 |
| 2 | 0.98 | 36.35 | 47.10 |
| 4 | 1.04 | 38.59 | 47.43 |
| 8 | 1.68 | 62.38 | 67.94 |
| 16 | 3.38 | 125.30 | 140.48 |
| 24 | 5.05 | 187.33 | 439.10 |

Co-location of SmolVLA workers (simulated with this, not ACT's). Alone: p50 382.778 ms.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 382.78 | 394.92 |
| 2 | 1.00 | 383.82 | 421.79 |
| 4 | 1.04 | 399.74 | 437.39 |
| 8 | 1.38 | 527.52 | 569.78 |

Co-location of pi0 workers (simulated with this, not ACT's). Alone: p50 378.077 ms.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 378.08 | 389.13 |
| 2 | 1.19 | 448.83 | 488.97 |

Simulated link, calibrated from one robot alone in real time: observation age at dispatch p50 8.011 ms, p99 13.646 ms (robot encoding, gRPC and the gateway's checks), simulated as 2.261 ms plus up to 11.5 ms.

Multi-adapter batching: 16 rank-16 LoRA adapters on 13 linear layers of the ACT base model; every request in a batch uses a different adapter. Parity with each adapter merged and run alone: max relative error 6.69e-05 (tolerance 1e-04), passed.

| Batch | Base p50 (ms) | Mixed adapters p50 (ms) | Overhead |
| --- | --- | --- | --- |
| 1 | 21.34 | 24.55 | 1.15 |
| 2 | 26.60 | 31.04 | 1.17 |
| 4 | 46.23 | 52.94 | 1.15 |
| 8 | 94.15 | 106.86 | 1.14 |
| 16 | 203.47 | 224.90 | 1.10 |

Admission control's estimate for ACT on this device: 3 robots (limited by acceptance_freshness), against 16 measured with synchronized arrivals. Admission assumes the worst case (every robot asks at once, behind a batch already running), so it must not exceed the synchronized capacity: it does not (3 <= 16).

For SmolVLA: 3 robots (limited by deadline), against 1 measured with synchronized arrivals: it does (3 > 1), so admission is optimistic on this device.

For pi0: 2 robots (limited by deadline), against 2 measured with synchronized arrivals: it does not (2 <= 2).

## Results

Per 1,000 requests: deadline misses (served after the predicted starvation time or rejected by the robot as too old, too old to dispatch, or failed). Per robot-hour: starvation events and faults. Age: from the robot's capture to the start of the forward pass.

### Same weights on every robot: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.2 / 13.4 / 13.6 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.2 / 13.4 / 13.6 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 8.2 / 13.6 / 13.7 | 1.00 | 7% | 0.500 | 396 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 8.0 / 13.7 / 13.7 | 1.00 | 14% | 0.250 | 792 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 8.5 / 13.7 / 13.8 | 1.00 | 29% | 0.125 | 1584 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.4 | 8.7 / 13.7 / 13.8 | 1.00 | 43% | 0.083 | 2376 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.4 | 8.3 / 13.6 / 13.7 | 1.00 | 57% | 0.062 | 3168 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 8.2 / 13.7 / 13.8 | 1.00 | 85% | 0.042 | 4752 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.4 | 8.4 / 13.7 / 13.8 | 1.00 | 98% | 0.031 | 6336 |
| Dedicated: one process per robot, one device | 48 | 0.00 | 0.0 | 0.0 | 33.4 | 8.4 / 13.7 / 13.8 | 1.00 | 100% | 0.021 | 9504 |
| Dedicated: one process per robot, one device | 64 | 0.00 | 0.0 | 0.0 | 33.4 | 8.3 / 13.7 / 13.8 | 1.00 | 100% | 0.016 | 12672 |
| Dedicated: one process per robot, one device | 96 | 0.00 | 0.0 | 0.0 | 33.6 | 8.3 / 13.6 / 13.8 | 1.00 | 100% | 0.010 | 19008 |
| Dedicated: one process per robot, one device | 128 | 0.00 | 0.0 | 0.0 | 63.5 | 8.4 / 13.7 / 13.8 | 1.00 | 100% | 0.008 | 25344 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 33.6 | 10.2 / 15.4 / 15.6 | 1.00 | 4% | 1.000 | 198 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 10.2 / 15.7 / 15.7 | 1.00 | 7% | 0.500 | 198 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 33.4 | 10.4 / 15.7 / 15.7 | 1.00 | 14% | 0.250 | 198 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 33.6 | 10.6 / 15.7 / 15.8 | 1.00 | 29% | 0.125 | 198 |
| Neura, FIFO | 12 | 0.00 | 0.0 | 0.0 | 33.7 | 10.8 / 15.6 / 15.8 | 1.00 | 43% | 0.083 | 198 |
| Neura, FIFO | 16 | 0.00 | 0.0 | 0.0 | 33.7 | 10.7 / 20.4 / 28.1 | 1.00 | 57% | 0.062 | 198 |
| Neura, FIFO | 24 | 0.00 | 0.0 | 0.0 | 33.6 | 10.6 / 15.6 / 16.6 | 1.00 | 86% | 0.042 | 198 |
| Neura, FIFO | 32 | 0.00 | 0.0 | 0.0 | 55.6 | 22.6 / 51.4 / 57.4 | 1.27 | 97% | 0.031 | 283 |
| Neura, FIFO | 48 | 708.28 | 625.0 | 126.2 | 60.3 | 78.0 / 97.5 / 102.0 | 2.14 | 100% | 0.021 | 499 |
| Neura, FIFO | 64 | 850.57 | 758.9 | 415.3 | 28.5 | 83.4 / 97.4 / 102.0 | 3.04 | 100% | 0.016 | 823 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 33.6 | 10.2 / 15.4 / 15.6 | 1.00 | 4% | 1.000 | 198 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 10.2 / 15.7 / 15.7 | 1.00 | 7% | 0.500 | 198 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 33.4 | 10.4 / 15.7 / 15.7 | 1.00 | 14% | 0.250 | 198 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 33.6 | 10.6 / 15.7 / 15.8 | 1.00 | 29% | 0.125 | 198 |
| Neura, round robin | 12 | 0.00 | 0.0 | 0.0 | 33.7 | 10.8 / 15.6 / 15.8 | 1.00 | 43% | 0.083 | 198 |
| Neura, round robin | 16 | 0.00 | 0.0 | 0.0 | 33.7 | 10.7 / 20.4 / 28.1 | 1.00 | 57% | 0.062 | 198 |
| Neura, round robin | 24 | 0.00 | 0.0 | 0.0 | 33.6 | 10.6 / 15.6 / 16.6 | 1.00 | 86% | 0.042 | 198 |
| Neura, round robin | 32 | 0.00 | 0.0 | 0.0 | 55.6 | 22.6 / 51.4 / 57.4 | 1.27 | 97% | 0.031 | 283 |
| Neura, round robin | 48 | 283.01 | 56.2 | 3.1 | 80.1 | 39.3 / 94.1 / 99.7 | 2.31 | 100% | 0.021 | 391 |
| Neura, round robin | 64 | 691.53 | 227.8 | 50.2 | 63.0 | 37.0 / 94.8 / 102.2 | 2.79 | 100% | 0.016 | 499 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 33.6 | 10.2 / 15.4 / 15.6 | 1.00 | 4% | 1.000 | 198 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 10.2 / 15.7 / 15.7 | 1.00 | 7% | 0.500 | 198 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 33.4 | 10.4 / 15.7 / 15.7 | 1.00 | 14% | 0.250 | 198 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 33.6 | 10.6 / 15.7 / 15.8 | 1.00 | 29% | 0.125 | 198 |
| Neura, deadline + fairness | 12 | 0.00 | 0.0 | 0.0 | 33.7 | 10.8 / 15.6 / 15.8 | 1.00 | 43% | 0.083 | 198 |
| Neura, deadline + fairness | 16 | 0.00 | 0.0 | 0.0 | 33.7 | 10.7 / 20.4 / 28.1 | 1.00 | 57% | 0.062 | 198 |
| Neura, deadline + fairness | 24 | 0.00 | 0.0 | 0.0 | 33.6 | 10.6 / 15.6 / 16.6 | 1.00 | 86% | 0.042 | 198 |
| Neura, deadline + fairness | 32 | 0.00 | 0.0 | 0.0 | 55.6 | 22.6 / 51.4 / 57.4 | 1.27 | 97% | 0.031 | 283 |
| Neura, deadline + fairness | 48 | 269.58 | 0.0 | 0.0 | 80.8 | 37.9 / 92.8 / 100.0 | 2.33 | 100% | 0.021 | 391 |
| Neura, deadline + fairness | 64 | 759.69 | 1195.3 | 7.5 | 25.3 | 50.0 / 95.5 / 103.2 | 2.30 | 100% | 0.016 | 391 |

### Same weights on every robot: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.2 / 13.4 / 13.6 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.2 / 13.4 / 13.6 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 13.7 / 13.8 | 1.00 | 4% | 0.500 | 396 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.5 | 8.0 / 13.6 / 13.8 | 1.00 | 4% | 0.250 | 792 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 34.7 | 8.4 / 13.6 / 13.8 | 1.00 | 8% | 0.125 | 1584 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 60.1 | 8.3 / 13.6 / 13.8 | 1.00 | 10% | 0.083 | 2376 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 88.8 | 8.2 / 13.7 / 13.7 | 1.00 | 13% | 0.062 | 3168 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 59.1 | 8.3 / 13.6 / 13.8 | 1.00 | 26% | 0.042 | 4752 |
| Dedicated: one process per robot, one device | 32 | 999.76 | 0.0 | 0.0 | 0.0 | 37.4 / 90.6 / 99.8 | 1.00 | 100% | 0.031 | 6336 |
| Dedicated: one process per robot, one device | 48 | 999.27 | 0.0 | 0.0 | 0.0 | 9.8 / 94.7 / 103.0 | 1.00 | 100% | 0.021 | 9504 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 33.6 | 10.2 / 15.4 / 15.6 | 1.00 | 4% | 1.000 | 198 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 33.9 | 12.4 / 20.3 / 22.1 | 1.00 | 7% | 0.500 | 198 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 66.1 | 24.7 / 61.9 / 70.2 | 1.74 | 10% | 0.250 | 283 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 50.4 | 15.7 / 53.4 / 64.1 | 1.30 | 24% | 0.125 | 283 |
| Neura, FIFO | 12 | 0.00 | 0.0 | 0.0 | 52.5 | 15.4 / 49.5 / 60.7 | 1.44 | 33% | 0.083 | 283 |
| Neura, FIFO | 16 | 0.52 | 0.0 | 0.0 | 53.7 | 13.8 / 56.6 / 71.3 | 1.47 | 44% | 0.062 | 715 |
| Neura, FIFO | 24 | 0.69 | 0.0 | 0.0 | 60.5 | 16.0 / 69.8 / 87.4 | 1.60 | 63% | 0.042 | 715 |
| Neura, FIFO | 32 | 1.04 | 0.0 | 0.0 | 63.3 | 25.7 / 72.8 / 93.4 | 1.54 | 86% | 0.031 | 986 |
| Neura, FIFO | 48 | 680.51 | 562.5 | 128.8 | 60.9 | 72.2 / 96.5 / 102.8 | 2.06 | 100% | 0.021 | 823 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 33.6 | 10.2 / 15.4 / 15.6 | 1.00 | 4% | 1.000 | 198 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 33.9 | 12.4 / 20.3 / 22.1 | 1.00 | 7% | 0.500 | 198 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 66.1 | 24.7 / 61.9 / 70.2 | 1.74 | 10% | 0.250 | 283 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 50.4 | 15.7 / 53.4 / 64.1 | 1.30 | 24% | 0.125 | 283 |
| Neura, round robin | 12 | 0.00 | 0.0 | 0.0 | 52.5 | 15.4 / 49.5 / 60.7 | 1.44 | 33% | 0.083 | 283 |
| Neura, round robin | 16 | 0.00 | 0.0 | 0.0 | 58.3 | 14.8 / 59.6 / 70.5 | 1.60 | 42% | 0.062 | 715 |
| Neura, round robin | 24 | 0.35 | 0.0 | 0.0 | 65.9 | 17.9 / 76.0 / 90.7 | 1.75 | 60% | 0.042 | 715 |
| Neura, round robin | 32 | 2.34 | 0.0 | 0.0 | 63.2 | 18.8 / 70.0 / 96.0 | 1.68 | 81% | 0.031 | 986 |
| Neura, round robin | 48 | 259.31 | 33.8 | 1.2 | 80.9 | 37.4 / 92.5 / 101.4 | 2.34 | 100% | 0.021 | 445 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 33.6 | 10.2 / 15.4 / 15.6 | 1.00 | 4% | 1.000 | 198 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 33.9 | 12.4 / 20.3 / 22.1 | 1.00 | 7% | 0.500 | 198 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 66.1 | 24.7 / 61.9 / 70.2 | 1.74 | 10% | 0.250 | 283 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 49.5 | 15.4 / 51.3 / 64.3 | 1.32 | 24% | 0.125 | 283 |
| Neura, deadline + fairness | 12 | 0.00 | 0.0 | 0.0 | 52.5 | 15.4 / 49.5 / 60.7 | 1.44 | 33% | 0.083 | 283 |
| Neura, deadline + fairness | 16 | 0.00 | 0.0 | 0.0 | 59.2 | 19.9 / 59.2 / 98.2 | 1.51 | 43% | 0.062 | 715 |
| Neura, deadline + fairness | 24 | 1.73 | 0.0 | 0.0 | 65.0 | 24.6 / 74.1 / 91.3 | 1.71 | 60% | 0.042 | 715 |
| Neura, deadline + fairness | 32 | 1.82 | 0.0 | 0.0 | 66.1 | 23.7 / 73.3 / 93.2 | 1.68 | 82% | 0.031 | 986 |

### Same weights on every robot: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.9 | 8.2 / 13.4 / 13.6 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.9 | 8.2 / 13.4 / 13.6 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.5 | 8.3 / 13.6 / 13.7 | 1.00 | 8% | 0.500 | 396 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.5 | 8.3 / 13.7 / 13.7 | 1.00 | 15% | 0.250 | 792 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.4 | 8.5 / 13.7 / 13.7 | 1.00 | 31% | 0.125 | 1584 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.5 | 8.4 / 13.6 / 13.8 | 1.00 | 46% | 0.083 | 2376 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.6 | 8.5 / 13.6 / 13.7 | 1.00 | 60% | 0.062 | 3168 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.7 | 8.4 / 13.6 / 13.8 | 1.00 | 90% | 0.042 | 4752 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.6 | 8.3 / 13.7 / 13.8 | 1.00 | 99% | 0.031 | 6336 |
| Dedicated: one process per robot, one device | 48 | 0.00 | 0.0 | 0.0 | 33.7 | 8.5 / 13.6 / 13.8 | 1.00 | 100% | 0.021 | 9504 |
| Dedicated: one process per robot, one device | 64 | 0.00 | 0.0 | 0.0 | 33.7 | 8.5 / 13.7 / 13.8 | 1.00 | 100% | 0.016 | 12672 |
| Dedicated: one process per robot, one device | 96 | 0.00 | 0.0 | 0.0 | 37.7 | 8.4 / 13.7 / 13.8 | 1.00 | 100% | 0.010 | 19008 |
| Dedicated: one process per robot, one device | 128 | 351.24 | 5.6 | 5.6 | 0.0 | 11.8 / 91.8 / 100.3 | 1.00 | 100% | 0.008 | 25344 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 33.9 | 10.2 / 15.4 / 15.6 | 1.00 | 4% | 1.000 | 198 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 33.6 | 10.2 / 15.7 / 15.7 | 1.00 | 8% | 0.500 | 198 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 34.6 | 10.3 / 15.6 / 15.7 | 1.00 | 15% | 0.250 | 198 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 34.4 | 10.4 / 15.6 / 15.8 | 1.00 | 31% | 0.125 | 198 |
| Neura, FIFO | 12 | 0.00 | 0.0 | 0.0 | 34.2 | 10.6 / 15.7 / 15.7 | 1.00 | 46% | 0.083 | 198 |
| Neura, FIFO | 16 | 0.00 | 0.0 | 0.0 | 35.1 | 10.7 / 22.5 / 28.4 | 1.00 | 61% | 0.062 | 198 |
| Neura, FIFO | 24 | 0.00 | 0.0 | 0.0 | 34.4 | 11.3 / 16.2 / 19.1 | 1.00 | 92% | 0.042 | 198 |
| Neura, FIFO | 32 | 0.00 | 0.0 | 0.0 | 63.9 | 28.7 / 52.0 / 68.2 | 1.37 | 99% | 0.031 | 283 |
| Neura, FIFO | 48 | 780.21 | 805.6 | 284.4 | 42.8 | 80.1 / 97.2 / 102.1 | 2.60 | 100% | 0.021 | 661 |
| Neura, FIFO | 64 | 840.12 | 582.2 | 404.1 | 18.8 | 84.3 / 98.3 / 104.0 | 3.87 | 100% | 0.016 | 986 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 33.9 | 10.2 / 15.4 / 15.6 | 1.00 | 4% | 1.000 | 198 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 33.6 | 10.2 / 15.7 / 15.7 | 1.00 | 8% | 0.500 | 198 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 34.6 | 10.3 / 15.6 / 15.7 | 1.00 | 15% | 0.250 | 198 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 34.4 | 10.4 / 15.6 / 15.8 | 1.00 | 31% | 0.125 | 198 |
| Neura, round robin | 12 | 0.00 | 0.0 | 0.0 | 34.2 | 10.6 / 15.7 / 15.7 | 1.00 | 46% | 0.083 | 198 |
| Neura, round robin | 16 | 0.00 | 0.0 | 0.0 | 35.1 | 10.7 / 22.5 / 28.4 | 1.00 | 61% | 0.062 | 198 |
| Neura, round robin | 24 | 0.00 | 0.0 | 0.0 | 34.4 | 11.3 / 16.2 / 19.1 | 1.00 | 92% | 0.042 | 198 |
| Neura, round robin | 32 | 0.00 | 0.0 | 0.0 | 64.0 | 28.7 / 52.0 / 68.2 | 1.37 | 99% | 0.031 | 283 |
| Neura, round robin | 48 | 486.43 | 186.9 | 27.5 | 74.8 | 38.8 / 94.1 / 100.6 | 2.48 | 100% | 0.021 | 391 |
| Neura, round robin | 64 | 768.12 | 324.4 | 204.4 | 54.8 | 38.7 / 95.0 / 101.2 | 2.57 | 100% | 0.016 | 553 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 33.9 | 10.2 / 15.4 / 15.6 | 1.00 | 4% | 1.000 | 198 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 33.6 | 10.2 / 15.7 / 15.7 | 1.00 | 8% | 0.500 | 198 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 34.6 | 10.3 / 15.6 / 15.7 | 1.00 | 15% | 0.250 | 198 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 34.4 | 10.4 / 15.6 / 15.8 | 1.00 | 31% | 0.125 | 198 |
| Neura, deadline + fairness | 12 | 0.00 | 0.0 | 0.0 | 34.2 | 10.6 / 15.7 / 15.7 | 1.00 | 46% | 0.083 | 198 |
| Neura, deadline + fairness | 16 | 0.00 | 0.0 | 0.0 | 35.1 | 10.7 / 22.5 / 28.4 | 1.00 | 61% | 0.062 | 198 |
| Neura, deadline + fairness | 24 | 0.00 | 0.0 | 0.0 | 34.4 | 11.3 / 16.2 / 19.1 | 1.00 | 92% | 0.042 | 198 |
| Neura, deadline + fairness | 32 | 0.00 | 0.0 | 0.0 | 64.0 | 28.7 / 52.0 / 68.2 | 1.37 | 99% | 0.031 | 283 |
| Neura, deadline + fairness | 48 | 485.10 | 0.0 | 0.0 | 77.8 | 36.8 / 93.9 / 101.1 | 2.53 | 100% | 0.021 | 391 |
| Neura, deadline + fairness | 64 | 814.81 | 1276.4 | 330.5 | 1.5 | 78.7 / 97.5 / 101.9 | 2.83 | 100% | 0.016 | 986 |

### Same weights on every robot: SmolVLA: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 601.9 | 8.2 / 13.6 / 13.7 | 1.00 | 60% | 1.000 | 1165 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 601.9 | 8.2 / 13.6 / 13.7 | 1.00 | 60% | 1.000 | 1165 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 602.6 | 8.3 / 13.7 / 13.8 | 1.00 | 100% | 0.500 | 2330 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 608.9 | 8.5 / 13.6 / 13.7 | 1.00 | 100% | 0.250 | 4660 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 719.2 | 8.2 / 13.6 / 13.7 | 1.00 | 100% | 0.125 | 9320 |
| Dedicated: one process per robot, one device | 12 | 0.49 | 0.0 | 0.0 | 942.2 | 8.4 / 13.7 / 13.8 | 1.00 | 100% | 0.083 | 13980 |
| Dedicated: one process per robot, one device | 16 | 293.41 | 354.4 | 1.9 | 806.6 | 8.8 / 90.3 / 215.6 | 1.00 | 100% | 0.062 | 18640 |
| Dedicated: one process per robot, one device | 24 | 361.36 | 788.8 | 466.2 | 357.7 | 10.2 / 409.7 / 542.0 | 1.00 | 100% | 0.042 | 27960 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 603.1 | 10.2 / 15.6 / 15.7 | 1.00 | 60% | 1.000 | 1165 |
| Neura, deadline + fairness | 2 | 239.20 | 90.0 | 0.0 | 912.7 | 369.5 / 438.3 / 489.1 | 1.01 | 100% | 0.500 | 1165 |
| Neura, deadline + fairness | 4 | 516.64 | 412.5 | 0.0 | 772.3 | 379.5 / 448.8 / 456.8 | 2.01 | 98% | 0.250 | 1165 |

### Same weights on every robot: SmolVLA: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 601.9 | 8.2 / 13.6 / 13.7 | 1.00 | 60% | 1.000 | 1165 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 601.9 | 8.2 / 13.6 / 13.7 | 1.00 | 60% | 1.000 | 1165 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 606.5 | 7.7 / 13.7 / 13.8 | 1.00 | 67% | 0.500 | 2330 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 622.1 | 8.2 / 13.7 / 13.8 | 1.00 | 69% | 0.250 | 4660 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 801.8 | 8.3 / 13.6 / 13.8 | 1.00 | 97% | 0.125 | 9320 |
| Dedicated: one process per robot, one device | 12 | 0.49 | 0.0 | 0.0 | 942.4 | 8.5 / 13.7 / 13.8 | 1.00 | 100% | 0.083 | 13980 |
| Dedicated: one process per robot, one device | 16 | 299.60 | 333.8 | 0.0 | 817.0 | 8.9 / 80.0 / 168.8 | 1.00 | 100% | 0.062 | 18640 |
| Dedicated: one process per robot, one device | 24 | 377.33 | 793.8 | 468.8 | 363.2 | 10.4 / 400.8 / 512.5 | 1.00 | 100% | 0.042 | 27960 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 603.1 | 10.2 / 15.6 / 15.7 | 1.00 | 60% | 1.000 | 1165 |
| Neura, deadline + fairness | 2 | 254.18 | 90.0 | 0.0 | 912.7 | 372.6 / 447.5 / 471.7 | 1.01 | 99% | 0.500 | 1165 |
| Neura, deadline + fairness | 4 | 439.07 | 517.5 | 0.0 | 711.0 | 376.6 / 462.7 / 492.6 | 2.00 | 97% | 0.250 | 1165 |

### Same weights on every robot: pi0: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 567.5 | 8.5 / 13.6 / 13.7 | 1.00 | 57% | 1.000 | 8482 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 567.5 | 8.5 / 13.6 / 13.7 | 1.00 | 57% | 1.000 | 8482 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 594.2 | 7.9 / 13.7 / 13.7 | 1.00 | 100% | 0.500 | 16964 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 806.5 | 8.3 / 13.7 / 13.7 | 1.00 | 100% | 0.250 | 33928 |
| Dedicated: one process per robot, one device | 8 | 375.13 | 967.5 | 0.0 | 479.8 | 10.2 / 133.5 / 203.1 | 1.00 | 98% | 0.125 | 67856 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 567.8 | 10.6 / 15.5 / 15.7 | 1.00 | 57% | 1.000 | 8482 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 955.6 | 353.8 / 397.8 / 398.7 | 1.00 | 100% | 0.500 | 8482 |
| Neura, deadline + fairness | 4 | 267.99 | 907.5 | 22.5 | 488.8 | 313.9 / 525.1 / 565.1 | 1.81 | 99% | 0.250 | 8482 |
| Neura, deadline + fairness | 8 | 295.15 | 990.0 | 240.0 | 304.6 | 320.8 / 582.2 / 591.3 | 3.25 | 99% | 0.125 | 8482 |

### Same weights on every robot: pi0: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 567.5 | 8.5 / 13.6 / 13.7 | 1.00 | 57% | 1.000 | 8482 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 567.5 | 8.5 / 13.6 / 13.7 | 1.00 | 57% | 1.000 | 8482 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 670.6 | 7.9 / 13.6 / 13.8 | 1.00 | 69% | 0.500 | 16964 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 875.3 | 8.1 / 13.6 / 13.7 | 1.00 | 91% | 0.250 | 33928 |
| Dedicated: one process per robot, one device | 8 | 371.10 | 937.5 | 0.0 | 487.5 | 10.0 / 139.6 / 206.1 | 1.00 | 98% | 0.125 | 67856 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 567.8 | 10.6 / 15.5 / 15.7 | 1.00 | 57% | 1.000 | 8482 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 956.1 | 353.8 / 398.2 / 408.2 | 1.00 | 100% | 0.500 | 8482 |
| Neura, deadline + fairness | 4 | 282.19 | 900.0 | 0.0 | 514.5 | 321.0 / 536.3 / 594.2 | 1.82 | 99% | 0.250 | 8482 |
| Neura, deadline + fairness | 8 | 298.93 | 1005.0 | 195.0 | 301.8 | 331.5 / 583.2 / 594.8 | 3.26 | 100% | 0.125 | 8482 |

## Real-time validation

The production gateway with real worker processes on the device and simulated robots over gRPC, next to the simulation of the same configuration (same robot count, warmup and window). Each cell is real / simulated. With a few hundred requests a p99 is close to the maximum: one collision moves it, so read it with the median and the miss rate. Late ticks: robot control ticks the harness itself ran late (high values mean the harness, not Neura, was overloaded).

| Case | Configuration | Robots | Requests | Misses / 1k | Starvation / robot-h | Age p50 ms | Age p99 ms | Mean batch | Device busy | Late ticks | Worker restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Same weights on every robot: phased arrivals | Dedicated: one device per robot | 1 | 60 / 60 | 0.00 / 0.00 | 0.0 / 0.0 | 6.9 / 8.0 | 10.2 / 13.4 | 1.00 / 1.00 | 4% / 4% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 1 | 60 / 60 | 0.00 / 0.00 | 0.0 / 0.0 | 9.8 / 10.0 | 24.7 / 15.4 | 1.00 / 1.00 | 4% / 4% | 0.2% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 4 | 240 / 240 | 0.00 / 0.00 | 0.0 / 0.0 | 9.8 / 10.4 | 13.2 / 15.6 | 1.00 / 1.00 | 15% / 14% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 8 | 480 / 480 | 0.00 / 0.00 | 0.0 / 0.0 | 10.1 / 10.8 | 13.1 / 15.7 | 1.00 / 1.00 | 31% / 29% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 10.0 / 10.9 | 31.1 / 20.8 | 1.00 / 1.00 | 60% / 58% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, FIFO | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 9.1 / 10.9 | 24.0 / 20.8 | 1.00 / 1.00 | 57% / 58% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Dedicated: one process per robot, one device | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 6.7 / 8.3 | 9.5 / 13.6 | 1.00 / 1.00 | 57% / 57% | 0.0% | 0 |
| Same weights on every robot: synchronized (bursty) arrivals | Neura, deadline + fairness | 16 | 961 / 960 | 1.04 / 0.00 | 0.0 / 0.0 | 16.8 / 18.8 | 78.0 / 60.2 | 1.71 / 1.51 | 43% / 43% | 0.0% | 0 |
| Same weights on every robot: phased, with a background GPU job | Neura, deadline + fairness | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 9.9 / 10.7 | 26.3 / 22.3 | 1.00 / 1.00 | 67% / 62% | 0.0% | 0 |
| Same weights on every robot: SmolVLA: phased arrivals | Neura, deadline + fairness | 4 | 306 / 285 | 0.00 / 491.23 | 0.0 / 450.0 | 365.6 / 381.6 | 402.5 / 448.7 | 2.00 / 2.01 | 100% / 98% | 0.0% | 0 |
| Same weights on every robot: pi0: phased arrivals | Neura, deadline + fairness | 4 | 290 / 278 | 244.83 / 262.59 | 825.0 / 930.0 | 331.2 / 326.4 | 552.0 / 556.0 | 1.70 / 1.79 | 99% / 100% | 0.0% | 0 |
| Same weights on every robot: SmolVLA: phased arrivals | Dedicated: one process per robot, one device | 2 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 7.0 / 8.1 | 10.9 / 13.7 | 1.00 / 1.00 | 90% / 100% | 0.1% | 0 |
| Same weights on every robot: pi0: phased arrivals | Dedicated: one process per robot, one device | 2 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 7.2 / 8.1 | 10.6 / 13.7 | 1.00 / 1.00 | 100% / 100% | 0.0% | 0 |

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
neura-bench run --preset vla --device cuda:0 --size base --out /tmp/neura-bench-08_3ki8a/report
```
