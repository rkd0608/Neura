# neura-server

The site data plane that runs on the customer's GPU server (design §3, §6, §9).

- `GatewayCore`: sans-IO gateway. Authenticated robot identity in, validated requests to the
  scheduler, action chunks and typed rejections out. Owns leases and admission.
- `LeaseTable`: one live lease per robot, monotonic generations persisted across restarts.
- `Scheduler`: per batch group; FIFO, round-robin and deadline-with-fairness policies; one
  outstanding request per session; expiry; batching bounded by profiled latency.
- `AdmissionController`: refuses sessions whose timing the admitted load could not meet.
- `PolicyBackend` / `MockPolicy`: the model-execution interface and a deterministic test policy.
- `grpc_server`: the asyncio gRPC adapter, plus a local HTTP status endpoint.
