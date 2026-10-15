# Neura GPU-sharing benchmark: CPU: Intel(R) Xeon(R) Processor @ 2.80GHz

Generated 2026-10-15T03:46:34+00:00 from commit `173b188` with `neura-bench run --preset cpu` (torch 2.11.0, LeRobot 0.6.1). Regenerate the Markdown from `results.json` with `neura-bench report`.

> **This run is on a CPU development host, not a GPU.** It shows the harness working end to end and how sharing behaves on this host. Its numbers say nothing about GPU capacity: run `make bench` on the GPU runner for that.

Models: synthetic checkpoints with the real LeRobot architectures and random weights (ACT and Diffusion Policy, size `small`, cameras 128 x 96, fp32). Latency, memory and batching depend on architecture and shapes, not on what weights learned; closed-loop task success needs trained weights and is not measured here.

## Robots per device at the SLO

SLO: at most 1.0 deadline misses per 1,000 requests, at most 0.0 starvation events per robot-hour, and no robot faults. A configuration's capacity is the largest robot count at which it and every smaller count measured met the SLO (simulation with measured latency and contention, below).

| Case | Workload | One device per robot | One process per robot | Best Neura |
| --- | --- | --- | --- | --- |
| Same weights on every robot | phased arrivals | 1 | 16, fails at 24 | 12, fails at 16 (Neura, deadline + fairness) |
| Same weights on every robot | synchronized (bursty) arrivals | 1 | 8, fails at 12 | 8, fails at 12 (Neura, round robin) |
| Same weights on every robot | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, deadline + fairness) |
| Per-robot LoRA adapters of one base model | phased arrivals | 1 | 16, fails at 24 | 8, fails at 12 (Neura, multi-adapter batching) |
| Different full fine-tunes of one architecture | phased arrivals | 1 | 16, fails at 24 | 12, fails at 16 (Neura, co-located groups) |
| Different full fine-tunes of one architecture | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, co-located groups) |
| Different architectures (ACT and Diffusion Policy) | phased arrivals | 0 (fails at 1) | 1, fails at 2 | 1, fails at 2 (Neura, co-located groups) |

- **Same weights on every robot:** sharing pays. Neura, deadline + fairness serves 12 robots per device (0.083 device-hours per robot-hour, against 1 with a device per robot and 16 robots with a process per robot); at 16 robots it misses the SLO, so the crossover where one device stops paying is between 12 and 16.
- **Per-robot LoRA adapters of one base model:** sharing pays. Neura, multi-adapter batching serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot and 16 robots with a process per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Different full fine-tunes of one architecture:** sharing pays. Neura, co-located groups serves 12 robots per device (0.083 device-hours per robot-hour, against 1 with a device per robot and 16 robots with a process per robot); at 16 robots it misses the SLO, so the crossover where one device stops paying is between 12 and 16.
- **Different architectures (ACT and Diffusion Policy):** sharing does not pay on this device: Neura, co-located groups serves 1 robot(s) at the SLO.

## Measurements on the device

Forward-pass latency through a worker process, ACT:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 75.75 | 92.13 | 814 | 40 |
| 2 | 109.44 | 195.78 | 831 | 25 |
| 4 | 118.44 | 149.73 | 841 | 26 |
| 8 | 176.97 | 284.50 | 847 | 20 |

Forward-pass latency through a worker process, DIFFUSION:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 238.29 | 282.57 | 901 | 20 |
| 2 | 373.19 | 434.01 | 901 | 20 |
| 4 | 418.06 | 453.76 | 901 | 20 |
| 8 | 501.36 | 531.87 | 901 | 20 |

Co-location: k workers on the device, each running batches of one back to back. Alone: p50 73.464 ms. A background job (matrix multiplications) slows a lone worker by 3.691x.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 73.46 | 94.31 |
| 2 | 1.40 | 102.45 | 115.77 |
| 3 | 1.62 | 119.07 | 142.72 |

Simulated link, calibrated from one robot alone in real time: observation age at dispatch p50 1.935 ms, p99 5.138 ms (robot encoding, gRPC and the gateway's checks), simulated as 1.741 ms plus up to 3.396 ms.

Multi-adapter batching: 8 rank-16 LoRA adapters on 9 linear layers of the ACT base model; every request in a batch uses a different adapter. Parity with each adapter merged and run alone: max relative error 3.35e-07 (tolerance 1e-04), passed.

| Batch | Base p50 (ms) | Mixed adapters p50 (ms) | Overhead |
| --- | --- | --- | --- |
| 1 | 74.52 | 79.74 | 1.07 |
| 2 | 105.36 | 109.95 | 1.04 |
| 4 | 118.66 | 128.73 | 1.08 |
| 8 | 198.15 | 206.28 | 1.04 |

Admission control's estimate for ACT on this device: 0 robots (limited by acceptance_freshness), against 4 measured with synchronized arrivals. Admission assumes the worst case (every robot asks at once), so it should not exceed the synchronized capacity.

## Results

Per 1,000 requests: deadline misses (served after the predicted starvation time, too old to dispatch, or failed). Per robot-hour: starvation events and faults. Age: from the robot's capture to the start of the forward pass.

### Same weights on every robot: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.3 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.0 / 5.1 | 1.00 | 31% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 66.4 | 3.4 / 5.0 / 5.1 | 1.00 | 61% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 67.4 | 3.5 / 5.1 / 5.1 | 1.00 | 91% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 86.6 | 3.5 / 5.1 / 5.1 | 1.00 | 97% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 178.27 | 42.5 | 42.5 | 0.0 | 18.7 / 94.0 / 97.8 | 1.00 | 100% | 0.042 | 19536 |
| Dedicated: one process per robot, one device | 32 | 271.32 | 0.0 | 0.0 | 0.0 | 4.0 / 93.7 / 98.9 | 1.00 | 100% | 0.031 | 26048 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.3 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 814 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.0 / 5.1 | 1.00 | 31% | 0.250 | 814 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 66.4 | 3.4 / 5.0 / 5.1 | 1.00 | 61% | 0.125 | 814 |
| Neura, FIFO | 12 | 0.00 | 0.0 | 0.0 | 67.0 | 3.8 / 13.3 / 24.7 | 1.00 | 91% | 0.083 | 814 |
| Neura, FIFO | 16 | 355.06 | 577.5 | 427.5 | 20.2 | 60.1 / 95.1 / 97.8 | 4.47 | 99% | 0.062 | 847 |
| Neura, FIFO | 24 | 442.80 | 372.5 | 302.5 | 12.2 | 63.7 / 94.8 / 97.5 | 5.53 | 99% | 0.042 | 847 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.3 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 814 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.0 / 5.1 | 1.00 | 31% | 0.250 | 814 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 66.4 | 3.4 / 5.0 / 5.1 | 1.00 | 61% | 0.125 | 814 |
| Neura, round robin | 12 | 0.00 | 0.0 | 0.0 | 67.0 | 3.8 / 13.3 / 24.7 | 1.00 | 91% | 0.083 | 814 |
| Neura, round robin | 16 | 358.46 | 618.8 | 427.5 | 22.4 | 56.0 / 95.4 / 98.4 | 4.29 | 99% | 0.062 | 847 |
| Neura, round robin | 24 | 473.40 | 390.0 | 280.0 | 16.8 | 53.9 / 94.7 / 98.6 | 5.22 | 99% | 0.042 | 847 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.3 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 814 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.0 / 5.1 | 1.00 | 31% | 0.250 | 814 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 66.4 | 3.4 / 5.0 / 5.1 | 1.00 | 61% | 0.125 | 814 |
| Neura, deadline + fairness | 12 | 0.00 | 0.0 | 0.0 | 67.0 | 3.8 / 13.3 / 24.7 | 1.00 | 91% | 0.083 | 814 |
| Neura, deadline + fairness | 16 | 336.35 | 693.8 | 408.8 | 16.4 | 58.4 / 94.9 / 99.5 | 4.27 | 99% | 0.062 | 847 |
| Neura, deadline + fairness | 24 | 447.95 | 365.0 | 300.0 | 10.2 | 62.7 / 96.5 / 98.9 | 5.63 | 99% | 0.042 | 847 |

### Same weights on every robot: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 98.3 | 3.6 / 5.1 / 5.1 | 1.00 | 11% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 97.9 | 3.4 / 5.1 / 5.1 | 1.00 | 22% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 0.0 | 3.6 / 6.5 / 36.2 | 1.00 | 100% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 244.63 | 0.0 | 0.0 | 0.0 | 34.6 / 95.7 / 98.9 | 1.00 | 100% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 407.71 | 0.0 | 0.0 | 0.0 | 4.3 / 95.4 / 98.0 | 1.00 | 100% | 0.062 | 13024 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.7 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 814 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.1 / 5.1 | 1.00 | 31% | 0.250 | 836 |
| Neura, FIFO | 8 | 25.14 | 30.0 | 0.0 | 70.4 | 4.7 / 91.5 / 95.8 | 1.04 | 63% | 0.125 | 845 |
| Neura, FIFO | 12 | 259.86 | 715.0 | 360.0 | 40.3 | 59.4 / 94.6 / 98.6 | 2.65 | 92% | 0.083 | 847 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.7 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 814 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.1 / 5.1 | 1.00 | 31% | 0.250 | 836 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 74.6 | 4.6 / 63.6 / 87.3 | 1.00 | 60% | 0.125 | 845 |
| Neura, round robin | 12 | 249.38 | 760.0 | 405.0 | 30.5 | 56.3 / 93.8 / 98.0 | 3.01 | 92% | 0.083 | 847 |
| Neura, round robin | 16 | 325.31 | 577.5 | 371.2 | 24.7 | 57.5 / 95.1 / 97.4 | 4.31 | 97% | 0.062 | 847 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.7 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 814 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.1 / 5.1 | 1.00 | 31% | 0.250 | 836 |
| Neura, deadline + fairness | 8 | 2.05 | 0.0 | 0.0 | 74.9 | 4.1 / 67.3 / 87.4 | 1.01 | 62% | 0.125 | 845 |
| Neura, deadline + fairness | 12 | 258.42 | 805.0 | 375.0 | 33.7 | 57.2 / 93.3 / 97.2 | 2.77 | 93% | 0.083 | 847 |

### Same weights on every robot: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 272.73 | 0.0 | 0.0 | 0.0 | 36.9 / 96.0 / 97.4 | 1.00 | 97% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 272.73 | 0.0 | 0.0 | 0.0 | 36.9 / 96.0 / 97.4 | 1.00 | 97% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 482.88 | 0.0 | 0.0 | 0.0 | 3.5 / 94.2 / 97.5 | 1.00 | 100% | 0.500 | 1628 |
| Neura, FIFO | 1 | 272.73 | 0.0 | 0.0 | 0.0 | 36.9 / 96.0 / 97.4 | 1.00 | 97% | 1.000 | 814 |
| Neura, FIFO | 2 | 245.42 | 0.0 | 0.0 | 0.0 | 37.5 / 94.8 / 97.2 | 1.58 | 98% | 0.500 | 831 |
| Neura, round robin | 1 | 272.73 | 0.0 | 0.0 | 0.0 | 36.9 / 96.0 / 97.4 | 1.00 | 97% | 1.000 | 814 |
| Neura, round robin | 2 | 245.42 | 0.0 | 0.0 | 0.0 | 37.5 / 94.8 / 97.2 | 1.58 | 98% | 0.500 | 831 |
| Neura, deadline + fairness | 1 | 272.73 | 0.0 | 0.0 | 0.0 | 36.9 / 96.0 / 97.4 | 1.00 | 97% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 245.42 | 0.0 | 0.0 | 0.0 | 37.5 / 94.8 / 97.2 | 1.58 | 98% | 0.500 | 831 |

### Per-robot LoRA adapters of one base model: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.3 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.0 / 5.1 | 1.00 | 31% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 66.4 | 3.4 / 5.0 / 5.1 | 1.00 | 61% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 67.4 | 3.5 / 5.1 / 5.1 | 1.00 | 91% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 86.6 | 3.5 / 5.1 / 5.1 | 1.00 | 97% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 178.27 | 42.5 | 42.5 | 0.0 | 18.7 / 94.0 / 97.8 | 1.00 | 100% | 0.042 | 19536 |
| Dedicated: one process per robot, one device | 32 | 271.32 | 0.0 | 0.0 | 0.0 | 4.0 / 93.7 / 98.9 | 1.00 | 100% | 0.031 | 26048 |
| Neura, multi-adapter batching | 1 | 0.00 | 0.0 | 0.0 | 68.9 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Neura, multi-adapter batching | 2 | 0.00 | 0.0 | 0.0 | 67.2 | 3.3 / 5.1 / 5.1 | 1.00 | 16% | 0.500 | 814 |
| Neura, multi-adapter batching | 4 | 0.00 | 0.0 | 0.0 | 68.8 | 3.4 / 5.0 / 5.1 | 1.00 | 33% | 0.250 | 814 |
| Neura, multi-adapter batching | 8 | 0.00 | 0.0 | 0.0 | 68.5 | 3.4 / 5.0 / 5.1 | 1.00 | 65% | 0.125 | 814 |
| Neura, multi-adapter batching | 12 | 265.46 | 730.0 | 355.0 | 37.3 | 58.9 / 95.8 / 98.8 | 2.98 | 97% | 0.083 | 847 |
| Neura, multi-adapter batching | 16 | 324.50 | 525.0 | 401.2 | 10.8 | 58.6 / 94.2 / 97.5 | 5.13 | 99% | 0.062 | 847 |

### Different full fine-tunes of one architecture: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.3 / 5.1 / 5.1 | 1.00 | 7%, 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.3 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.0 / 5.1 | 1.00 | 31% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 66.4 | 3.4 / 5.0 / 5.1 | 1.00 | 61% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 67.4 | 3.5 / 5.1 / 5.1 | 1.00 | 91% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 86.6 | 3.5 / 5.1 / 5.1 | 1.00 | 97% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 178.27 | 42.5 | 42.5 | 0.0 | 18.7 / 94.0 / 97.8 | 1.00 | 100% | 0.042 | 19536 |
| Dedicated: one process per robot, one device | 32 | 271.32 | 0.0 | 0.0 | 0.0 | 4.0 / 93.7 / 98.9 | 1.00 | 100% | 0.031 | 26048 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Neura, co-located groups | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 3.3 / 5.1 / 5.1 | 1.00 | 15% | 0.500 | 1628 |
| Neura, co-located groups | 4 | 0.00 | 0.0 | 0.0 | 67.2 | 3.4 / 5.0 / 5.1 | 1.00 | 31% | 0.250 | 1628 |
| Neura, co-located groups | 8 | 0.00 | 0.0 | 0.0 | 66.4 | 3.4 / 5.0 / 5.1 | 1.00 | 61% | 0.125 | 1628 |
| Neura, co-located groups | 12 | 0.00 | 0.0 | 0.0 | 67.4 | 3.7 / 13.2 / 24.7 | 1.00 | 92% | 0.083 | 1628 |
| Neura, co-located groups | 16 | 412.83 | 761.2 | 427.5 | 23.6 | 61.7 / 96.7 / 99.4 | 2.13 | 100% | 0.062 | 1689 |
| Neura, co-located groups | 24 | 476.63 | 342.5 | 292.5 | 9.2 | 66.3 / 96.5 / 98.4 | 4.26 | 100% | 0.042 | 1694 |

### Different full fine-tunes of one architecture: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 288.70 | 0.0 | 0.0 | 0.0 | 35.7 / 94.5 / 98.1 | 1.00 | 96%, 96% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 272.73 | 0.0 | 0.0 | 0.0 | 36.9 / 96.0 / 97.4 | 1.00 | 97% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 482.88 | 0.0 | 0.0 | 0.0 | 3.5 / 94.2 / 97.5 | 1.00 | 100% | 0.500 | 1628 |
| Neura, co-located groups | 1 | 272.73 | 0.0 | 0.0 | 0.0 | 36.9 / 96.0 / 97.4 | 1.00 | 97% | 1.000 | 814 |
| Neura, co-located groups | 2 | 482.88 | 0.0 | 0.0 | 0.0 | 3.5 / 94.2 / 97.5 | 1.00 | 100% | 0.500 | 1628 |

### Different architectures (ACT and Diffusion Policy): phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 46.44 | 0.0 | 0.0 | 66.7 | 25.2 / 91.4 / 96.5 | 1.00 | 8%, 99% | 1.000 | 901 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 85.11 | 0.0 | 0.0 | 97.2 | 23.8 / 94.9 / 96.8 | 1.00 | 98% | 0.500 | 1715 |
| Dedicated: one process per robot, one device | 4 | 358.04 | 0.0 | 0.0 | 108.9 | 3.9 / 94.2 / 98.4 | 1.00 | 100% | 0.250 | 3430 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 66.7 | 3.5 / 5.0 / 5.1 | 1.00 | 8% | 1.000 | 814 |
| Neura, co-located groups | 2 | 85.11 | 0.0 | 0.0 | 97.2 | 23.8 / 94.9 / 96.8 | 1.00 | 98% | 0.500 | 1715 |
| Neura, co-located groups | 4 | 259.26 | 0.0 | 0.0 | 97.5 | 4.6 / 70.0 / 76.1 | 1.29 | 97% | 0.250 | 1715 |

## Real-time validation

The production gateway with real worker processes on the device and simulated robots over gRPC, next to the simulation of the same configuration. Late ticks: robot control ticks the harness itself ran late (high values mean the harness, not Neura, was overloaded).

| Case | Configuration | Robots | Misses / 1k (real / sim) | Starvation / robot-h | Age p99 ms | Mean batch | Device busy | Late ticks | Worker restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Same weights on every robot: phased arrivals | Dedicated: one device per robot | 1 | 0.00 / 0.00 | 0.0 / 0.0 | 3.7 / 5.0 | 1.00 / 1.00 | 7% / 8% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 1 | 0.00 / 0.00 | 0.0 / 0.0 | 4.2 / 5.0 | 1.00 / 1.00 | 7% / 8% | 0.2% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 4 | 0.00 / 0.00 | 0.0 / 0.0 | 3.0 / 5.0 | 1.00 / 1.00 | 29% / 31% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, FIFO | 4 | 0.00 / 0.00 | 0.0 / 0.0 | 4.3 / 5.0 | 1.00 / 1.00 | 30% / 31% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Dedicated: one process per robot, one device | 3 | 0.00 / 0.00 | 0.0 / 0.0 | 2.7 / 5.1 | 1.00 / 1.00 | 22% / 23% | 0.0% | 0 |
| Same weights on every robot: synchronized (bursty) arrivals | Neura, deadline + fairness | 4 | 0.00 / 0.00 | 0.0 / 0.0 | 68.9 / 5.1 | 1.00 / 1.00 | 31% / 31% | 0.0% | 0 |
| Same weights on every robot: phased, with a background GPU job | Neura, deadline + fairness | 4 | 312.34 / 224.09 | 0.0 / 0.0 | 96.0 / 94.1 | 2.05 / 2.62 | 99% / 98% | 0.0% | 0 |
| Different full fine-tunes of one architecture: phased arrivals | Neura, co-located groups | 4 | 0.00 / 0.00 | 0.0 / 0.0 | 4.0 / 5.0 | 1.00 / 1.00 | 33% / 31% | 0.0% | 0 |
| Different architectures (ACT and Diffusion Policy): phased arrivals | Neura, co-located groups | 4 | 205.13 / 259.26 | 0.0 / 0.0 | 94.0 / 74.2 | 1.25 / 1.28 | 99% / 97% | 0.0% | 0 |

## Method and limits

- Configurations follow design §10: the dedicated baselines run batch size 1 with the same model, precision, observation encoding and harness; Neura runs one worker per batch group with the scheduler policy named.
- Latency is measured through `neura-worker` processes over their Unix sockets, so serialization and IPC are included. Simulation draws forward-pass times from these measurements (lognormal fitted to p50 and p99) and shares the device between running passes by the measured co-location slowdown; beyond 3 concurrent workers the slowdown is extrapolated linearly.
- Multi-adapter batching is measured at the forward pass and simulated at the serving level; the data plane does not group robots by base model yet (design §9.5: built only if this benchmark shows it pays). Measured overheads below 1 are timing noise and are not credited.
- Admission control is set aside in the benchmark so it can measure what N robots get; production refuses sessions beyond the admission estimate.
- Robots that latch a fault are reset by a simulated operator after one second and counted.

## Reproduce

```
neura-bench run --preset cpu
```
