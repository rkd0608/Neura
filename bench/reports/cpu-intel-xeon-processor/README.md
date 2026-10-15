# Neura GPU-sharing benchmark: CPU: Intel(R) Xeon(R) Processor @ 2.80GHz

Generated 2026-10-15T04:50:58+00:00 from commit `9f7cebb` with `neura-bench run --preset cpu` (torch 2.11.0, LeRobot 0.6.1). Regenerate the Markdown from `results.json` with `neura-bench report`.

> **This run is on a CPU development host, not a GPU.** It shows the harness working end to end and how sharing behaves on this host. Its numbers say nothing about GPU capacity: run `make bench` on the GPU runner for that.

Models: synthetic checkpoints with the real LeRobot architectures and random weights (ACT and Diffusion Policy, size `small`, cameras 128 x 96, fp32). Latency, memory and batching depend on architecture and shapes, not on what weights learned; closed-loop task success needs trained weights and is not measured here.

## Robots per device at the SLO

SLO: at most 1.0 deadline misses per 1,000 requests, at most 0.0 starvation events per robot-hour, and no robot faults. A configuration's capacity is the largest robot count at which it and every smaller count measured met the SLO (simulation with measured latency and contention, below).

| Case | Workload | One device per robot | One process per robot | Best Neura |
| --- | --- | --- | --- | --- |
| Same weights on every robot | phased arrivals | 1 | 12, fails at 16 | 8, fails at 12 (Neura, deadline + fairness) |
| Same weights on every robot | synchronized (bursty) arrivals | 1 | 4, fails at 8 | 4, fails at 8 (Neura, deadline + fairness) |
| Same weights on every robot | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, deadline + fairness) |
| Per-robot LoRA adapters of one base model | phased arrivals | 1 | 12, fails at 16 | 8, fails at 12 (Neura, multi-adapter batching) |
| Different full fine-tunes of one architecture | phased arrivals | 1 | 12, fails at 16 | 8, fails at 12 (Neura, co-located groups) |
| Different full fine-tunes of one architecture | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, co-located groups) |
| Different architectures (ACT and Diffusion Policy) | phased arrivals | 0 (fails at 1) | 1, fails at 2 | 1, fails at 2 (Neura, co-located groups) |

- **Same weights on every robot:** sharing pays. Neura, deadline + fairness serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot and 12 robots with a process per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Per-robot LoRA adapters of one base model:** sharing pays. Neura, multi-adapter batching serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot and 12 robots with a process per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Different full fine-tunes of one architecture:** sharing pays. Neura, co-located groups serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot and 12 robots with a process per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Different architectures (ACT and Diffusion Policy):** sharing does not pay on this device: Neura, co-located groups serves 1 robot(s) at the SLO.

## Measurements on the device

Forward-pass latency through a worker process, ACT:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 78.58 | 106.87 | 814 | 102 |
| 2 | 105.61 | 139.48 | 831 | 76 |
| 4 | 132.10 | 169.84 | 839 | 61 |
| 8 | 207.25 | 233.44 | 848 | 39 |

Forward-pass latency through a worker process, DIFFUSION:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 248.09 | 290.57 | 901 | 33 |
| 2 | 420.26 | 458.30 | 901 | 20 |
| 4 | 439.22 | 501.45 | 901 | 20 |
| 8 | 516.74 | 553.08 | 901 | 20 |

Co-location: k workers on the device, each running batches of one back to back. Alone: p50 77.954 ms. A background job (matrix multiplications) slows a lone worker by 3.921x.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 77.95 | 95.95 |
| 2 | 1.46 | 113.75 | 135.74 |
| 3 | 1.69 | 131.91 | 163.85 |

Simulated link, calibrated from one robot alone in real time: observation age at dispatch p50 2.062 ms, p99 2.94 ms (robot encoding, gRPC and the gateway's checks), simulated as 1.166 ms plus up to 1.792 ms.

Multi-adapter batching: 8 rank-16 LoRA adapters on 9 linear layers of the ACT base model; every request in a batch uses a different adapter. Parity with each adapter merged and run alone: max relative error 3.35e-07 (tolerance 1e-04), passed.

| Batch | Base p50 (ms) | Mixed adapters p50 (ms) | Overhead |
| --- | --- | --- | --- |
| 1 | 80.13 | 84.67 | 1.06 |
| 2 | 106.32 | 111.21 | 1.05 |
| 4 | 126.80 | 132.37 | 1.04 |
| 8 | 198.50 | 209.47 | 1.05 |

Admission control's estimate for ACT on this device: 0 robots (limited by dispatch_freshness), against 4 measured with synchronized arrivals. Admission assumes the worst case (every robot asks at once), so it should not exceed the synchronized capacity.

## Results

Per 1,000 requests: deadline misses (served after the predicted starvation time, too old to dispatch, or failed). Per robot-hour: starvation events and faults. Age: from the robot's capture to the start of the forward pass.

### Same weights on every robot: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 66.9 | 2.0 / 2.9 / 2.9 | 1.00 | 16% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 67.7 | 2.1 / 2.9 / 3.0 | 1.00 | 32% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 67.3 | 2.0 / 2.9 / 3.0 | 1.00 | 63% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 71.7 | 2.1 / 2.9 / 3.0 | 1.00 | 91% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 382.27 | 213.8 | 198.8 | 42.6 | 2.4 / 96.0 / 98.9 | 1.00 | 99% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 210.88 | 0.0 | 0.0 | 0.0 | 3.0 / 96.3 / 99.2 | 1.00 | 100% | 0.042 | 19536 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 68.6 | 3.7 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 66.5 | 3.7 / 4.9 / 4.9 | 1.00 | 16% | 0.500 | 814 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 68.4 | 3.6 / 4.9 / 5.0 | 1.00 | 32% | 0.250 | 814 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 68.1 | 3.5 / 6.0 / 21.3 | 1.00 | 63% | 0.125 | 814 |
| Neura, FIFO | 12 | 314.73 | 507.5 | 352.5 | 46.7 | 59.4 / 96.6 / 98.8 | 2.25 | 97% | 0.083 | 848 |
| Neura, FIFO | 16 | 392.73 | 451.9 | 386.2 | 10.4 | 62.8 / 97.4 / 98.9 | 4.73 | 100% | 0.062 | 848 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 68.6 | 3.7 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 66.5 | 3.7 / 4.9 / 4.9 | 1.00 | 16% | 0.500 | 814 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 68.4 | 3.6 / 4.9 / 5.0 | 1.00 | 32% | 0.250 | 814 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 68.1 | 3.5 / 6.0 / 21.3 | 1.00 | 63% | 0.125 | 814 |
| Neura, round robin | 12 | 320.58 | 542.5 | 320.0 | 48.7 | 57.6 / 97.0 / 99.2 | 1.98 | 96% | 0.083 | 848 |
| Neura, round robin | 16 | 446.99 | 485.6 | 354.4 | 20.9 | 61.9 / 97.2 / 99.0 | 3.91 | 100% | 0.062 | 848 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 68.6 | 3.7 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 66.5 | 3.7 / 4.9 / 4.9 | 1.00 | 16% | 0.500 | 814 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 68.4 | 3.6 / 4.9 / 5.0 | 1.00 | 32% | 0.250 | 814 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 68.1 | 3.5 / 6.0 / 21.3 | 1.00 | 63% | 0.125 | 814 |
| Neura, deadline + fairness | 12 | 298.11 | 560.0 | 342.5 | 43.3 | 60.6 / 96.5 / 98.8 | 2.38 | 97% | 0.083 | 848 |
| Neura, deadline + fairness | 16 | 374.50 | 412.5 | 331.9 | 5.5 | 63.5 / 97.3 / 99.1 | 4.77 | 100% | 0.062 | 848 |

### Same weights on every robot: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 80.0 | 2.1 / 2.9 / 3.0 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 73.2 | 2.2 / 2.9 / 3.0 | 1.00 | 31% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 12.14 | 11.2 | 11.2 | 0.0 | 2.8 / 88.9 / 98.3 | 1.00 | 100% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 300.73 | 0.0 | 0.0 | 0.0 | 20.1 / 96.4 / 98.5 | 1.00 | 100% | 0.083 | 9768 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 68.6 | 3.7 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 68.6 | 3.8 / 4.9 / 4.9 | 1.00 | 16% | 0.500 | 814 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 69.0 | 3.7 / 4.9 / 13.1 | 1.00 | 32% | 0.250 | 831 |
| Neura, FIFO | 8 | 47.58 | 56.2 | 15.0 | 78.9 | 4.3 / 90.7 / 94.9 | 1.14 | 65% | 0.125 | 843 |
| Neura, FIFO | 12 | 352.06 | 727.5 | 422.5 | 32.1 | 61.4 / 96.9 / 98.6 | 2.67 | 95% | 0.083 | 848 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 68.6 | 3.7 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 68.6 | 3.8 / 4.9 / 4.9 | 1.00 | 16% | 0.500 | 814 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 69.0 | 3.7 / 4.9 / 13.1 | 1.00 | 32% | 0.250 | 831 |
| Neura, round robin | 8 | 26.16 | 22.5 | 7.5 | 75.6 | 4.0 / 85.6 / 97.9 | 1.06 | 63% | 0.125 | 843 |
| Neura, round robin | 12 | 374.25 | 780.0 | 400.0 | 36.9 | 59.4 / 96.7 / 99.1 | 2.28 | 94% | 0.083 | 848 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 68.6 | 3.7 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 68.6 | 3.8 / 4.9 / 4.9 | 1.00 | 16% | 0.500 | 814 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 69.0 | 3.7 / 4.9 / 13.1 | 1.00 | 32% | 0.250 | 831 |
| Neura, deadline + fairness | 8 | 28.27 | 26.2 | 0.0 | 76.2 | 4.3 / 85.7 / 98.0 | 1.04 | 65% | 0.125 | 843 |
| Neura, deadline + fairness | 12 | 328.15 | 735.0 | 465.0 | 22.6 | 65.9 / 95.9 / 98.9 | 3.02 | 97% | 0.083 | 848 |

### Same weights on every robot: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 337.98 | 0.0 | 0.0 | 0.0 | 2.8 / 96.4 / 98.3 | 1.00 | 96% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 337.98 | 0.0 | 0.0 | 0.0 | 2.8 / 96.4 / 98.3 | 1.00 | 96% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 287.18 | 0.0 | 0.0 | 0.0 | 2.7 / 93.9 / 98.0 | 1.00 | 100% | 0.500 | 1628 |
| Neura, FIFO | 1 | 351.92 | 0.0 | 0.0 | 0.0 | 4.7 / 95.8 / 97.3 | 1.00 | 96% | 1.000 | 814 |
| Neura, FIFO | 2 | 389.49 | 0.0 | 0.0 | 0.0 | 3.9 / 89.3 / 96.3 | 1.99 | 96% | 0.500 | 831 |
| Neura, round robin | 1 | 351.92 | 0.0 | 0.0 | 0.0 | 4.7 / 95.8 / 97.3 | 1.00 | 96% | 1.000 | 814 |
| Neura, round robin | 2 | 389.49 | 0.0 | 0.0 | 0.0 | 3.9 / 89.3 / 96.3 | 1.99 | 96% | 0.500 | 831 |
| Neura, deadline + fairness | 1 | 351.92 | 0.0 | 0.0 | 0.0 | 4.7 / 95.8 / 97.3 | 1.00 | 96% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 389.49 | 0.0 | 0.0 | 0.0 | 3.9 / 89.3 / 96.3 | 1.99 | 96% | 0.500 | 831 |

### Per-robot LoRA adapters of one base model: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 66.9 | 2.0 / 2.9 / 2.9 | 1.00 | 16% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 67.7 | 2.1 / 2.9 / 3.0 | 1.00 | 32% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 67.3 | 2.0 / 2.9 / 3.0 | 1.00 | 63% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 71.7 | 2.1 / 2.9 / 3.0 | 1.00 | 91% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 382.27 | 213.8 | 198.8 | 42.6 | 2.4 / 96.0 / 98.9 | 1.00 | 99% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 210.88 | 0.0 | 0.0 | 0.0 | 3.0 / 96.3 / 99.2 | 1.00 | 100% | 0.042 | 19536 |
| Neura, multi-adapter batching | 1 | 0.00 | 0.0 | 0.0 | 70.8 | 2.3 / 2.9 / 3.0 | 1.00 | 8% | 1.000 | 814 |
| Neura, multi-adapter batching | 2 | 0.00 | 0.0 | 0.0 | 70.8 | 2.0 / 2.9 / 2.9 | 1.00 | 17% | 0.500 | 814 |
| Neura, multi-adapter batching | 4 | 0.00 | 0.0 | 0.0 | 70.7 | 2.0 / 2.9 / 3.0 | 1.00 | 34% | 0.250 | 814 |
| Neura, multi-adapter batching | 8 | 0.00 | 0.0 | 0.0 | 70.7 | 2.0 / 2.9 / 3.0 | 1.00 | 67% | 0.125 | 814 |
| Neura, multi-adapter batching | 12 | 298.81 | 482.5 | 390.0 | 9.2 | 63.3 / 97.1 / 98.2 | 4.46 | 99% | 0.083 | 848 |
| Neura, multi-adapter batching | 16 | 295.29 | 240.0 | 219.4 | 3.7 | 60.7 / 96.5 / 98.4 | 5.96 | 100% | 0.062 | 848 |

### Different full fine-tunes of one architecture: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 0.00 | 0.0 | 0.0 | 66.9 | 2.0 / 2.9 / 2.9 | 1.00 | 8%, 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 66.9 | 2.0 / 2.9 / 2.9 | 1.00 | 16% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 67.7 | 2.1 / 2.9 / 3.0 | 1.00 | 32% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 67.3 | 2.0 / 2.9 / 3.0 | 1.00 | 63% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 71.7 | 2.1 / 2.9 / 3.0 | 1.00 | 91% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 382.27 | 213.8 | 198.8 | 42.6 | 2.4 / 96.0 / 98.9 | 1.00 | 99% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 210.88 | 0.0 | 0.0 | 0.0 | 3.0 / 96.3 / 99.2 | 1.00 | 100% | 0.042 | 19536 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 68.6 | 3.7 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, co-located groups | 2 | 0.00 | 0.0 | 0.0 | 66.5 | 3.7 / 4.9 / 4.9 | 1.00 | 16% | 0.500 | 1628 |
| Neura, co-located groups | 4 | 0.00 | 0.0 | 0.0 | 68.4 | 3.6 / 4.9 / 5.0 | 1.00 | 32% | 0.250 | 1628 |
| Neura, co-located groups | 8 | 0.00 | 0.0 | 0.0 | 68.1 | 3.5 / 6.0 / 21.3 | 1.00 | 63% | 0.125 | 1628 |
| Neura, co-located groups | 12 | 229.26 | 300.0 | 155.0 | 76.2 | 4.1 / 95.3 / 99.4 | 1.54 | 82% | 0.083 | 1684 |
| Neura, co-located groups | 16 | 479.72 | 620.6 | 451.9 | 16.7 | 54.7 / 97.6 / 99.1 | 2.32 | 100% | 0.062 | 1690 |

### Different full fine-tunes of one architecture: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 337.43 | 0.0 | 0.0 | 0.0 | 2.9 / 96.7 / 99.0 | 1.00 | 96%, 97% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 337.98 | 0.0 | 0.0 | 0.0 | 2.8 / 96.4 / 98.3 | 1.00 | 96% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 287.18 | 0.0 | 0.0 | 0.0 | 2.7 / 93.9 / 98.0 | 1.00 | 100% | 0.500 | 1628 |
| Neura, co-located groups | 1 | 351.92 | 0.0 | 0.0 | 0.0 | 4.7 / 95.8 / 97.3 | 1.00 | 96% | 1.000 | 814 |
| Neura, co-located groups | 2 | 270.02 | 0.0 | 0.0 | 0.0 | 4.8 / 92.7 / 98.2 | 1.00 | 100% | 0.500 | 1628 |

### Different architectures (ACT and Diffusion Policy): phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 101.06 | 0.0 | 0.0 | 67.2 | 32.7 / 95.1 / 97.9 | 1.00 | 8%, 98% | 1.000 | 901 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 143.07 | 0.0 | 0.0 | 100.3 | 15.9 / 95.7 / 98.3 | 1.00 | 98% | 0.500 | 1715 |
| Dedicated: one process per robot, one device | 4 | 377.28 | 7.5 | 0.0 | 107.4 | 2.2 / 94.9 / 98.9 | 1.00 | 100% | 0.250 | 3430 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 68.6 | 3.7 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, co-located groups | 2 | 149.19 | 0.0 | 0.0 | 101.1 | 20.2 / 95.7 / 97.7 | 1.00 | 98% | 0.500 | 1715 |
| Neura, co-located groups | 4 | 221.79 | 0.0 | 0.0 | 100.8 | 4.2 / 92.2 / 97.8 | 1.51 | 98% | 0.250 | 1715 |

## Real-time validation

The production gateway with real worker processes on the device and simulated robots over gRPC, next to the simulation of the same configuration (same robot count, warmup and window). Each cell is real / simulated. With a few hundred requests a p99 is close to the maximum: one collision moves it, so read it with the median and the miss rate. Late ticks: robot control ticks the harness itself ran late (high values mean the harness, not Neura, was overloaded).

| Case | Configuration | Robots | Requests | Misses / 1k | Starvation / robot-h | Age p50 ms | Age p99 ms | Mean batch | Device busy | Late ticks | Worker restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Same weights on every robot: phased arrivals | Dedicated: one device per robot | 1 | 45 / 45 | 0.00 / 0.00 | 0.0 / 0.0 | 2.0 / 1.9 | 3.3 / 2.9 | 1.00 / 1.00 | 8% / 8% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 1 | 45 / 45 | 0.00 / 0.00 | 0.0 / 0.0 | 4.7 / 3.7 | 7.2 / 4.7 | 1.00 / 1.00 | 8% / 8% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 4.6 / 3.6 | 6.4 / 4.9 | 1.00 / 1.00 | 34% / 32% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, FIFO | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 4.6 / 3.6 | 8.0 / 4.9 | 1.00 / 1.00 | 34% / 32% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Dedicated: one process per robot, one device | 3 | 135 / 135 | 0.00 / 0.00 | 0.0 / 0.0 | 2.0 / 2.1 | 3.9 / 2.9 | 1.00 / 1.00 | 25% / 24% | 0.0% | 0 |
| Same weights on every robot: synchronized (bursty) arrivals | Neura, deadline + fairness | 4 | 182 / 180 | 5.50 / 0.00 | 0.0 / 0.0 | 4.5 / 3.8 | 24.4 / 4.9 | 1.01 / 1.00 | 34% / 32% | 0.0% | 0 |
| Same weights on every robot: phased, with a background GPU job | Neura, deadline + fairness | 4 | 852 / 825 | 196.01 / 356.36 | 0.0 / 0.0 | 29.4 / 4.6 | 102.7 / 95.9 | 3.35 / 2.30 | 98% / 98% | 0.1% | 0 |
| Different full fine-tunes of one architecture: phased arrivals | Neura, co-located groups | 4 | 181 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 4.6 / 3.6 | 10.2 / 4.9 | 1.00 / 1.00 | 34% / 32% | 0.0% | 0 |
| Different architectures (ACT and Diffusion Policy): phased arrivals | Neura, co-located groups | 4 | 515 / 481 | 188.35 / 187.11 | 0.0 / 0.0 | 6.1 / 4.3 | 98.3 / 68.9 | 1.34 / 1.52 | 99% / 99% | 0.0% | 0 |

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
