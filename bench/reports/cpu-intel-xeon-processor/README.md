# Neura GPU-sharing benchmark: CPU: Intel(R) Xeon(R) Processor @ 2.80GHz

Generated 2026-10-15T05:07:49+00:00 from commit `184b221` with `neura-bench run --preset cpu` (torch 2.11.0, LeRobot 0.6.1). Regenerate the Markdown from `results.json` with `neura-bench report`.

> **This run is on a CPU development host, not a GPU.** It shows the harness working end to end and how sharing behaves on this host. Its numbers say nothing about GPU capacity: run `make bench` on the GPU runner for that.

Models: synthetic checkpoints with the real LeRobot architectures and random weights (ACT, Diffusion Policy; size `small`, cameras 128 x 96, fp32). Latency, memory and batching depend on architecture and shapes, not on what weights learned; closed-loop task success needs trained weights and is not measured here.

## Robots per device at the SLO

SLO: at most 1.0 deadline misses per 1,000 requests, at most 0.0 starvation events per robot-hour, no robot faults. A configuration's capacity is the largest robot count at which it and every smaller count measured met the SLO (simulation with measured latency and contention, below).

| Case | Workload | One device per robot | One process per robot | Best Neura |
| --- | --- | --- | --- | --- |
| Same weights on every robot | phased arrivals | 1 | 8, fails at 12 | 8, fails at 12 (Neura, deadline + fairness) |
| Same weights on every robot | synchronized (bursty) arrivals | 1 | 1, fails at 2 | 2, fails at 4 (Neura, deadline + fairness) |
| Same weights on every robot | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, deadline + fairness) |
| Per-robot LoRA adapters of one base model | phased arrivals | 1 | 8, fails at 12 | 8, fails at 12 (Neura, multi-adapter batching) |
| 2 different full fine-tunes of one architecture | phased arrivals | 1 | 8, fails at 12 | 8, fails at 12 (Neura, co-located groups) |
| 2 different full fine-tunes of one architecture | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, co-located groups) |
| Different architectures (ACT and Diffusion Policy) | phased arrivals | 0 (fails at 1) | 1, fails at 2 | 1, fails at 2 (Neura, co-located groups) |

- **Same weights on every robot:** sharing a device pays, but here one process per robot serves as many or more (8 against 8): on this device batching does not beat co-location. Neura, deadline + fairness serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Per-robot LoRA adapters of one base model:** sharing a device pays, but here one process per robot serves as many or more (8 against 8): on this device batching does not beat co-location. Neura, multi-adapter batching serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **2 different full fine-tunes of one architecture:** sharing a device pays, but here one process per robot serves as many or more (8 against 8): on this device batching does not beat co-location. Neura, co-located groups serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Different architectures (ACT and Diffusion Policy):** sharing does not pay on this device: Neura, co-located groups serves 1 robot(s) at the SLO.

## Measurements on the device

Forward-pass latency through a worker process, ACT:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 78.79 | 105.94 | 816 | 101 |
| 2 | 103.07 | 127.16 | 835 | 77 |
| 4 | 144.10 | 167.22 | 846 | 56 |
| 8 | 192.24 | 227.51 | 856 | 42 |

Forward-pass latency through a worker process, Diffusion Policy:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 244.30 | 271.73 | 901 | 33 |
| 2 | 367.24 | 400.61 | 901 | 22 |
| 4 | 415.62 | 476.07 | 901 | 20 |
| 8 | 498.87 | 558.22 | 901 | 20 |

Co-location: k workers on the device, each running batches of one back to back. Alone: p50 74.932 ms. A background job (matrix multiplications) slows a lone worker by 4.167x.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 74.93 | 98.45 |
| 2 | 1.43 | 106.86 | 134.08 |
| 3 | 1.62 | 121.74 | 151.63 |

Simulated link, calibrated from one robot alone in real time: observation age at dispatch p50 2.095 ms, p99 4.647 ms (robot encoding, gRPC and the gateway's checks), simulated as 0.419 ms plus up to 3.352 ms.

Multi-adapter batching: 8 rank-16 LoRA adapters on 9 linear layers of the ACT base model; every request in a batch uses a different adapter. Parity with each adapter merged and run alone: max relative error 3.35e-07 (tolerance 1e-04), passed.

| Batch | Base p50 (ms) | Mixed adapters p50 (ms) | Overhead |
| --- | --- | --- | --- |
| 1 | 70.09 | 78.07 | 1.11 |
| 2 | 97.67 | 104.69 | 1.07 |
| 4 | 125.47 | 133.79 | 1.07 |
| 8 | 176.91 | 185.75 | 1.05 |

Admission control's estimate for ACT on this device: 0 robots (limited by dispatch_freshness), against 2 measured with synchronized arrivals. Admission assumes the worst case (every robot asks at once, behind a batch already running), so it must not exceed the synchronized capacity: it does not (0 <= 2).

## Results

Per 1,000 requests: deadline misses (served after the predicted starvation time or rejected by the robot as too old, too old to dispatch, or failed). Per robot-hour: starvation events and faults. Age: from the robot's capture to the start of the forward pass.

### Same weights on every robot: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.3 / 3.8 / 3.8 | 1.00 | 8% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.3 / 3.8 / 3.8 | 1.00 | 8% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 66.9 | 2.0 / 3.7 / 3.8 | 1.00 | 16% | 0.500 | 1632 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 67.6 | 2.0 / 3.7 / 3.8 | 1.00 | 32% | 0.250 | 3264 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 67.3 | 2.2 / 3.7 / 3.8 | 1.00 | 63% | 0.125 | 6528 |
| Dedicated: one process per robot, one device | 12 | 7.59 | 0.0 | 0.0 | 76.0 | 2.2 / 3.7 / 3.8 | 1.00 | 87% | 0.083 | 9792 |
| Dedicated: one process per robot, one device | 16 | 344.18 | 213.8 | 185.6 | 85.0 | 2.5 / 88.2 / 97.9 | 1.00 | 95% | 0.062 | 13056 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 69.4 | 4.3 / 5.8 / 5.8 | 1.00 | 8% | 1.000 | 816 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 68.5 | 4.0 / 5.7 / 5.8 | 1.00 | 16% | 0.500 | 816 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 68.3 | 4.1 / 5.7 / 5.8 | 1.00 | 32% | 0.250 | 816 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 68.7 | 4.1 / 5.7 / 5.8 | 1.00 | 64% | 0.125 | 816 |
| Neura, FIFO | 12 | 752.28 | 685.0 | 320.0 | 41.5 | 62.6 / 97.8 / 99.4 | 2.31 | 97% | 0.083 | 856 |
| Neura, FIFO | 16 | 863.08 | 600.0 | 435.0 | 8.0 | 69.6 / 97.9 / 99.9 | 3.99 | 99% | 0.062 | 856 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 69.4 | 4.3 / 5.8 / 5.8 | 1.00 | 8% | 1.000 | 816 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 68.5 | 4.0 / 5.7 / 5.8 | 1.00 | 16% | 0.500 | 816 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 68.3 | 4.1 / 5.7 / 5.8 | 1.00 | 32% | 0.250 | 816 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 68.7 | 4.1 / 5.7 / 5.8 | 1.00 | 64% | 0.125 | 816 |
| Neura, round robin | 12 | 723.32 | 602.5 | 342.5 | 50.0 | 60.4 / 97.6 / 100.7 | 2.09 | 96% | 0.083 | 856 |
| Neura, round robin | 16 | 860.23 | 594.4 | 436.9 | 21.4 | 66.6 / 97.9 / 100.0 | 3.43 | 100% | 0.062 | 856 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 69.4 | 4.3 / 5.8 / 5.8 | 1.00 | 8% | 1.000 | 816 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 68.5 | 4.0 / 5.7 / 5.8 | 1.00 | 16% | 0.500 | 816 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 68.3 | 4.1 / 5.7 / 5.8 | 1.00 | 32% | 0.250 | 816 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 68.7 | 4.1 / 5.7 / 5.8 | 1.00 | 64% | 0.125 | 816 |
| Neura, deadline + fairness | 12 | 742.64 | 770.0 | 342.5 | 37.0 | 62.3 / 97.6 / 100.0 | 2.33 | 97% | 0.083 | 856 |
| Neura, deadline + fairness | 16 | 868.05 | 633.8 | 446.2 | 6.7 | 68.5 / 97.6 / 100.2 | 4.08 | 100% | 0.062 | 856 |

### Same weights on every robot: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.3 / 3.8 / 3.8 | 1.00 | 8% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.3 / 3.8 / 3.8 | 1.00 | 8% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 2 | 4.15 | 0.0 | 0.0 | 91.0 | 2.3 / 3.7 / 3.8 | 1.00 | 13% | 0.500 | 1632 |
| Dedicated: one process per robot, one device | 4 | 2.08 | 0.0 | 0.0 | 87.4 | 2.2 / 3.7 / 3.8 | 1.00 | 26% | 0.250 | 3264 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 69.4 | 4.3 / 5.8 / 5.8 | 1.00 | 8% | 1.000 | 816 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 68.6 | 4.1 / 5.7 / 5.7 | 1.00 | 16% | 0.500 | 816 |
| Neura, FIFO | 4 | 4.15 | 0.0 | 0.0 | 70.4 | 4.1 / 54.6 / 83.4 | 1.03 | 31% | 0.250 | 840 |
| Neura, FIFO | 8 | 134.86 | 63.8 | 15.0 | 77.4 | 4.2 / 91.3 / 97.6 | 1.20 | 60% | 0.125 | 848 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 69.4 | 4.3 / 5.8 / 5.8 | 1.00 | 8% | 1.000 | 816 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 68.6 | 4.1 / 5.7 / 5.7 | 1.00 | 16% | 0.500 | 816 |
| Neura, round robin | 4 | 4.15 | 0.0 | 0.0 | 70.4 | 4.1 / 54.6 / 83.4 | 1.03 | 31% | 0.250 | 840 |
| Neura, round robin | 8 | 4.15 | 0.0 | 0.0 | 72.0 | 4.5 / 39.8 / 97.2 | 1.02 | 63% | 0.125 | 835 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 69.4 | 4.3 / 5.8 / 5.8 | 1.00 | 8% | 1.000 | 816 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 68.6 | 4.1 / 5.7 / 5.7 | 1.00 | 16% | 0.500 | 816 |
| Neura, deadline + fairness | 4 | 4.15 | 0.0 | 0.0 | 70.4 | 4.1 / 54.6 / 83.4 | 1.03 | 31% | 0.250 | 840 |
| Neura, deadline + fairness | 8 | 66.67 | 37.5 | 11.2 | 74.9 | 4.5 / 81.8 / 93.8 | 1.10 | 62% | 0.125 | 846 |

### Same weights on every robot: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 989.64 | 0.0 | 0.0 | 0.0 | 3.0 / 96.4 / 97.9 | 1.00 | 96% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 1 | 989.64 | 0.0 | 0.0 | 0.0 | 3.0 / 96.4 / 97.9 | 1.00 | 96% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 2 | 700.85 | 0.0 | 0.0 | 0.0 | 4.6 / 94.6 / 97.1 | 1.00 | 100% | 0.500 | 1632 |
| Neura, FIFO | 1 | 987.91 | 0.0 | 0.0 | 0.0 | 5.1 / 96.5 / 98.6 | 1.00 | 96% | 1.000 | 816 |
| Neura, FIFO | 2 | 792.21 | 0.0 | 0.0 | 0.0 | 4.6 / 94.0 / 95.5 | 1.81 | 97% | 0.500 | 835 |
| Neura, round robin | 1 | 987.91 | 0.0 | 0.0 | 0.0 | 5.1 / 96.5 / 98.6 | 1.00 | 96% | 1.000 | 816 |
| Neura, round robin | 2 | 783.33 | 0.0 | 0.0 | 0.0 | 4.6 / 94.1 / 95.6 | 1.81 | 97% | 0.500 | 835 |
| Neura, deadline + fairness | 1 | 987.91 | 0.0 | 0.0 | 0.0 | 5.1 / 96.5 / 98.6 | 1.00 | 96% | 1.000 | 816 |
| Neura, deadline + fairness | 2 | 817.50 | 0.0 | 0.0 | 0.0 | 4.5 / 94.6 / 96.3 | 1.81 | 97% | 0.500 | 835 |

### Per-robot LoRA adapters of one base model: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.3 / 3.8 / 3.8 | 1.00 | 8% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.3 / 3.8 / 3.8 | 1.00 | 8% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 66.9 | 2.0 / 3.7 / 3.8 | 1.00 | 16% | 0.500 | 1632 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 67.6 | 2.0 / 3.7 / 3.8 | 1.00 | 32% | 0.250 | 3264 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 67.3 | 2.2 / 3.7 / 3.8 | 1.00 | 63% | 0.125 | 6528 |
| Dedicated: one process per robot, one device | 12 | 7.59 | 0.0 | 0.0 | 76.0 | 2.2 / 3.7 / 3.8 | 1.00 | 87% | 0.083 | 9792 |
| Dedicated: one process per robot, one device | 16 | 344.18 | 213.8 | 185.6 | 85.0 | 2.5 / 88.2 / 97.9 | 1.00 | 95% | 0.062 | 13056 |
| Neura, multi-adapter batching | 1 | 0.00 | 0.0 | 0.0 | 77.8 | 4.2 / 5.8 / 5.8 | 1.00 | 9% | 1.000 | 816 |
| Neura, multi-adapter batching | 2 | 0.00 | 0.0 | 0.0 | 76.2 | 4.0 / 5.7 / 5.8 | 1.00 | 18% | 0.500 | 816 |
| Neura, multi-adapter batching | 4 | 0.00 | 0.0 | 0.0 | 76.9 | 4.1 / 5.7 / 5.8 | 1.00 | 36% | 0.250 | 816 |
| Neura, multi-adapter batching | 8 | 0.00 | 0.0 | 0.0 | 78.3 | 4.3 / 24.2 / 40.3 | 1.00 | 71% | 0.125 | 816 |
| Neura, multi-adapter batching | 12 | 836.84 | 807.5 | 457.5 | 15.1 | 64.6 / 98.3 / 100.3 | 3.01 | 99% | 0.083 | 856 |
| Neura, multi-adapter batching | 16 | 862.86 | 431.2 | 346.9 | 8.0 | 61.5 / 97.5 / 100.3 | 4.93 | 100% | 0.062 | 856 |

### 2 different full fine-tunes of one architecture: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 0.00 | 0.0 | 0.0 | 66.9 | 2.0 / 3.7 / 3.8 | 1.00 | 8%, 8% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.3 / 3.8 / 3.8 | 1.00 | 8% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 66.9 | 2.0 / 3.7 / 3.8 | 1.00 | 16% | 0.500 | 1632 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 67.6 | 2.0 / 3.7 / 3.8 | 1.00 | 32% | 0.250 | 3264 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 67.3 | 2.2 / 3.7 / 3.8 | 1.00 | 63% | 0.125 | 6528 |
| Dedicated: one process per robot, one device | 12 | 7.59 | 0.0 | 0.0 | 76.0 | 2.2 / 3.7 / 3.8 | 1.00 | 87% | 0.083 | 9792 |
| Dedicated: one process per robot, one device | 16 | 344.18 | 213.8 | 185.6 | 85.0 | 2.5 / 88.2 / 97.9 | 1.00 | 95% | 0.062 | 13056 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 69.4 | 4.3 / 5.8 / 5.8 | 1.00 | 8% | 1.000 | 816 |
| Neura, co-located groups | 2 | 0.00 | 0.0 | 0.0 | 68.5 | 4.0 / 5.7 / 5.8 | 1.00 | 16% | 0.500 | 1632 |
| Neura, co-located groups | 4 | 0.00 | 0.0 | 0.0 | 68.3 | 4.1 / 5.7 / 5.8 | 1.00 | 32% | 0.250 | 1632 |
| Neura, co-located groups | 8 | 0.00 | 0.0 | 0.0 | 68.7 | 4.1 / 5.7 / 5.8 | 1.00 | 64% | 0.125 | 1632 |
| Neura, co-located groups | 12 | 361.31 | 225.0 | 92.5 | 80.0 | 4.7 / 95.2 / 99.6 | 1.19 | 86% | 0.083 | 1696 |
| Neura, co-located groups | 16 | 891.69 | 579.4 | 420.0 | 18.1 | 55.7 / 97.7 / 99.8 | 2.54 | 100% | 0.062 | 1709 |

### 2 different full fine-tunes of one architecture: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 981.61 | 0.0 | 0.0 | 0.0 | 3.2 / 96.1 / 98.5 | 1.00 | 96%, 96% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 1 | 989.64 | 0.0 | 0.0 | 0.0 | 3.0 / 96.4 / 97.9 | 1.00 | 96% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 2 | 700.85 | 0.0 | 0.0 | 0.0 | 4.6 / 94.6 / 97.1 | 1.00 | 100% | 0.500 | 1632 |
| Neura, co-located groups | 1 | 987.91 | 0.0 | 0.0 | 0.0 | 5.1 / 96.5 / 98.6 | 1.00 | 96% | 1.000 | 816 |
| Neura, co-located groups | 2 | 717.61 | 0.0 | 0.0 | 0.0 | 5.4 / 95.5 / 97.9 | 1.00 | 100% | 0.500 | 1632 |

### Different architectures (ACT and Diffusion Policy): phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 815.55 | 0.0 | 0.0 | 70.3 | 33.0 / 96.1 / 97.7 | 1.00 | 8%, 99% | 1.000 | 901 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.3 / 3.8 / 3.8 | 1.00 | 8% | 1.000 | 816 |
| Dedicated: one process per robot, one device | 2 | 821.16 | 0.0 | 0.0 | 98.6 | 22.9 / 96.3 / 99.2 | 1.00 | 98% | 0.500 | 1717 |
| Dedicated: one process per robot, one device | 4 | 836.25 | 0.0 | 0.0 | 108.1 | 2.4 / 96.5 / 99.5 | 1.00 | 100% | 0.250 | 3434 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 69.4 | 4.3 / 5.8 / 5.8 | 1.00 | 8% | 1.000 | 816 |
| Neura, co-located groups | 2 | 821.69 | 0.0 | 0.0 | 100.8 | 20.8 / 98.2 / 99.8 | 1.00 | 98% | 0.500 | 1717 |
| Neura, co-located groups | 4 | 778.51 | 0.0 | 0.0 | 99.7 | 4.0 / 60.3 / 79.3 | 1.45 | 96% | 0.250 | 1717 |

## Real-time validation

The production gateway with real worker processes on the device and simulated robots over gRPC, next to the simulation of the same configuration (same robot count, warmup and window). Each cell is real / simulated. With a few hundred requests a p99 is close to the maximum: one collision moves it, so read it with the median and the miss rate. Late ticks: robot control ticks the harness itself ran late (high values mean the harness, not Neura, was overloaded).

| Case | Configuration | Robots | Requests | Misses / 1k | Starvation / robot-h | Age p50 ms | Age p99 ms | Mean batch | Device busy | Late ticks | Worker restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Same weights on every robot: phased arrivals | Dedicated: one device per robot | 1 | 45 / 45 | 0.00 / 0.00 | 0.0 / 0.0 | 2.0 / 2.3 | 6.2 / 3.8 | 1.00 / 1.00 | 8% / 8% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 1 | 45 / 45 | 0.00 / 0.00 | 0.0 / 0.0 | 4.4 / 4.3 | 5.7 / 5.8 | 1.00 / 1.00 | 8% / 8% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 4 | 181 / 180 | 5.53 / 0.00 | 0.0 / 0.0 | 4.5 / 4.0 | 45.5 / 5.7 | 1.00 / 1.00 | 33% / 32% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, FIFO | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 4.5 / 4.0 | 5.8 / 5.7 | 1.00 / 1.00 | 33% / 32% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Dedicated: one process per robot, one device | 3 | 135 / 135 | 7.41 / 0.00 | 0.0 / 0.0 | 2.0 / 1.8 | 3.7 / 3.7 | 1.00 / 1.00 | 25% / 24% | 0.1% | 0 |
| Same weights on every robot: synchronized (bursty) arrivals | Neura, deadline + fairness | 4 | 182 / 182 | 10.99 / 10.99 | 0.0 / 0.0 | 2.4 / 3.9 | 48.3 / 75.5 | 1.08 / 1.07 | 32% / 30% | 0.0% | 0 |
| Same weights on every robot: phased, with a background GPU job | Neura, deadline + fairness | 4 | 887 / 820 | 624.58 / 615.85 | 0.0 / 0.0 | 49.4 / 5.3 | 95.7 / 97.7 | 2.21 / 2.17 | 99% / 98% | 0.0% | 0 |
| 2 different full fine-tunes of one architecture: phased arrivals | Neura, co-located groups | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 4.6 / 4.0 | 6.5 / 5.7 | 1.00 / 1.00 | 34% / 32% | 0.0% | 0 |
| Different architectures (ACT and Diffusion Policy): phased arrivals | Neura, co-located groups | 4 | 517 / 514 | 504.84 / 793.77 | 0.0 / 0.0 | 17.3 / 3.8 | 95.0 / 69.8 | 1.36 / 1.47 | 99% / 96% | 0.0% | 0 |

## Method and limits

- Configurations follow design §10: the dedicated baselines run batch size 1 with the same model, precision, observation encoding and harness; Neura runs one worker per batch group with the scheduler policy named.
- Latency is measured through `neura-worker` processes over their Unix sockets, so serialization and IPC are included. Simulation draws forward-pass times from these measurements (lognormal fitted to p50 and p99) and shares the device between running passes by the measured co-location slowdown; beyond 3 concurrent workers the slowdown is extrapolated linearly.
- Multi-adapter batching is measured at the forward pass and simulated at the serving level; the data plane does not group robots by base model yet (design §9.5: built only if this benchmark shows it pays). Measured overheads below 1 are timing noise and are not credited.
- Simulated robots tick with no timing noise, and the simulated gateway decodes an observation in no time. Real robots drift by milliseconds, so near-simultaneous requests sometimes miss the batching window: the simulation is somewhat optimistic for synchronized arrivals near a freshness limit (compare the validation table).
- On a CPU host the background job and the worker share the CPU with the gateway and the simulated robots, so real ages there include the harness slowing down; the simulation models the device only. On a GPU the background job competes for the GPU alone.
- Admission control is set aside in the benchmark so it can measure what N robots get; production refuses sessions beyond the admission estimate.
- Robots that latch a fault are reset by a simulated operator after one second and counted.

## Reproduce

```
neura-bench run --preset cpu
```
