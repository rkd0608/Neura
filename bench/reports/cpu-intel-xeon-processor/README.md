# Neura GPU-sharing benchmark: CPU: Intel(R) Xeon(R) Processor @ 2.80GHz

Generated 2026-10-15T04:31:46+00:00 from commit `105d97d` with `neura-bench run --preset cpu` (torch 2.11.0, LeRobot 0.6.1). Regenerate the Markdown from `results.json` with `neura-bench report`.

> **This run is on a CPU development host, not a GPU.** It shows the harness working end to end and how sharing behaves on this host. Its numbers say nothing about GPU capacity: run `make bench` on the GPU runner for that.

Models: synthetic checkpoints with the real LeRobot architectures and random weights (ACT and Diffusion Policy, size `small`, cameras 128 x 96, fp32). Latency, memory and batching depend on architecture and shapes, not on what weights learned; closed-loop task success needs trained weights and is not measured here.

## Robots per device at the SLO

SLO: at most 1.0 deadline misses per 1,000 requests, at most 0.0 starvation events per robot-hour, and no robot faults. A configuration's capacity is the largest robot count at which it and every smaller count measured met the SLO (simulation with measured latency and contention, below).

| Case | Workload | One device per robot | One process per robot | Best Neura |
| --- | --- | --- | --- | --- |
| Same weights on every robot | phased arrivals | 1 | 12, fails at 16 | 12, fails at 16 (Neura, deadline + fairness) |
| Same weights on every robot | synchronized (bursty) arrivals | 1 | 12, fails at 16 | 2, fails at 4 (Neura, deadline + fairness) |
| Same weights on every robot | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, deadline + fairness) |
| Per-robot LoRA adapters of one base model | phased arrivals | 1 | 12, fails at 16 | 8, fails at 12 (Neura, multi-adapter batching) |
| Different full fine-tunes of one architecture | phased arrivals | 1 | 12, fails at 16 | 8, fails at 12 (Neura, co-located groups) |
| Different full fine-tunes of one architecture | phased, with a background GPU job | 0 (fails at 1) | 0 (fails at 1) | 0 (fails at 1) (Neura, co-located groups) |
| Different architectures (ACT and Diffusion Policy) | phased arrivals | 0 (fails at 1) | 1, fails at 2 | 1, fails at 2 (Neura, co-located groups) |

- **Same weights on every robot:** sharing pays. Neura, deadline + fairness serves 12 robots per device (0.083 device-hours per robot-hour, against 1 with a device per robot and 12 robots with a process per robot); at 16 robots it misses the SLO, so the crossover where one device stops paying is between 12 and 16.
- **Per-robot LoRA adapters of one base model:** sharing pays. Neura, multi-adapter batching serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot and 12 robots with a process per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Different full fine-tunes of one architecture:** sharing pays. Neura, co-located groups serves 8 robots per device (0.125 device-hours per robot-hour, against 1 with a device per robot and 12 robots with a process per robot); at 12 robots it misses the SLO, so the crossover where one device stops paying is between 8 and 12.
- **Different architectures (ACT and Diffusion Policy):** sharing does not pay on this device: Neura, co-located groups serves 1 robot(s) at the SLO.

## Measurements on the device

Forward-pass latency through a worker process, ACT:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 77.10 | 99.17 | 814 | 103 |
| 2 | 109.70 | 144.17 | 831 | 71 |
| 4 | 125.53 | 170.30 | 839 | 65 |
| 8 | 178.24 | 221.69 | 849 | 45 |

Forward-pass latency through a worker process, DIFFUSION:

| Batch | p50 (ms) | p99 (ms) | Peak memory (MiB) | Samples |
| --- | --- | --- | --- | --- |
| 1 | 232.75 | 290.22 | 901 | 34 |
| 2 | 404.70 | 443.36 | 901 | 20 |
| 4 | 444.29 | 501.98 | 901 | 20 |
| 8 | 498.42 | 574.13 | 901 | 20 |

Co-location: k workers on the device, each running batches of one back to back. Alone: p50 72.881 ms. A background job (matrix multiplications) slows a lone worker by 3.919x.

| Workers | Slowdown | p50 (ms) | p99 (ms) |
| --- | --- | --- | --- |
| 1 | 1.00 | 72.88 | 94.42 |
| 2 | 1.57 | 114.32 | 137.02 |
| 3 | 1.67 | 121.55 | 158.77 |

Simulated link, calibrated from one robot alone in real time: observation age at dispatch p50 2.028 ms, p99 2.924 ms (robot encoding, gRPC and the gateway's checks), simulated as 1.114 ms plus up to 1.829 ms.

Multi-adapter batching: 8 rank-16 LoRA adapters on 9 linear layers of the ACT base model; every request in a batch uses a different adapter. Parity with each adapter merged and run alone: max relative error 3.35e-07 (tolerance 1e-04), passed.

| Batch | Base p50 (ms) | Mixed adapters p50 (ms) | Overhead |
| --- | --- | --- | --- |
| 1 | 91.28 | 98.13 | 1.07 |
| 2 | 140.03 | 147.80 | 1.06 |
| 4 | 161.15 | 172.52 | 1.07 |
| 8 | 208.93 | 218.44 | 1.05 |

Admission control's estimate for ACT on this device: 0 robots (limited by dispatch_freshness), against 2 measured with synchronized arrivals. Admission assumes the worst case (every robot asks at once), so it should not exceed the synchronized capacity.

## Results

Per 1,000 requests: deadline misses (served after the predicted starvation time, too old to dispatch, or failed). Per robot-hour: starvation events and faults. Age: from the robot's capture to the start of the forward pass.

### Same weights on every robot: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 65.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 65.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 65.6 | 2.0 / 2.9 / 2.9 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 66.9 | 2.1 / 2.9 / 2.9 | 1.00 | 31% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 66.1 | 2.0 / 2.9 / 2.9 | 1.00 | 62% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 71.1 | 2.0 / 2.9 / 2.9 | 1.00 | 91% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 22.52 | 61.9 | 39.4 | 98.1 | 2.1 / 27.1 / 97.9 | 1.00 | 93% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 279.68 | 51.2 | 46.2 | 38.6 | 25.9 / 96.3 / 99.0 | 1.00 | 100% | 0.042 | 19536 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 4.1 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 4.0 / 4.9 / 4.9 | 1.00 | 15% | 0.500 | 814 |
| Neura, FIFO | 4 | 0.00 | 0.0 | 0.0 | 67.5 | 4.1 / 4.9 / 4.9 | 1.00 | 31% | 0.250 | 814 |
| Neura, FIFO | 8 | 0.00 | 0.0 | 0.0 | 67.2 | 4.0 / 4.9 / 4.9 | 1.00 | 62% | 0.125 | 814 |
| Neura, FIFO | 12 | 0.00 | 0.0 | 0.0 | 69.4 | 4.4 / 26.3 / 53.5 | 1.00 | 93% | 0.083 | 814 |
| Neura, FIFO | 16 | 421.87 | 710.6 | 459.4 | 19.6 | 61.1 / 97.5 / 99.2 | 3.98 | 99% | 0.062 | 849 |
| Neura, FIFO | 24 | 521.93 | 730.0 | 510.0 | 20.6 | 38.2 / 97.2 / 98.9 | 5.08 | 95% | 0.042 | 849 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 4.1 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 4.0 / 4.9 / 4.9 | 1.00 | 15% | 0.500 | 814 |
| Neura, round robin | 4 | 0.00 | 0.0 | 0.0 | 67.5 | 4.1 / 4.9 / 4.9 | 1.00 | 31% | 0.250 | 814 |
| Neura, round robin | 8 | 0.00 | 0.0 | 0.0 | 67.2 | 4.0 / 4.9 / 4.9 | 1.00 | 62% | 0.125 | 814 |
| Neura, round robin | 12 | 0.00 | 0.0 | 0.0 | 69.4 | 4.4 / 26.3 / 53.5 | 1.00 | 93% | 0.083 | 814 |
| Neura, round robin | 16 | 424.73 | 780.0 | 412.5 | 25.1 | 59.2 / 97.6 / 99.4 | 3.77 | 98% | 0.062 | 849 |
| Neura, round robin | 24 | 532.69 | 632.5 | 408.8 | 20.3 | 30.3 / 97.6 / 99.1 | 5.17 | 96% | 0.042 | 849 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 4.1 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 4.0 / 4.9 / 4.9 | 1.00 | 15% | 0.500 | 814 |
| Neura, deadline + fairness | 4 | 0.00 | 0.0 | 0.0 | 67.5 | 4.1 / 4.9 / 4.9 | 1.00 | 31% | 0.250 | 814 |
| Neura, deadline + fairness | 8 | 0.00 | 0.0 | 0.0 | 67.2 | 4.0 / 4.9 / 4.9 | 1.00 | 62% | 0.125 | 814 |
| Neura, deadline + fairness | 12 | 0.00 | 0.0 | 0.0 | 69.4 | 4.4 / 26.3 / 53.5 | 1.00 | 93% | 0.083 | 814 |
| Neura, deadline + fairness | 16 | 397.31 | 785.6 | 442.5 | 18.9 | 56.5 / 97.4 / 99.5 | 4.05 | 98% | 0.062 | 849 |
| Neura, deadline + fairness | 24 | 513.06 | 633.8 | 457.5 | 18.1 | 33.8 / 97.0 / 98.8 | 5.33 | 96% | 0.042 | 849 |

### Same weights on every robot: synchronized (bursty) arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 65.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 65.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 82.5 | 2.1 / 2.9 / 2.9 | 1.00 | 14% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 87.7 | 2.1 / 2.9 / 2.9 | 1.00 | 27% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 81.1 | 2.1 / 2.9 / 2.9 | 1.00 | 56% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 87.8 | 2.1 / 2.9 / 2.9 | 1.00 | 81% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 10.44 | 0.0 | 0.0 | 0.0 | 6.6 / 88.8 / 98.8 | 1.00 | 100% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 283.74 | 0.0 | 0.0 | 0.0 | 29.2 / 96.7 / 98.9 | 1.00 | 100% | 0.042 | 19536 |
| Neura, FIFO | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 4.1 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, FIFO | 2 | 0.00 | 0.0 | 0.0 | 96.9 | 3.8 / 4.8 / 4.8 | 2.00 | 11% | 0.500 | 831 |
| Neura, FIFO | 4 | 3.95 | 0.0 | 0.0 | 100.4 | 3.7 / 4.7 / 4.9 | 2.22 | 21% | 0.250 | 839 |
| Neura, FIFO | 8 | 17.12 | 41.2 | 30.0 | 94.4 | 3.5 / 25.2 / 74.6 | 2.30 | 43% | 0.125 | 849 |
| Neura, round robin | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 4.1 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, round robin | 2 | 0.00 | 0.0 | 0.0 | 96.9 | 3.8 / 4.8 / 4.8 | 2.00 | 11% | 0.500 | 831 |
| Neura, round robin | 4 | 3.95 | 0.0 | 0.0 | 100.4 | 3.7 / 4.7 / 4.9 | 2.22 | 21% | 0.250 | 839 |
| Neura, round robin | 8 | 17.12 | 41.2 | 30.0 | 94.4 | 3.5 / 25.2 / 74.6 | 2.30 | 43% | 0.125 | 849 |
| Neura, deadline + fairness | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 4.1 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 0.00 | 0.0 | 0.0 | 96.9 | 3.8 / 4.8 / 4.8 | 2.00 | 11% | 0.500 | 831 |
| Neura, deadline + fairness | 4 | 3.95 | 0.0 | 0.0 | 100.4 | 3.7 / 4.7 / 4.9 | 2.22 | 21% | 0.250 | 839 |
| Neura, deadline + fairness | 8 | 18.29 | 48.8 | 30.0 | 95.7 | 3.6 / 61.8 / 77.1 | 2.52 | 42% | 0.125 | 849 |

### Same weights on every robot: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 333.33 | 0.0 | 0.0 | 0.0 | 2.9 / 96.7 / 98.1 | 1.00 | 96% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 333.33 | 0.0 | 0.0 | 0.0 | 2.9 / 96.7 / 98.1 | 1.00 | 96% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 217.76 | 0.0 | 0.0 | 0.0 | 5.5 / 96.6 / 99.4 | 1.00 | 100% | 0.500 | 1628 |
| Neura, FIFO | 1 | 340.28 | 0.0 | 0.0 | 0.0 | 4.8 / 96.9 / 99.1 | 1.00 | 96% | 1.000 | 814 |
| Neura, FIFO | 2 | 311.69 | 0.0 | 0.0 | 0.0 | 4.2 / 92.1 / 97.5 | 1.97 | 97% | 0.500 | 831 |
| Neura, round robin | 1 | 340.28 | 0.0 | 0.0 | 0.0 | 4.8 / 96.9 / 99.1 | 1.00 | 96% | 1.000 | 814 |
| Neura, round robin | 2 | 311.69 | 0.0 | 0.0 | 0.0 | 4.2 / 92.1 / 97.5 | 1.97 | 97% | 0.500 | 831 |
| Neura, deadline + fairness | 1 | 340.28 | 0.0 | 0.0 | 0.0 | 4.8 / 96.9 / 99.1 | 1.00 | 96% | 1.000 | 814 |
| Neura, deadline + fairness | 2 | 311.69 | 0.0 | 0.0 | 0.0 | 4.2 / 92.1 / 97.5 | 1.97 | 97% | 0.500 | 831 |

### Per-robot LoRA adapters of one base model: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 1 | 0.00 | 0.0 | 0.0 | 65.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 65.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 65.6 | 2.0 / 2.9 / 2.9 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 66.9 | 2.1 / 2.9 / 2.9 | 1.00 | 31% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 66.1 | 2.0 / 2.9 / 2.9 | 1.00 | 62% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 71.1 | 2.0 / 2.9 / 2.9 | 1.00 | 91% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 22.52 | 61.9 | 39.4 | 98.1 | 2.1 / 27.1 / 97.9 | 1.00 | 93% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 279.68 | 51.2 | 46.2 | 38.6 | 25.9 / 96.3 / 99.0 | 1.00 | 100% | 0.042 | 19536 |
| Neura, multi-adapter batching | 1 | 0.00 | 0.0 | 0.0 | 70.3 | 4.1 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, multi-adapter batching | 2 | 0.00 | 0.0 | 0.0 | 69.3 | 4.0 / 4.9 / 4.9 | 1.00 | 16% | 0.500 | 814 |
| Neura, multi-adapter batching | 4 | 0.00 | 0.0 | 0.0 | 71.0 | 4.1 / 4.9 / 4.9 | 1.00 | 34% | 0.250 | 814 |
| Neura, multi-adapter batching | 8 | 0.00 | 0.0 | 0.0 | 70.6 | 4.0 / 4.9 / 4.9 | 1.00 | 67% | 0.125 | 814 |
| Neura, multi-adapter batching | 12 | 314.68 | 632.5 | 257.5 | 59.2 | 4.9 / 96.8 / 98.2 | 3.13 | 78% | 0.083 | 849 |
| Neura, multi-adapter batching | 16 | 434.07 | 742.5 | 461.2 | 17.6 | 56.2 / 97.4 / 99.6 | 4.06 | 98% | 0.062 | 849 |

### Different full fine-tunes of one architecture: phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 0.00 | 0.0 | 0.0 | 65.6 | 2.0 / 2.9 / 2.9 | 1.00 | 8%, 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 65.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 0.00 | 0.0 | 0.0 | 65.6 | 2.0 / 2.9 / 2.9 | 1.00 | 15% | 0.500 | 1628 |
| Dedicated: one process per robot, one device | 4 | 0.00 | 0.0 | 0.0 | 66.9 | 2.1 / 2.9 / 2.9 | 1.00 | 31% | 0.250 | 3256 |
| Dedicated: one process per robot, one device | 8 | 0.00 | 0.0 | 0.0 | 66.1 | 2.0 / 2.9 / 2.9 | 1.00 | 62% | 0.125 | 6512 |
| Dedicated: one process per robot, one device | 12 | 0.00 | 0.0 | 0.0 | 71.1 | 2.0 / 2.9 / 2.9 | 1.00 | 91% | 0.083 | 9768 |
| Dedicated: one process per robot, one device | 16 | 22.52 | 61.9 | 39.4 | 98.1 | 2.1 / 27.1 / 97.9 | 1.00 | 93% | 0.062 | 13024 |
| Dedicated: one process per robot, one device | 24 | 279.68 | 51.2 | 46.2 | 38.6 | 25.9 / 96.3 / 99.0 | 1.00 | 100% | 0.042 | 19536 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 4.1 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, co-located groups | 2 | 0.00 | 0.0 | 0.0 | 66.7 | 4.0 / 4.9 / 4.9 | 1.00 | 15% | 0.500 | 1628 |
| Neura, co-located groups | 4 | 0.00 | 0.0 | 0.0 | 67.5 | 4.1 / 4.9 / 4.9 | 1.00 | 31% | 0.250 | 1628 |
| Neura, co-located groups | 8 | 0.00 | 0.0 | 0.0 | 67.2 | 4.0 / 4.9 / 4.9 | 1.00 | 62% | 0.125 | 1628 |
| Neura, co-located groups | 12 | 85.26 | 87.5 | 40.0 | 68.7 | 4.5 / 92.5 / 98.4 | 1.08 | 92% | 0.083 | 1680 |
| Neura, co-located groups | 16 | 532.02 | 643.1 | 451.9 | 22.8 | 47.7 / 97.7 / 99.0 | 2.05 | 100% | 0.062 | 1692 |

### Different full fine-tunes of one architecture: phased, with a background GPU job

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 331.59 | 0.0 | 0.0 | 0.0 | 6.4 / 96.1 / 98.7 | 1.00 | 96%, 96% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 1 | 333.33 | 0.0 | 0.0 | 0.0 | 2.9 / 96.7 / 98.1 | 1.00 | 96% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 217.76 | 0.0 | 0.0 | 0.0 | 5.5 / 96.6 / 99.4 | 1.00 | 100% | 0.500 | 1628 |
| Neura, co-located groups | 1 | 340.28 | 0.0 | 0.0 | 0.0 | 4.8 / 96.9 / 99.1 | 1.00 | 96% | 1.000 | 814 |
| Neura, co-located groups | 2 | 206.05 | 0.0 | 0.0 | 0.0 | 11.8 / 96.7 / 98.0 | 1.00 | 100% | 0.500 | 1628 |

### Different architectures (ACT and Diffusion Policy): phased arrivals

| Configuration | Robots | Misses / 1k | Starvation / robot-h | Faults / robot-h | Late-trimmed / 1k actions | Age p50 / p99 / max (ms) | Mean batch | Device busy | GPU-h / robot-h | Peak MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dedicated: one device per robot | 2 | 26.44 | 0.0 | 0.0 | 66.9 | 10.4 / 93.2 / 97.8 | 1.00 | 8%, 98% | 1.000 | 901 |
| Dedicated: one process per robot, one device | 1 | 0.00 | 0.0 | 0.0 | 65.8 | 2.1 / 2.9 / 2.9 | 1.00 | 8% | 1.000 | 814 |
| Dedicated: one process per robot, one device | 2 | 89.53 | 0.0 | 0.0 | 108.1 | 18.1 / 94.6 / 97.8 | 1.00 | 98% | 0.500 | 1715 |
| Dedicated: one process per robot, one device | 4 | 360.45 | 0.0 | 0.0 | 112.6 | 2.2 / 95.1 / 97.8 | 1.00 | 100% | 0.250 | 3430 |
| Neura, co-located groups | 1 | 0.00 | 0.0 | 0.0 | 67.8 | 4.1 / 4.9 / 4.9 | 1.00 | 8% | 1.000 | 814 |
| Neura, co-located groups | 2 | 90.50 | 0.0 | 0.0 | 108.3 | 15.8 / 96.7 / 97.6 | 1.00 | 98% | 0.500 | 1715 |
| Neura, co-located groups | 4 | 311.97 | 0.0 | 0.0 | 106.4 | 4.0 / 67.6 / 93.1 | 1.50 | 98% | 0.250 | 1715 |

## Real-time validation

The production gateway with real worker processes on the device and simulated robots over gRPC, next to the simulation of the same configuration (same robot count, warmup and window). Each cell is real / simulated. With a few hundred requests a p99 is close to the maximum: one collision moves it, so read it with the median and the miss rate. Late ticks: robot control ticks the harness itself ran late (high values mean the harness, not Neura, was overloaded).

| Case | Configuration | Robots | Requests | Misses / 1k | Starvation / robot-h | Age p50 ms | Age p99 ms | Mean batch | Device busy | Late ticks | Worker restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Same weights on every robot: phased arrivals | Dedicated: one device per robot | 1 | 45 / 45 | 0.00 / 0.00 | 0.0 / 0.0 | 2.0 / 2.2 | 2.5 / 2.9 | 1.00 / 1.00 | 8% / 8% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 1 | 45 / 45 | 0.00 / 0.00 | 0.0 / 0.0 | 4.5 / 4.2 | 5.9 / 4.9 | 1.00 / 1.00 | 8% / 8% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, deadline + fairness | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 4.5 / 4.1 | 6.8 / 4.9 | 1.00 / 1.00 | 33% / 31% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Neura, FIFO | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 4.8 / 4.1 | 7.0 / 4.9 | 1.00 / 1.00 | 35% / 31% | 0.0% | 0 |
| Same weights on every robot: phased arrivals | Dedicated: one process per robot, one device | 3 | 135 / 135 | 0.00 / 0.00 | 0.0 / 0.0 | 2.2 / 2.0 | 4.6 / 2.9 | 1.00 / 1.00 | 25% / 23% | 0.0% | 0 |
| Same weights on every robot: synchronized (bursty) arrivals | Neura, deadline + fairness | 4 | 248 / 206 | 44.35 / 9.71 | 40.0 / 0.0 | 4.9 / 3.5 | 91.8 / 4.7 | 1.96 / 2.65 | 32% / 20% | 0.1% | 0 |
| Same weights on every robot: phased, with a background GPU job | Neura, deadline + fairness | 4 | 880 / 818 | 312.50 / 348.41 | 0.0 / 0.0 | 54.0 / 4.7 | 100.6 / 96.6 | 2.27 / 2.28 | 99% / 99% | 0.0% | 0 |
| Different full fine-tunes of one architecture: phased arrivals | Neura, co-located groups | 4 | 180 / 180 | 0.00 / 0.00 | 0.0 / 0.0 | 4.6 / 4.1 | 6.5 / 4.9 | 1.00 / 1.00 | 33% / 31% | 0.0% | 0 |
| Different architectures (ACT and Diffusion Policy): phased arrivals | Neura, co-located groups | 4 | 513 / 488 | 165.69 / 288.93 | 0.0 / 0.0 | 5.2 / 4.1 | 94.5 / 90.8 | 1.48 / 1.49 | 98% / 98% | 0.0% | 0 |

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
