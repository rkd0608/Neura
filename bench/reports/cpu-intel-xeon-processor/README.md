# Neura GPU-sharing benchmark: CPU: Intel(R) Xeon(R) Processor @ 2.80GHz

Generated 2026-10-15T04:07:43+00:00 from commit `ce4a36a` with `neura-bench run --preset cpu` (torch 2.11.0, LeRobot 0.6.1). Regenerate the Markdown from `results.json` with `neura-bench report`.

> **This run is on a CPU development host, not a GPU.** It shows the harness working end to end and how sharing behaves on this host. Its numbers say nothing about GPU capacity: run `make bench` on the GPU runner for that.

Models: synthetic checkpoints with the real LeRobot architectures and random weights (ACT and Diffusion Policy, size `small`, cameras 128 x 96, fp32). Latency, memory and batching depend on architecture and shapes, not on what weights learned; closed-loop task success needs trained weights and is not measured here.

## Robots per device at the SLO

SLO: at most 1.0 deadline misses per 1,000 requests, at most 0.0 starvation events per robot-hour, and no robot faults. A configuration's capacity is the largest robot count at which it and every smaller count measured met the SLO (simulation with measured latency and contention, below).

| Case | Workload | One device per robot | One process per robot | Best Neura |
| --- | --- | --- | --- | --- |
| Same weights on every robot | phased arrivals | 1 | 24, fails at 32 | 12, fails at 16 (Neura, deadline + fairness) |
| Same weights on every robot | synchronized (bursty) arrivals | 1 | 16, fails at 24 | 8, fails at 12 (Neura, deadline + fairness) |
| Same weights on every robot | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, deadline + fairness) |
| Per-robot LoRA adapters of one base model | phased arrivals | 1 | 24, fails at 32 | 12, fails at 16 (Neura, multi-adapter batching) |
| Different full fine-tunes of one architecture | phased arrivals | 1 | 24, fails at 32 | 12, fails at 16 (Neura, co-located groups) |
| Different full fine-tunes of one architecture | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, co-located groups) |
| Different architectures (ACT and Diffusion Policy) | phased arrivals | 0 (fails at 1) | 1, fails at 2 | 1, fails at 2 (Neura, co-located groups) |

- **Same weights on every robot:** sharing pays. Neura, deadline + fairness serves 12 robots per device (0.083 device-hours per robot-hour, against 1 with a device per robot and 24 robots with a process per robot); at 16 robots it misses the SLO, so the crossover where one device stops paying is between 12 and 16.
- **Per-robot LoRA adapters of one base model:** sharing pays. Neura, multi-adapter batching serves 12 robots per device (0.083 device-hours per robot-hour, against 1 with a device per robot and 24 robots with a process per robot); at 16 robots it misses the SLO, so the crossover where one device stops paying is between 12 and 16.
- **Different full fine-tunes of one architecture:** sharing pays. Neura, co-located groups serves 12 robots per device (0.083 device-hours per robot-hour, against 1 with a device per robot and 24 robots with a process per robot); at 16 robots it misses the SLO, so the crossover where one device stops paying is between 12 and 16.
- **Different architectures (ACT and Diffusion Policy):** sharing does not pay on this device: Neura, co-located groups serves 1 robot(s) at the SLO.

## Measurements on the device

Forward-pass latency through a worker process, ACT:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 73.11 | 91.84 | 814 | 109 |
| 2 | 104.63 | 136.26 | 832 | 75 |
| 4 | 124.74 | 161.51 | 842 | 64 |
| 8 | 189.97 | 216.53 | 855 | 43 |

Forward-pass latency through a worker process, DIFFUSION:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 248.27 | 301.27 | 901 | 33 |
| 2 | 357.06 | 398.60 | 901 | 23 |
| 4 | 420.23 | 463.89 | 901 | 20 |
| 8 | 492.74 | 612.18 | 901 | 20 |

Co-location: k workers on the device, each running batches of one back to back. Alone: p50 77.618 ms. A background job (matrix multiplications) slows a lone worker by 3.675x.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 77.62 | 95.49 |
| 2 | 1.54 | 119.65 | 141.56 |
| 3 | 1.62 | 125.73 | 168.10 |

Simulated link, calibrated from one robot alone in real time: observation age at dispatch p50 2.02 ms, p99 2.586 ms (robot encoding, gRPC and the gateway's checks), simulated as 1.442 ms plus up to 1.155 ms.

Multi-adapter batching: 8 rank-16 LoRA adapters on 9 linear layers of the ACT base model; every request in a batch uses a different adapter. Parity with each adapter merged and run alone: max relative error 3.35e-07 (tolerance 1e-04), passed.

| Batch | Base p50 (ms) | Mixed adapters p50 (ms) | Overhead |
| --- | --- | --- | --- |
| 1 | 74.28 | 78.78 | 1.06 |
| 2 | 97.52 | 103.77 | 1.06 |
| 4 | 120.21 | 128.54 | 1.07 |
| 8 | 191.66 | 204.60 | 1.07 |

Admission control's estimate for ACT on this device: 0 robots (limited by acceptance_freshness), against 8 measured with synchronized arrivals. Admission assumes the worst case (every robot asks at once), so it should not exceed the synchronized capacity.

## Results

Per 1,000 requests: deadline misses (served after the predicted starvation time, too old to dispatch, or failed). Per robot-hour: starvation events and faults. Age: from the robot's capture to the start of the forward pass.

### Same weights on every robot: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 65.0 | 2.0 / 2.6 / 2.6 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 64.9 | 2.0 / 2.6 / 2.6 | 1.00 | 29% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 64.6 | 2.1 / 2.6 / 2.6 | 1.00 | 59% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 66.4 | 2.0 / 2.6 / 2.6 | 1.00 | 87% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 86.8 | 2.0 / 2.6 / 2.6 | 1.00 | 98% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 104.3 | 2.0 / 2.6 / 2.6 | 1.00 | 99% | 0.042 | 19536 |
| Dedicated: one process per robot, one device | 32 | 150.81 | 485.6 | 416.2 | 76.3 | 2.3 / 92.7 / 98.9 | 1.00 | 100% | 0.031 | 26048 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 65.0 | 2.0 / 2.6 / 2.6 | 1.00 | 15% | 0.500 | 814 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 64.9 | 2.0 / 2.6 / 2.6 | 1.00 | 29% | 0.250 | 814 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 64.6 | 2.1 / 2.6 / 2.6 | 1.00 | 59% | 0.125 | 814 |
| Neura, FIFO | 12 | 0.00 | 0.0 | 0.0 | 66.5 | 2.2 / 34.1 / 43.6 | 1.00 | 88% | 0.083 | 814 |
| Neura, FIFO | 16 | 347.60 | 590.6 | 420.0 | 21.3 | 52.7 / 96.8 / 98.7 | 4.58 | 99% | 0.062 | 855 |
| Neura, FIFO | 24 | 449.27 | 316.2 | 257.5 | 15.9 | 48.5 / 96.0 / 98.8 | 5.68 | 99% | 0.042 | 855 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 65.0 | 2.0 / 2.6 / 2.6 | 1.00 | 15% | 0.500 | 814 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 64.9 | 2.0 / 2.6 / 2.6 | 1.00 | 29% | 0.250 | 814 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 64.6 | 2.1 / 2.6 / 2.6 | 1.00 | 59% | 0.125 | 814 |
| Neura, round robin | 12 | 0.00 | 0.0 | 0.0 | 66.5 | 2.2 / 34.1 / 43.6 | 1.00 | 88% | 0.083 | 814 |
| Neura, round robin | 16 | 349.43 | 616.9 | 433.1 | 23.5 | 50.9 / 96.3 / 98.7 | 4.25 | 99% | 0.062 | 855 |
| Neura, round robin | 24 | 432.94 | 293.8 | 240.0 | 14.7 | 42.6 / 96.2 / 99.0 | 5.54 | 99% | 0.042 | 855 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 65.0 | 2.0 / 2.6 / 2.6 | 1.00 | 15% | 0.500 | 814 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 64.9 | 2.0 / 2.6 / 2.6 | 1.00 | 29% | 0.250 | 814 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 64.6 | 2.1 / 2.6 / 2.6 | 1.00 | 59% | 0.125 | 814 |
| Neura, deadline + fairness | 12 | 0.00 | 0.0 | 0.0 | 66.5 | 2.2 / 34.1 / 43.6 | 1.00 | 88% | 0.083 | 814 |
| Neura, deadline + fairness | 16 | 313.91 | 605.6 | 435.0 | 17.1 | 48.7 / 96.3 / 98.7 | 4.75 | 99% | 0.062 | 855 |
| Neura, deadline + fairness | 24 | 436.68 | 267.5 | 220.0 | 15.1 | 48.5 / 96.5 / 98.1 | 5.74 | 99% | 0.042 | 855 |

### Same weights on every robot: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 99.6 | 2.0 / 2.6 / 2.6 | 1.00 | 12% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 85.0 | 2.1 / 2.6 / 2.6 | 1.00 | 24% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 97.5 | 2.1 / 2.6 / 2.6 | 1.00 | 39% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 92.3 | 2.1 / 2.6 / 2.6 | 1.00 | 66% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 91.3 | 2.1 / 2.6 / 2.6 | 1.00 | 87% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 57.07 | 0.0 | 0.0 | 0.0 | 29.5 / 94.8 / 99.0 | 1.00 | 100% | 0.042 | 19536 |
| Dedicated: one process per robot, one device | 32 | 268.34 | 0.0 | 0.0 | 0.0 | 33.7 / 97.0 / 98.7 | 1.00 | 100% | 0.031 | 26048 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 65.8 | 2.4 / 22.4 / 24.8 | 1.00 | 15% | 0.500 | 814 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 66.2 | 2.2 / 21.9 / 31.5 | 1.00 | 30% | 0.250 | 832 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 65.7 | 2.2 / 22.1 / 33.6 | 1.00 | 59% | 0.125 | 845 |
| Neura, FIFO | 12 | 215.10 | 527.5 | 287.5 | 46.1 | 49.3 / 97.2 / 98.3 | 2.94 | 94% | 0.083 | 855 |
| Neura, FIFO | 16 | 262.75 | 333.8 | 268.1 | 14.8 | 44.7 / 93.5 / 98.2 | 5.60 | 98% | 0.062 | 855 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 65.8 | 2.4 / 22.4 / 24.8 | 1.00 | 15% | 0.500 | 814 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 66.2 | 2.2 / 21.9 / 31.5 | 1.00 | 30% | 0.250 | 832 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 65.7 | 2.2 / 22.1 / 33.6 | 1.00 | 59% | 0.125 | 845 |
| Neura, round robin | 12 | 210.40 | 592.5 | 372.5 | 37.3 | 52.5 / 95.2 / 98.0 | 3.45 | 95% | 0.083 | 855 |
| Neura, round robin | 16 | 263.22 | 345.0 | 279.4 | 18.7 | 45.0 / 94.0 / 98.7 | 5.41 | 98% | 0.062 | 855 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 65.8 | 2.4 / 22.4 / 24.8 | 1.00 | 15% | 0.500 | 814 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 66.2 | 2.2 / 21.9 / 31.5 | 1.00 | 30% | 0.250 | 832 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 65.7 | 2.2 / 22.1 / 33.6 | 1.00 | 59% | 0.125 | 845 |
| Neura, deadline + fairness | 12 | 173.75 | 535.0 | 335.0 | 25.6 | 51.4 / 94.0 / 97.7 | 4.38 | 97% | 0.083 | 855 |
| Neura, deadline + fairness | 16 | 241.60 | 354.4 | 262.5 | 12.3 | 46.7 / 92.9 / 98.7 | 5.59 | 98% | 0.062 | 855 |

### Same weights on every robot: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 237.59 | 0.0 | 0.0 | 0.0 | 32.4 / 96.6 / 98.6 | 1.00 | 97% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 237.59 | 0.0 | 0.0 | 0.0 | 32.4 / 96.6 / 98.6 | 1.00 | 97% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 454.06 | 0.0 | 0.0 | 0.0 | 2.1 / 83.4 / 97.2 | 1.00 | 99% | 0.500 | 1628 |
| Neura, FIFO | 1 | 237.59 | 0.0 | 0.0 | 0.0 | 32.4 / 96.6 / 98.6 | 1.00 | 97% | 1.000 | 814 |
| Neura, FIFO | 2 | 289.81 | 0.0 | 0.0 | 0.0 | 21.3 / 91.5 / 97.1 | 1.53 | 97% | 0.500 | 832 |
| Neura, round robin | 1 | 237.59 | 0.0 | 0.0 | 0.0 | 32.4 / 96.6 / 98.6 | 1.00 | 97% | 1.000 | 814 |
| Neura, round robin | 2 | 288.75 | 0.0 | 0.0 | 0.0 | 23.5 / 88.3 / 97.1 | 1.53 | 98% | 0.500 | 832 |
| Neura, deadline + fairness | 1 | 237.59 | 0.0 | 0.0 | 0.0 | 32.4 / 96.6 / 98.6 | 1.00 | 97% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 286.51 | 0.0 | 0.0 | 0.0 | 22.7 / 91.5 / 97.1 | 1.53 | 98% | 0.500 | 832 |

### Per-robot LoRA adapters of one base model: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 65.0 | 2.0 / 2.6 / 2.6 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 64.9 | 2.0 / 2.6 / 2.6 | 1.00 | 29% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 64.6 | 2.1 / 2.6 / 2.6 | 1.00 | 59% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 66.4 | 2.0 / 2.6 / 2.6 | 1.00 | 87% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 86.8 | 2.0 / 2.6 / 2.6 | 1.00 | 98% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 104.3 | 2.0 / 2.6 / 2.6 | 1.00 | 99% | 0.042 | 19536 |
| Dedicated: one process per robot, one device | 32 | 150.81 | 485.6 | 416.2 | 76.3 | 2.3 / 92.7 / 98.9 | 1.00 | 100% | 0.031 | 26048 |
| Neura, multi-adapter batching | 1 | 0.00 | 0.0 | 0.0 | 66.4 | 2.0 / 2.5 / 2.6 | 1.00 | 8% | 1.000 | 814 |
| Neura, multi-adapter batching | 2 | 0.00 | 0.0 | 0.0 | 66.4 | 2.0 / 2.6 / 2.6 | 1.00 | 16% | 0.500 | 814 |
| Neura, multi-adapter batching | 4 | 0.00 | 0.0 | 0.0 | 66.7 | 2.0 / 2.6 / 2.6 | 1.00 | 31% | 0.250 | 814 |
| Neura, multi-adapter batching | 8 | 0.00 | 0.0 | 0.0 | 66.4 | 2.0 / 2.6 / 2.6 | 1.00 | 62% | 0.125 | 814 |
| Neura, multi-adapter batching | 12 | 0.00 | 0.0 | 0.0 | 67.5 | 2.3 / 20.9 / 30.9 | 1.00 | 93% | 0.083 | 814 |
| Neura, multi-adapter batching | 16 | 330.31 | 431.2 | 341.2 | 10.8 | 45.5 / 95.9 / 98.2 | 5.50 | 99% | 0.062 | 855 |
| Neura, multi-adapter batching | 24 | 402.86 | 200.0 | 176.2 | 10.5 | 46.5 / 95.0 / 98.6 | 6.07 | 99% | 0.042 | 855 |

### Different full fine-tunes of one architecture: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 0.00 | 0.0 | 0.0 | 65.0 | 2.0 / 2.6 / 2.6 | 1.00 | 7%, 7% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 65.0 | 2.0 / 2.6 / 2.6 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 64.9 | 2.0 / 2.6 / 2.6 | 1.00 | 29% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 64.6 | 2.1 / 2.6 / 2.6 | 1.00 | 59% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 66.4 | 2.0 / 2.6 / 2.6 | 1.00 | 87% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 0.00 | 0.0 | 0.0 | 86.8 | 2.0 / 2.6 / 2.6 | 1.00 | 98% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 0.00 | 0.0 | 0.0 | 104.3 | 2.0 / 2.6 / 2.6 | 1.00 | 99% | 0.042 | 19536 |
| Dedicated: one process per robot, one device | 32 | 150.81 | 485.6 | 416.2 | 76.3 | 2.3 / 92.7 / 98.9 | 1.00 | 100% | 0.031 | 26048 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Neura, co-located groups | 2 | 0.00 | 0.0 | 0.0 | 65.0 | 2.0 / 2.6 / 2.6 | 1.00 | 15% | 0.500 | 1628 |
| Neura, co-located groups | 4 | 0.00 | 0.0 | 0.0 | 64.9 | 2.0 / 2.6 / 2.6 | 1.00 | 29% | 0.250 | 1628 |
| Neura, co-located groups | 8 | 0.00 | 0.0 | 0.0 | 64.6 | 2.1 / 2.6 / 2.6 | 1.00 | 59% | 0.125 | 1628 |
| Neura, co-located groups | 12 | 0.00 | 0.0 | 0.0 | 66.1 | 2.1 / 34.8 / 49.9 | 1.00 | 88% | 0.083 | 1628 |
| Neura, co-located groups | 16 | 484.84 | 607.5 | 433.1 | 17.1 | 58.3 / 97.4 / 98.9 | 2.32 | 100% | 0.062 | 1702 |
| Neura, co-located groups | 24 | 583.23 | 310.0 | 251.2 | 9.7 | 68.2 / 97.4 / 98.8 | 3.61 | 100% | 0.042 | 1710 |

### Different full fine-tunes of one architecture: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 231.11 | 0.0 | 0.0 | 0.0 | 36.1 / 96.0 / 98.1 | 1.00 | 97%, 97% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 237.59 | 0.0 | 0.0 | 0.0 | 32.4 / 96.6 / 98.6 | 1.00 | 97% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 454.06 | 0.0 | 0.0 | 0.0 | 2.1 / 83.4 / 97.2 | 1.00 | 99% | 0.500 | 1628 |
| Neura, co-located groups | 1 | 237.59 | 0.0 | 0.0 | 0.0 | 32.4 / 96.6 / 98.6 | 1.00 | 97% | 1.000 | 814 |
| Neura, co-located groups | 2 | 454.06 | 0.0 | 0.0 | 0.0 | 2.1 / 83.4 / 97.2 | 1.00 | 99% | 0.500 | 1628 |

### Different architectures (ACT and Diffusion Policy): phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 104.07 | 0.0 | 0.0 | 65.0 | 29.7 / 96.6 / 98.3 | 1.00 | 7%, 98% | 1.000 | 901 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 148.59 | 0.0 | 0.0 | 100.6 | 20.4 / 96.0 / 97.3 | 1.00 | 98% | 0.500 | 1715 |
| Dedicated: one process per robot, one device | 4 | 389.20 | 0.0 | 0.0 | 104.3 | 2.0 / 92.7 / 97.4 | 1.00 | 100% | 0.250 | 3430 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 63.3 | 2.0 / 2.5 / 2.6 | 1.00 | 7% | 1.000 | 814 |
| Neura, co-located groups | 2 | 148.59 | 0.0 | 0.0 | 100.6 | 20.4 / 96.0 / 97.3 | 1.00 | 98% | 0.500 | 1715 |
| Neura, co-located groups | 4 | 252.86 | 0.0 | 0.0 | 100.3 | 2.5 / 92.1 / 96.5 | 1.30 | 98% | 0.250 | 1715 |

## Real-time validation

The production gateway with real worker processes on the device and simulated robots over gRPC, next to the simulation of the same configuration (same robot count, warmup and window). Each cell is real / simulated. With a few hundred requests a p99 is close to the maximum: one collision moves it, so read it with the median and the miss rate. Late ticks: robot control ticks the harness itself ran late (high values mean the harness, not Neura, was overloaded).

| Case | Configuration | Robots | Requests | Misses / 1k | Starvation / robot-h | Age p50 ms | Age p99 ms | Mean batch | Device busy | Late ticks | Worker restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Same weights on every robot: phased arrivals | Dedicated: one device per robot | 1 | 45 / 45 | 0.00 / 0.00 | 0.0 / 0.0 | 2.0 / 1.9 | 3.4 / 2.5 | 1.00 / 1.00 | 8% / 7% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 1 | 45 / 45 | 0.00 / 0.00 | 0.0 / 0.0 | 2.2 / 1.9 | 4.2 / 2.5 | 1.00 / 1.00 | 8% / 7% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 2.0 / 2.0 | 4.1 / 2.6 | 1.00 / 1.00 | 33% / 29% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, FIFO | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 2.0 / 2.0 | 3.5 / 2.6 | 1.00 / 1.00 | 33% / 29% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Dedicated: one process per robot, one device | 3 | 135 / 135 | 0.00 / 0.00 | 0.0 / 0.0 | 2.0 / 2.1 | 4.1 / 2.6 | 1.00 / 1.00 | 26% / 22% | 0.0% | 0 |
| Same weights on every robot: synchronized (bursty) arrivals | Neura, deadline + fairness | 4 | 210 / 180 | 38.09 / 0.00 | 40.0 / 0.0 | 2.2 / 2.2 | 83.4 / 22.0 | 1.21 / 1.00 | 33% / 30% | 0.0% | 0 |
| Same weights on every robot: phased, with a background GPU job | Neura, deadline + fairness | 4 | 884 / 812 | 312.22 / 252.46 | 0.0 / 0.0 | 52.8 / 32.8 | 99.1 / 93.2 | 2.10 / 2.56 | 99% / 99% | 0.0% | 0 |
| Different full fine-tunes of one architecture: phased arrivals | Neura, co-located groups | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 1.9 / 2.0 | 4.2 / 2.6 | 1.00 / 1.00 | 32% / 29% | 0.0% | 0 |
| Different architectures (ACT and Diffusion Policy): phased arrivals | Neura, co-located groups | 4 | 515 / 492 | 182.52 / 243.90 | 0.0 / 0.0 | 3.3 / 2.5 | 97.1 / 82.6 | 1.36 / 1.31 | 99% / 97% | 0.0% | 0 |

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
