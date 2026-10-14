# neura-sim

Simulation for Neura's data plane.

- `SimulatedArm`, `SimController`, `SimSensors`: a position-controlled arm with fault injection,
  implementing the runtime's `Controller` and `SensorSource` interfaces.
- `Link`: a one-way network model with latency, jitter, delay spikes, partitions, duplication and
  reordering.
- `SimSite`: a deterministic discrete-event simulation of N robots, the gateway, the scheduler and a
  policy worker. Every node has its own clock offset, so any code that compared timestamps across
  machines would fail. Used for the fault suite (design §7) and for scheduling experiments.
- `grpc_robots`: real robot clients that drive simulated arms against a live gateway over gRPC.
