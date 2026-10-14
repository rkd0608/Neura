# neura-control

The NucleiLabs cloud control plane (design §13, §20). It is never on the robot action path.

Milestone status: M1 provides the service skeleton (health, readiness, metadata, the published
OpenAPI document and RFC 9457 problem responses). Resources, operations, events, audit and
authentication arrive in M3.

Run locally: `uv run neura-control` (reads `NEURA_CONTROL_*` environment variables).
