# neura-adapters

The interface between Neura's policy workers and models (design §12), plus the adapters Neura
ships:

- `mock`: deterministic, for tests and the simulator.
- `lerobot`: LeRobot policies (ACT, SmolVLA, π0, diffusion) loaded from a checkpoint directory,
  with the checkpoint's own pre- and post-processors. Install with the `lerobot` extra.
- `lora`: multi-adapter (LoRA) batching. One base forward pass with per-request adapters,
  checked against unbatched execution before it is used (§9.5).

An adapter receives inputs the gateway has already validated against the contract. It returns one
action chunk per input, in the model's declared action order and units.
