# Neura GPU-sharing benchmark: NVIDIA L4

Generated 2026-10-17T09:58:25+00:00 from commit `d64d176` with `neura-bench run --preset gpu --device cuda:0 --size base --out /tmp/neura-bench-k8fi7vrn/report` (torch 2.11.0, LeRobot 0.6.1). Regenerate the Markdown from `results.json` with `neura-bench report`.

Models: synthetic checkpoints with the real LeRobot architectures and random weights (ACT, Diffusion Policy, SmolVLA, pi0; size `base`, cameras 640 x 480, bf16). Latency, memory and batching depend on architecture and shapes, not on what weights learned; closed-loop task success needs trained weights and is not measured here. SmolVLA reads its backbone's configuration and processor from the Hub, not its weights; pi0 is LeRobot's (PaliGemma with Gemma 2B, and a 300M action expert), kept in bfloat16 as LeRobot runs it, its instruction tokenized by a public tokenizer in place of PaliGemma's gated one and padded to the same 48 tokens, so the model computes the same shapes.

## Robots per device at the SLO

SLO: at most 1.0 deadline misses per 1,000 requests, at most 0.0 starvation events per robot-hour, no robot faults, and no more device memory than the device has (23034 MiB: every worker process's peak allocation plus its measured overhead, the CUDA context and caches). A configuration's capacity is the largest robot count at which it and every smaller count measured met the SLO (simulation with measured latency and contention, below).

| Case | Workload | One device per robot | One process per robot | Best Neura |
| --- | --- | --- | --- | --- |
| Same weights on every robot | phased arrivals | 1 | 96, fails at 128 | 32, fails at 48 (Neura, deadline + fairness) |
| Same weights on every robot | synchronized (bursty) arrivals | 1 | 96, fails at 128 | 24, fails at 32 (Neura, deadline + fairness) |
| Same weights on every robot | phased, with a background GPU job | 1 | 96, fails at 128 | 32, fails at 48 (Neura, deadline + fairness) |
| Per-robot LoRA adapters of one base model | phased arrivals | 1 | 96, fails at 128 | 24, fails at 32 (Neura, multi-adapter batching) |
| Different full fine-tunes of one architecture | phased arrivals | 1 | 96, fails at 128 | 96, fails at 128 (Neura, co-located groups) |
| Different full fine-tunes of one architecture | phased, with a background GPU job | 1 | 96, fails at 128 | 64, fails at 96 (Neura, co-located groups) |
| Different architectures (ACT and Diffusion Policy) | phased arrivals | 0 (fails at 1) | 1, fails at 2 | 1, fails at 2 (Neura, co-located groups) |
| Same weights on every robot: SmolVLA | phased arrivals | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, deadline + fairness) |
| Same weights on every robot: SmolVLA | synchronized (bursty) arrivals | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, deadline + fairness) |
| Same weights on every robot: pi0 | phased arrivals | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, deadline + fairness) |
| Same weights on every robot: pi0 | synchronized (bursty) arrivals | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, deadline + fairness) |

- **Same weights on every robot:** sharing a device pays, but here one process per robot serves as many or more (96 against 32): on this device batching does not beat co-location. Neura, deadline + fairness serves 32 robots per device (0.031 device-hours per robot-hour, against 1 with a device per robot); at 48 robots it misses the SLO, so the crossover where one device stops paying is between 32 and 48.
- **Per-robot LoRA adapters of one base model:** sharing a device pays, but here one process per robot serves as many or more (96 against 24): on this device batching does not beat co-location. Neura, multi-adapter batching serves 24 robots per device (0.042 device-hours per robot-hour, against 1 with a device per robot); at 32 robots it misses the SLO, so the crossover where one device stops paying is between 24 and 32.
- **Different full fine-tunes of one architecture:** sharing a device pays, but here one process per robot serves as many or more (96 against 96): on this device batching does not beat co-location. Neura, co-located groups serves 96 robots per device (0.010 device-hours per robot-hour, against 1 with a device per robot); at 128 robots it misses the SLO, so the crossover where one device stops paying is between 96 and 128.
- **Different architectures (ACT and Diffusion Policy):** sharing does not pay on this device: Neura, co-located groups serves 1 robot(s) at the SLO.
- **Same weights on every robot: SmolVLA:** sharing does not pay on this device: Neura, deadline + fairness serves 0 robot(s) at the SLO.
- **Same weights on every robot: pi0:** sharing does not pay on this device: Neura, deadline + fairness serves 0 robot(s) at the SLO.

## Measurements on the device

Forward-pass latency through a worker process, ACT:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 36.20 | 39.84 | 198 | 276 |
| 2 | 45.55 | 53.71 | 228 | 216 |
| 4 | 65.78 | 76.75 | 338 | 151 |
| 8 | 126.29 | 141.05 | 553 | 79 |
| 16 | 322.50 | 360.83 | 986 | 31 |

Forward-pass latency through a worker process, Diffusion Policy:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 125.78 | 136.65 | 1051 | 80 |
| 2 | 137.04 | 153.02 | 1051 | 73 |
| 4 | 154.80 | 171.76 | 1051 | 65 |
| 8 | 195.99 | 207.98 | 1051 | 51 |
| 16 | 338.02 | 392.69 | 1118 | 30 |

Forward-pass latency through a worker process, SmolVLA:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 374.25 | 401.88 | 1165 | 27 |
| 2 | 397.03 | 415.22 | 1165 | 26 |
| 4 | 430.25 | 454.40 | 1165 | 24 |
| 8 | 530.49 | 580.94 | 1165 | 20 |
| 16 | 817.74 | 855.82 | 1381 | 20 |

Forward-pass latency through a worker process, pi0:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 380.31 | 406.56 | 8482 | 27 |
| 2 | 445.58 | 462.78 | 8482 | 23 |
| 4 | 597.04 | 626.21 | 8482 | 20 |
| 8 | 916.72 | 940.57 | 8482 | 20 |
| 16 | 1753.39 | 1788.65 | 9084 | 20 |

Co-location: k workers on the device, each running batches of one back to back. Alone: p50 35.819 ms. A background job (matrix multiplications) slows a lone worker by 1.118x.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 35.82 | 40.32 |
| 2 | 1.01 | 36.03 | 41.09 |
| 3 | 1.07 | 38.47 | 51.84 |
| 4 | 1.10 | 39.45 | 47.75 |

Simulated link, calibrated from one robot alone in real time: observation age at dispatch p50 7.547 ms, p99 11.614 ms (robot encoding, gRPC and the gateway's checks), simulated as 3.397 ms plus up to 8.3 ms.

Multi-adapter batching: 16 rank-16 LoRA adapters on 13 linear layers of the ACT base model; every request in a batch uses a different adapter. Parity with each adapter merged and run alone: max relative error 6.69e-05 (tolerance 1e-04), passed.

| Batch | Base p50 (ms) | Mixed adapters p50 (ms) | Overhead |
| --- | --- | --- | --- |
| 1 | 20.75 | 24.00 | 1.16 |
| 2 | 26.10 | 30.51 | 1.17 |
| 4 | 45.47 | 52.60 | 1.16 |
| 8 | 94.37 | 106.08 | 1.12 |
| 16 | 207.17 | 228.13 | 1.10 |

Admission control's estimate for ACT on this device: 4 robots (limited by acceptance_freshness), against 24 measured with synchronized arrivals. Admission assumes the worst case (every robot asks at once, behind a batch already running), so it must not exceed the synchronized capacity: it does not (4 <= 24).

For SmolVLA: 0 robots (limited by acceptance_freshness), against 0 measured with synchronized arrivals: it does not (0 <= 0).

For pi0: 0 robots (limited by acceptance_freshness), against 0 measured with synchronized arrivals: it does not (0 <= 0).

## Results

Per 1,000 requests: deadline misses (served after the predicted starvation time or rejected by the robot as too old, too old to dispatch, or failed). Per robot-hour: starvation events and faults. Age: from the robot's capture to the start of the forward pass.

### Same weights on every robot: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.6 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.6 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 8.0 / 11.6 / 11.6 | 1.00 | 7% | 0.500 | 396 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 14% | 0.250 | 792 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 8.0 / 11.6 / 11.7 | 1.00 | 29% | 0.125 | 1584 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 11.6 / 11.7 | 1.00 | 44% | 0.083 | 2376 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 7.5 / 11.6 / 11.7 | 1.00 | 58% | 0.062 | 3168 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 87% | 0.042 | 4752 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 100% | 0.031 | 6336 |
| Dedicated: one process per robot, one device | 48 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.021 | 9504 |
| Dedicated: one process per robot, one device | 64 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.016 | 12672 |
| Dedicated: one process per robot, one device | 96 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.010 | 19008 |
| Dedicated: one process per robot, one device | 128 | 0.00 | 0.0 | 0.0 | 33.4 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.008 | 25344 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 10.2 / 13.6 / 13.7 | 1.00 | 7% | 0.500 | 198 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 9.7 / 13.6 / 13.7 | 1.00 | 14% | 0.250 | 198 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 9.8 / 13.6 / 13.7 | 1.00 | 29% | 0.125 | 198 |
| Neura, FIFO | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 10.0 / 13.6 / 13.7 | 1.00 | 43% | 0.083 | 198 |
| Neura, FIFO | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 9.7 / 13.6 / 13.7 | 1.00 | 58% | 0.062 | 198 |
| Neura, FIFO | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 9.9 / 13.6 / 13.7 | 1.00 | 87% | 0.042 | 198 |
| Neura, FIFO | 32 | 0.00 | 0.0 | 0.0 | 53.2 | 22.6 / 44.9 / 58.9 | 1.27 | 98% | 0.031 | 283 |
| Neura, FIFO | 48 | 667.68 | 481.2 | 70.0 | 67.9 | 74.0 / 95.8 / 99.1 | 1.93 | 100% | 0.021 | 445 |
| Neura, FIFO | 64 | 828.91 | 825.0 | 337.5 | 38.1 | 84.6 / 96.8 / 99.2 | 2.16 | 100% | 0.016 | 661 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 10.2 / 13.6 / 13.7 | 1.00 | 7% | 0.500 | 198 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 9.7 / 13.6 / 13.7 | 1.00 | 14% | 0.250 | 198 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 9.8 / 13.6 / 13.7 | 1.00 | 29% | 0.125 | 198 |
| Neura, round robin | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 10.0 / 13.6 / 13.7 | 1.00 | 43% | 0.083 | 198 |
| Neura, round robin | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 9.7 / 13.6 / 13.7 | 1.00 | 58% | 0.062 | 198 |
| Neura, round robin | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 9.9 / 13.6 / 13.7 | 1.00 | 87% | 0.042 | 198 |
| Neura, round robin | 32 | 0.00 | 0.0 | 0.0 | 53.2 | 22.6 / 44.9 / 58.9 | 1.27 | 98% | 0.031 | 283 |
| Neura, round robin | 48 | 180.05 | 8.1 | 0.6 | 82.4 | 35.7 / 92.2 / 98.6 | 2.38 | 99% | 0.021 | 391 |
| Neura, round robin | 64 | 630.33 | 153.3 | 21.1 | 71.6 | 38.3 / 93.6 / 99.8 | 3.04 | 100% | 0.016 | 445 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 10.2 / 13.6 / 13.7 | 1.00 | 7% | 0.500 | 198 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 9.7 / 13.6 / 13.7 | 1.00 | 14% | 0.250 | 198 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 9.8 / 13.6 / 13.7 | 1.00 | 29% | 0.125 | 198 |
| Neura, deadline + fairness | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 10.0 / 13.6 / 13.7 | 1.00 | 43% | 0.083 | 198 |
| Neura, deadline + fairness | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 9.7 / 13.6 / 13.7 | 1.00 | 58% | 0.062 | 198 |
| Neura, deadline + fairness | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 9.9 / 13.6 / 13.7 | 1.00 | 87% | 0.042 | 198 |
| Neura, deadline + fairness | 32 | 0.00 | 0.0 | 0.0 | 53.4 | 22.8 / 45.2 / 56.4 | 1.27 | 98% | 0.031 | 283 |
| Neura, deadline + fairness | 48 | 147.38 | 0.0 | 0.0 | 82.0 | 35.7 / 91.8 / 98.6 | 2.38 | 99% | 0.021 | 391 |
| Neura, deadline + fairness | 64 | 630.12 | 75.0 | 0.0 | 71.8 | 37.8 / 93.9 / 98.8 | 3.20 | 100% | 0.016 | 445 |

### Same weights on every robot: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.6 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.6 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 8.0 / 11.6 / 11.7 | 1.00 | 4% | 0.500 | 396 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 8.0 / 11.6 / 11.7 | 1.00 | 5% | 0.250 | 792 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.7 | 7.6 / 11.6 / 11.7 | 1.00 | 5% | 0.125 | 1584 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 36.1 | 7.6 / 11.6 / 11.7 | 1.00 | 8% | 0.083 | 2376 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 35.3 | 7.6 / 11.6 / 11.7 | 1.00 | 8% | 0.062 | 3168 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 64.5 | 7.9 / 11.6 / 11.7 | 1.00 | 7% | 0.042 | 4752 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 66.6 | 7.8 / 11.6 / 11.7 | 1.00 | 7% | 0.031 | 6336 |
| Dedicated: one process per robot, one device | 48 | 0.00 | 0.0 | 0.0 | 67.5 | 7.8 / 11.6 / 11.7 | 1.00 | 11% | 0.021 | 9504 |
| Dedicated: one process per robot, one device | 64 | 0.00 | 0.0 | 0.0 | 96.0 | 7.8 / 11.6 / 11.7 | 1.00 | 13% | 0.016 | 12672 |
| Dedicated: one process per robot, one device | 96 | 0.00 | 0.0 | 0.0 | 116.8 | 7.8 / 11.6 / 11.7 | 1.00 | 18% | 0.010 | 19008 |
| Dedicated: one process per robot, one device | 128 | 0.00 | 0.0 | 0.0 | 79.0 | 7.8 / 11.6 / 11.7 | 1.00 | 35% | 0.008 | 25344 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 11.5 / 17.5 / 18.5 | 1.00 | 7% | 0.500 | 198 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 47.1 | 12.0 / 48.0 / 49.9 | 1.37 | 12% | 0.250 | 228 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 65.3 | 17.8 / 68.3 / 72.8 | 1.92 | 19% | 0.125 | 338 |
| Neura, FIFO | 12 | 0.00 | 0.0 | 0.0 | 59.0 | 12.0 / 58.8 / 67.3 | 1.68 | 30% | 0.083 | 283 |
| Neura, FIFO | 16 | 0.52 | 0.0 | 0.0 | 64.9 | 19.9 / 65.3 / 74.1 | 1.78 | 39% | 0.062 | 391 |
| Neura, FIFO | 24 | 1.39 | 0.0 | 0.0 | 68.2 | 18.7 / 71.0 / 87.0 | 1.95 | 56% | 0.042 | 715 |
| Neura, FIFO | 32 | 0.00 | 0.0 | 0.0 | 66.0 | 24.9 / 65.8 / 84.3 | 1.77 | 79% | 0.031 | 986 |
| Neura, FIFO | 48 | 645.56 | 506.2 | 91.2 | 64.9 | 71.4 / 94.0 / 100.3 | 2.01 | 100% | 0.021 | 986 |
| Neura, FIFO | 64 | 806.67 | 796.9 | 336.6 | 37.6 | 79.0 / 95.3 / 100.5 | 2.21 | 100% | 0.016 | 931 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 11.5 / 17.5 / 18.5 | 1.00 | 7% | 0.500 | 198 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 47.1 | 12.0 / 48.0 / 49.9 | 1.37 | 12% | 0.250 | 228 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 65.3 | 17.8 / 68.3 / 72.8 | 1.92 | 19% | 0.125 | 338 |
| Neura, round robin | 12 | 0.00 | 0.0 | 0.0 | 60.1 | 12.0 / 58.2 / 64.8 | 1.68 | 31% | 0.083 | 283 |
| Neura, round robin | 16 | 0.52 | 0.0 | 0.0 | 64.8 | 19.7 / 65.6 / 74.1 | 1.77 | 39% | 0.062 | 391 |
| Neura, round robin | 24 | 0.69 | 0.0 | 0.0 | 68.5 | 18.7 / 68.7 / 81.1 | 1.97 | 56% | 0.042 | 715 |
| Neura, round robin | 32 | 1.56 | 0.0 | 0.0 | 64.7 | 20.4 / 65.3 / 86.8 | 1.80 | 79% | 0.031 | 986 |
| Neura, round robin | 48 | 122.83 | 3.8 | 0.0 | 82.7 | 32.4 / 87.4 / 99.9 | 2.45 | 98% | 0.021 | 986 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 11.5 / 17.5 / 18.5 | 1.00 | 7% | 0.500 | 198 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 47.1 | 12.0 / 48.0 / 49.9 | 1.37 | 12% | 0.250 | 228 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 65.3 | 17.8 / 68.3 / 72.8 | 1.92 | 19% | 0.125 | 338 |
| Neura, deadline + fairness | 12 | 0.00 | 0.0 | 0.0 | 59.0 | 12.0 / 58.8 / 67.3 | 1.68 | 30% | 0.083 | 283 |
| Neura, deadline + fairness | 16 | 0.52 | 0.0 | 0.0 | 64.7 | 18.6 / 66.1 / 76.2 | 1.78 | 39% | 0.062 | 391 |
| Neura, deadline + fairness | 24 | 0.69 | 0.0 | 0.0 | 67.9 | 20.0 / 67.8 / 78.2 | 2.09 | 54% | 0.042 | 715 |
| Neura, deadline + fairness | 32 | 1.04 | 0.0 | 0.0 | 68.7 | 23.7 / 68.8 / 87.4 | 2.01 | 73% | 0.031 | 986 |
| Neura, deadline + fairness | 48 | 112.75 | 0.0 | 0.0 | 83.4 | 33.8 / 88.3 / 97.6 | 2.45 | 99% | 0.021 | 986 |

### Same weights on every robot: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.7 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.7 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 8.2 / 11.6 / 11.7 | 1.00 | 8% | 0.500 | 396 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 16% | 0.250 | 792 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 32% | 0.125 | 1584 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 49% | 0.083 | 2376 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.4 | 8.0 / 11.6 / 11.7 | 1.00 | 65% | 0.062 | 3168 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 95% | 0.042 | 4752 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.031 | 6336 |
| Dedicated: one process per robot, one device | 48 | 0.00 | 0.0 | 0.0 | 33.4 | 7.9 / 11.6 / 11.7 | 1.00 | 100% | 0.021 | 9504 |
| Dedicated: one process per robot, one device | 64 | 0.00 | 0.0 | 0.0 | 33.7 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.016 | 12672 |
| Dedicated: one process per robot, one device | 96 | 0.00 | 0.0 | 0.0 | 35.0 | 7.7 / 11.6 / 11.7 | 1.00 | 100% | 0.010 | 19008 |
| Dedicated: one process per robot, one device | 128 | 0.00 | 0.0 | 0.0 | 38.0 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.008 | 25344 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 33.5 | 10.2 / 13.6 / 13.7 | 1.00 | 8% | 0.500 | 198 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 9.8 / 13.6 / 13.7 | 1.00 | 16% | 0.250 | 198 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 33.5 | 9.7 / 13.6 / 13.7 | 1.00 | 32% | 0.125 | 198 |
| Neura, FIFO | 12 | 0.00 | 0.0 | 0.0 | 33.6 | 9.6 / 13.6 / 13.7 | 1.00 | 49% | 0.083 | 198 |
| Neura, FIFO | 16 | 0.00 | 0.0 | 0.0 | 33.6 | 9.8 / 13.6 / 13.7 | 1.00 | 65% | 0.062 | 198 |
| Neura, FIFO | 24 | 0.00 | 0.0 | 0.0 | 34.1 | 11.7 / 16.8 / 19.8 | 1.00 | 97% | 0.042 | 198 |
| Neura, FIFO | 32 | 0.00 | 0.0 | 0.0 | 65.8 | 26.0 / 54.2 / 78.8 | 1.48 | 98% | 0.031 | 283 |
| Neura, FIFO | 48 | 809.80 | 809.4 | 332.5 | 37.8 | 82.4 / 96.1 / 99.8 | 2.70 | 100% | 0.021 | 715 |
| Neura, FIFO | 64 | 868.52 | 613.1 | 410.6 | 19.2 | 85.5 / 96.6 / 100.0 | 3.66 | 100% | 0.016 | 931 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 33.5 | 10.2 / 13.6 / 13.7 | 1.00 | 8% | 0.500 | 198 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 9.8 / 13.6 / 13.7 | 1.00 | 16% | 0.250 | 198 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 33.5 | 9.7 / 13.6 / 13.7 | 1.00 | 32% | 0.125 | 198 |
| Neura, round robin | 12 | 0.00 | 0.0 | 0.0 | 33.6 | 9.6 / 13.6 / 13.7 | 1.00 | 49% | 0.083 | 198 |
| Neura, round robin | 16 | 0.00 | 0.0 | 0.0 | 33.6 | 9.8 / 13.6 / 13.7 | 1.00 | 65% | 0.062 | 198 |
| Neura, round robin | 24 | 0.00 | 0.0 | 0.0 | 34.1 | 11.7 / 16.8 / 19.8 | 1.00 | 97% | 0.042 | 198 |
| Neura, round robin | 32 | 0.00 | 0.0 | 0.0 | 65.9 | 26.3 / 54.2 / 78.4 | 1.49 | 98% | 0.031 | 283 |
| Neura, round robin | 48 | 501.17 | 213.8 | 25.0 | 76.3 | 39.0 / 93.5 / 99.3 | 2.65 | 100% | 0.021 | 445 |
| Neura, round robin | 64 | 761.89 | 343.6 | 193.1 | 58.0 | 40.0 / 95.0 / 100.3 | 2.85 | 100% | 0.016 | 499 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 33.5 | 10.2 / 13.6 / 13.7 | 1.00 | 8% | 0.500 | 198 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 9.8 / 13.6 / 13.7 | 1.00 | 16% | 0.250 | 198 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 33.5 | 9.7 / 13.6 / 13.7 | 1.00 | 32% | 0.125 | 198 |
| Neura, deadline + fairness | 12 | 0.00 | 0.0 | 0.0 | 33.6 | 9.6 / 13.6 / 13.7 | 1.00 | 49% | 0.083 | 198 |
| Neura, deadline + fairness | 16 | 0.00 | 0.0 | 0.0 | 33.6 | 9.8 / 13.6 / 13.7 | 1.00 | 65% | 0.062 | 198 |
| Neura, deadline + fairness | 24 | 0.00 | 0.0 | 0.0 | 34.1 | 11.7 / 16.8 / 19.8 | 1.00 | 97% | 0.042 | 198 |
| Neura, deadline + fairness | 32 | 0.00 | 0.0 | 0.0 | 66.0 | 26.0 / 54.7 / 76.7 | 1.49 | 98% | 0.031 | 283 |
| Neura, deadline + fairness | 48 | 474.00 | 1.2 | 0.0 | 81.4 | 36.9 / 93.7 / 98.8 | 2.76 | 100% | 0.021 | 445 |
| Neura, deadline + fairness | 64 | 833.02 | 1186.9 | 371.7 | 1.3 | 82.2 / 96.7 / 100.0 | 2.98 | 100% | 0.016 | 931 |

### Per-robot LoRA adapters of one base model: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.6 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.6 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 8.0 / 11.6 / 11.6 | 1.00 | 7% | 0.500 | 396 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 14% | 0.250 | 792 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 8.0 / 11.6 / 11.7 | 1.00 | 29% | 0.125 | 1584 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 11.6 / 11.7 | 1.00 | 44% | 0.083 | 2376 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 7.5 / 11.6 / 11.7 | 1.00 | 58% | 0.062 | 3168 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 87% | 0.042 | 4752 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 100% | 0.031 | 6336 |
| Dedicated: one process per robot, one device | 48 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.021 | 9504 |
| Dedicated: one process per robot, one device | 64 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.016 | 12672 |
| Dedicated: one process per robot, one device | 96 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.010 | 19008 |
| Dedicated: one process per robot, one device | 128 | 0.00 | 0.0 | 0.0 | 33.4 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.008 | 25344 |
| Neura, multi-adapter batching | 1 | 0.00 | 0.0 | 0.0 | 34.2 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, multi-adapter batching | 2 | 0.00 | 0.0 | 0.0 | 34.0 | 10.2 / 13.6 / 13.7 | 1.00 | 8% | 0.500 | 198 |
| Neura, multi-adapter batching | 4 | 0.00 | 0.0 | 0.0 | 34.0 | 9.6 / 13.6 / 13.7 | 1.00 | 17% | 0.250 | 198 |
| Neura, multi-adapter batching | 8 | 0.00 | 0.0 | 0.0 | 34.2 | 9.7 / 13.6 / 13.7 | 1.00 | 34% | 0.125 | 198 |
| Neura, multi-adapter batching | 12 | 0.00 | 0.0 | 0.0 | 34.2 | 9.6 / 13.6 / 13.7 | 1.00 | 50% | 0.083 | 198 |
| Neura, multi-adapter batching | 16 | 0.00 | 0.0 | 0.0 | 33.9 | 9.8 / 13.6 / 13.7 | 1.00 | 67% | 0.062 | 198 |
| Neura, multi-adapter batching | 24 | 0.00 | 0.0 | 0.0 | 53.7 | 17.5 / 50.4 / 54.1 | 1.05 | 97% | 0.042 | 228 |
| Neura, multi-adapter batching | 32 | 16.68 | 0.0 | 0.0 | 74.0 | 27.9 / 80.1 / 96.1 | 1.64 | 96% | 0.031 | 338 |
| Neura, multi-adapter batching | 48 | 609.44 | 121.2 | 0.0 | 72.6 | 38.0 / 94.2 / 99.1 | 2.71 | 100% | 0.021 | 499 |

### Different full fine-tunes of one architecture: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 4%, 4%, 4%, 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.6 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 8.0 / 11.6 / 11.6 | 1.00 | 7% | 0.500 | 396 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 14% | 0.250 | 792 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 8.0 / 11.6 / 11.7 | 1.00 | 29% | 0.125 | 1584 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 7.9 / 11.6 / 11.7 | 1.00 | 44% | 0.083 | 2376 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 7.5 / 11.6 / 11.7 | 1.00 | 58% | 0.062 | 3168 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 87% | 0.042 | 4752 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 100% | 0.031 | 6336 |
| Dedicated: one process per robot, one device | 48 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.021 | 9504 |
| Dedicated: one process per robot, one device | 64 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.016 | 12672 |
| Dedicated: one process per robot, one device | 96 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.010 | 19008 |
| Dedicated: one process per robot, one device | 128 | 0.00 | 0.0 | 0.0 | 33.4 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.008 | 25344 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, co-located groups | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 10.2 / 13.6 / 13.7 | 1.00 | 7% | 0.500 | 396 |
| Neura, co-located groups | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 9.7 / 13.6 / 13.7 | 1.00 | 14% | 0.250 | 792 |
| Neura, co-located groups | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 9.8 / 13.6 / 13.7 | 1.00 | 29% | 0.125 | 792 |
| Neura, co-located groups | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 10.0 / 13.6 / 13.7 | 1.00 | 43% | 0.083 | 792 |
| Neura, co-located groups | 16 | 0.00 | 0.0 | 0.0 | 33.3 | 9.7 / 13.6 / 13.7 | 1.00 | 58% | 0.062 | 792 |
| Neura, co-located groups | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 9.8 / 13.6 / 13.7 | 1.00 | 87% | 0.042 | 792 |
| Neura, co-located groups | 32 | 0.00 | 0.0 | 0.0 | 45.1 | 17.4 / 44.3 / 54.5 | 1.10 | 93% | 0.031 | 882 |
| Neura, co-located groups | 48 | 0.00 | 0.0 | 0.0 | 61.2 | 25.4 / 56.2 / 67.5 | 1.53 | 100% | 0.021 | 1132 |
| Neura, co-located groups | 64 | 0.13 | 0.0 | 0.0 | 63.8 | 26.5 / 58.8 / 90.6 | 1.61 | 100% | 0.016 | 1242 |
| Neura, co-located groups | 96 | 0.09 | 0.0 | 0.0 | 64.5 | 26.4 / 59.2 / 89.1 | 1.63 | 100% | 0.010 | 1405 |
| Neura, co-located groups | 128 | 1.04 | 0.0 | 0.0 | 69.4 | 27.8 / 65.1 / 97.3 | 1.74 | 100% | 0.008 | 1458 |

### Different full fine-tunes of one architecture: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 4%, 4%, 4%, 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.7 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 33.3 | 8.2 / 11.6 / 11.7 | 1.00 | 8% | 0.500 | 396 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 7.7 / 11.6 / 11.7 | 1.00 | 16% | 0.250 | 792 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 32% | 0.125 | 1584 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 49% | 0.083 | 2376 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 33.4 | 8.0 / 11.6 / 11.7 | 1.00 | 65% | 0.062 | 3168 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 95% | 0.042 | 4752 |
| Dedicated: one process per robot, one device | 32 | 0.00 | 0.0 | 0.0 | 33.3 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.031 | 6336 |
| Dedicated: one process per robot, one device | 48 | 0.00 | 0.0 | 0.0 | 33.4 | 7.9 / 11.6 / 11.7 | 1.00 | 100% | 0.021 | 9504 |
| Dedicated: one process per robot, one device | 64 | 0.00 | 0.0 | 0.0 | 33.7 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.016 | 12672 |
| Dedicated: one process per robot, one device | 96 | 0.00 | 0.0 | 0.0 | 35.0 | 7.7 / 11.6 / 11.7 | 1.00 | 100% | 0.010 | 19008 |
| Dedicated: one process per robot, one device | 128 | 0.00 | 0.0 | 0.0 | 38.0 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.008 | 25344 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, co-located groups | 2 | 0.00 | 0.0 | 0.0 | 33.5 | 10.2 / 13.6 / 13.7 | 1.00 | 8% | 0.500 | 396 |
| Neura, co-located groups | 4 | 0.00 | 0.0 | 0.0 | 33.3 | 9.8 / 13.6 / 13.7 | 1.00 | 16% | 0.250 | 792 |
| Neura, co-located groups | 8 | 0.00 | 0.0 | 0.0 | 33.5 | 9.7 / 13.6 / 13.7 | 1.00 | 32% | 0.125 | 792 |
| Neura, co-located groups | 12 | 0.00 | 0.0 | 0.0 | 33.6 | 9.6 / 13.6 / 13.7 | 1.00 | 49% | 0.083 | 792 |
| Neura, co-located groups | 16 | 0.00 | 0.0 | 0.0 | 33.6 | 9.8 / 13.6 / 13.7 | 1.00 | 65% | 0.062 | 792 |
| Neura, co-located groups | 24 | 0.00 | 0.0 | 0.0 | 33.8 | 11.2 / 15.3 / 17.7 | 1.00 | 97% | 0.042 | 792 |
| Neura, co-located groups | 32 | 0.00 | 0.0 | 0.0 | 53.2 | 19.1 / 49.9 / 57.0 | 1.14 | 95% | 0.031 | 912 |
| Neura, co-located groups | 48 | 0.52 | 0.0 | 0.0 | 63.4 | 26.4 / 61.6 / 91.7 | 1.41 | 100% | 0.021 | 1187 |
| Neura, co-located groups | 64 | 0.78 | 0.0 | 0.0 | 64.8 | 25.8 / 60.0 / 84.8 | 1.42 | 100% | 0.016 | 1187 |
| Neura, co-located groups | 96 | 1.82 | 0.0 | 0.0 | 70.5 | 28.2 / 66.5 / 95.0 | 1.42 | 100% | 0.010 | 1352 |
| Neura, co-located groups | 128 | 109.82 | 0.0 | 0.0 | 82.8 | 34.3 / 90.8 / 98.7 | 1.78 | 100% | 0.008 | 1618 |

### Different architectures (ACT and Diffusion Policy): phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 556.86 | 3030.0 | 0.0 | 161.5 | 8.4 / 11.7 / 14.6 | 1.00 | 4%, 68% | 1.000 | 1051 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 8.1 / 11.6 / 11.7 | 1.00 | 4% | 1.000 | 198 |
| Dedicated: one process per robot, one device | 2 | 552.80 | 2970.0 | 0.0 | 163.0 | 8.4 / 11.6 / 14.6 | 1.00 | 69% | 0.500 | 1249 |
| Dedicated: one process per robot, one device | 4 | 546.23 | 2985.0 | 0.0 | 158.3 | 8.2 / 11.7 / 17.2 | 1.00 | 89% | 0.250 | 2498 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 33.3 | 10.1 / 13.7 / 13.7 | 1.00 | 4% | 1.000 | 198 |
| Neura, co-located groups | 2 | 535.43 | 2985.0 | 0.0 | 155.8 | 10.4 / 13.7 / 16.8 | 1.00 | 68% | 0.500 | 1249 |
| Neura, co-located groups | 4 | 482.57 | 2632.5 | 37.5 | 66.8 | 10.4 / 48.0 / 96.2 | 1.12 | 86% | 0.250 | 1249 |

### Same weights on every robot: SmolVLA: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 1000.00 | 0.0 | 0.0 | 0.0 | 7.6 / 11.6 / 11.6 | 1.00 | 92% | 1.000 | 1165 |
| Dedicated: one process per robot, one device | 1 | 1000.00 | 0.0 | 0.0 | 0.0 | 7.6 / 11.6 / 11.6 | 1.00 | 92% | 1.000 | 1165 |
| Dedicated: one process per robot, one device | 2 | 999.15 | 0.0 | 0.0 | 0.0 | 8.1 / 11.6 / 11.7 | 1.00 | 99% | 0.500 | 2330 |
| Neura, deadline + fairness | 1 | 1000.00 | 0.0 | 0.0 | 0.0 | 9.8 / 13.6 / 13.7 | 1.00 | 91% | 1.000 | 1165 |
| Neura, deadline + fairness | 2 | 752.51 | 0.0 | 0.0 | 0.0 | 10.1 / 13.5 / 13.7 | 1.00 | 94% | 0.500 | 1165 |

### Same weights on every robot: SmolVLA: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 1000.00 | 0.0 | 0.0 | 0.0 | 7.6 / 11.6 / 11.6 | 1.00 | 92% | 1.000 | 1165 |
| Dedicated: one process per robot, one device | 1 | 1000.00 | 0.0 | 0.0 | 0.0 | 7.6 / 11.6 / 11.6 | 1.00 | 92% | 1.000 | 1165 |
| Dedicated: one process per robot, one device | 2 | 999.15 | 0.0 | 0.0 | 0.0 | 7.8 / 11.6 / 11.7 | 1.00 | 99% | 0.500 | 2330 |
| Neura, deadline + fairness | 1 | 1000.00 | 0.0 | 0.0 | 0.0 | 9.8 / 13.6 / 13.7 | 1.00 | 91% | 1.000 | 1165 |
| Neura, deadline + fairness | 2 | 833.91 | 0.0 | 0.0 | 0.0 | 8.7 / 13.2 / 13.5 | 1.35 | 92% | 0.500 | 1165 |

### Same weights on every robot: pi0: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 1001.72 | 0.0 | 0.0 | 0.0 | 7.7 / 11.6 / 11.7 | 1.00 | 92% | 1.000 | 8482 |
| Dedicated: one process per robot, one device | 1 | 1001.72 | 0.0 | 0.0 | 0.0 | 7.7 / 11.6 / 11.7 | 1.00 | 92% | 1.000 | 8482 |
| Dedicated: one process per robot, one device | 2 | 998.26 | 0.0 | 0.0 | 0.0 | 7.9 / 11.6 / 11.7 | 1.00 | 98% | 0.500 | 16964 |
| Neura, deadline + fairness | 1 | 1001.73 | 0.0 | 0.0 | 0.0 | 10.2 / 13.6 / 13.7 | 1.00 | 92% | 1.000 | 8482 |
| Neura, deadline + fairness | 2 | 748.73 | 0.0 | 0.0 | 0.0 | 9.5 / 13.6 / 13.7 | 1.00 | 93% | 0.500 | 8482 |

### Same weights on every robot: pi0: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 1001.72 | 0.0 | 0.0 | 0.0 | 7.7 / 11.6 / 11.7 | 1.00 | 92% | 1.000 | 8482 |
| Dedicated: one process per robot, one device | 1 | 1001.72 | 0.0 | 0.0 | 0.0 | 7.7 / 11.6 / 11.7 | 1.00 | 92% | 1.000 | 8482 |
| Dedicated: one process per robot, one device | 2 | 1001.73 | 0.0 | 0.0 | 0.0 | 7.8 / 11.6 / 11.7 | 1.00 | 100% | 0.500 | 16964 |
| Neura, deadline + fairness | 1 | 1001.73 | 0.0 | 0.0 | 0.0 | 10.2 / 13.6 / 13.7 | 1.00 | 92% | 1.000 | 8482 |
| Neura, deadline + fairness | 2 | 852.62 | 0.0 | 0.0 | 0.0 | 8.5 / 13.3 / 13.6 | 1.41 | 94% | 0.500 | 8482 |

## Real-time validation

The production gateway with real worker processes on the device and simulated robots over gRPC, next to the simulation of the same configuration (same robot count, warmup and window). Each cell is real / simulated. With a few hundred requests a p99 is close to the maximum: one collision moves it, so read it with the median and the miss rate. Late ticks: robot control ticks the harness itself ran late (high values mean the harness, not Neura, was overloaded).

| Case | Configuration | Robots | Requests | Misses / 1k | Starvation / robot-h | Age p50 ms | Age p99 ms | Mean batch | Device busy | Late ticks | Worker restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Same weights on every robot: phased arrivals | Dedicated: one device per robot | 1 | 60 / 60 | 0.00 / 0.00 | 0.0 / 0.0 | 6.7 / 7.9 | 8.2 / 11.3 | 1.00 / 1.00 | 4% / 4% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 1 | 60 / 60 | 0.00 / 0.00 | 0.0 / 0.0 | 9.1 / 9.3 | 11.5 / 13.3 | 1.00 / 1.00 | 4% / 4% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 4 | 237 / 240 | 4.22 / 0.00 | 0.0 / 0.0 | 9.6 / 10.0 | 11.8 / 13.6 | 1.00 / 1.00 | 14% / 14% | 0.1% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 8 | 480 / 480 | 0.00 / 0.00 | 0.0 / 0.0 | 8.9 / 10.0 | 11.9 / 13.6 | 1.00 / 1.00 | 29% / 29% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 9.5 / 9.6 | 29.2 / 13.6 | 1.00 / 1.00 | 59% / 58% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, FIFO | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 9.9 / 9.6 | 28.8 / 13.6 | 1.00 / 1.00 | 59% / 58% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Dedicated: one process per robot, one device | 4 | 240 / 240 | 0.00 / 0.00 | 0.0 / 0.0 | 7.5 / 7.8 | 9.7 / 11.6 | 1.00 / 1.00 | 15% / 14% | 0.0% | 0 |
| Same weights on every robot: synchronized (bursty) arrivals | Neura, deadline + fairness | 16 | 960 / 961 | 0.00 / 1.04 | 0.0 / 0.0 | 19.0 / 20.3 | 81.6 / 66.0 | 1.80 / 1.78 | 43% / 39% | 0.0% | 0 |
| Same weights on every robot: phased, with a background GPU job | Neura, deadline + fairness | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 9.8 / 9.8 | 25.4 / 13.6 | 1.00 / 1.00 | 66% / 65% | 0.0% | 0 |
| Different full fine-tunes of one architecture: phased arrivals | Neura, co-located groups | 16 | 960 / 960 | 0.00 / 0.00 | 0.0 / 0.0 | 15.2 / 9.6 | 34.8 / 13.6 | 1.00 / 1.00 | 60% / 58% | 0.0% | 0 |
| Different architectures (ACT and Diffusion Policy): phased arrivals | Neura, co-located groups | 16 | 2884 / 2858 | 824.90 / 748.42 | 15.0 / 566.2 | 36.1 / 11.3 | 119.0 / 94.9 | 2.41 / 1.16 | 93% / 96% | 2.0% | 0 |
| Same weights on every robot: SmolVLA: phased arrivals | Neura, deadline + fairness | 4 | 1198 / 1212 | 623.54 / 628.71 | 0.0 / 0.0 | 10.6 / 9.5 | 21.8 / 13.5 | 1.01 / 1.01 | 96% / 95% | 0.0% | 0 |
| Same weights on every robot: pi0: phased arrivals | Neura, deadline + fairness | 4 | 1214 / 1192 | 624.38 / 626.68 | 0.0 / 0.0 | 9.2 / 9.5 | 17.6 / 13.6 | 1.00 / 1.01 | 96% / 95% | 0.0% | 0 |

## Method and limits

- Configurations follow design §10: the dedicated baselines run batch size 1 with the same model, precision, observation encoding and harness; Neura runs one worker per batch group with the scheduler policy named.
- Latency is measured through `neura-worker` processes over their Unix sockets, so serialization and IPC are included. Simulation draws forward-pass times from these measurements (lognormal fitted to p50 and p99) and shares the device between running passes by the measured co-location slowdown; beyond 4 concurrent workers the slowdown is extrapolated linearly.
- Multi-adapter batching is measured at the forward pass and simulated at the serving level; the data plane does not group robots by base model yet (design §9.5: built only if this benchmark shows it pays). Measured overheads below 1 are timing noise and are not credited.
- Simulated robots tick with no timing noise, and the simulated gateway decodes an observation in no time. Real robots drift by milliseconds, so near-simultaneous requests sometimes miss the batching window: the simulation is somewhat optimistic for synchronized arrivals near a freshness limit (compare the validation table).
- On a CPU host the background job and the worker share the CPU with the gateway and the simulated robots, so real ages there include the harness slowing down; the simulation models the device only. On a GPU the background job competes for the GPU alone.
- Admission control is set aside in the benchmark so it can measure what N robots get; production refuses sessions beyond the admission estimate.
- Robots that latch a fault are reset by a simulated operator after one second and counted.

## Reproduce

```
neura-bench run --preset gpu --device cuda:0 --size base --out /tmp/neura-bench-k8fi7vrn/report
```
