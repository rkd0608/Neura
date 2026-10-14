# neura-agent

The Neura site agent that runs on a customer GPU server (design §17).

Milestone status: M1 provides the process skeleton: the local site API (health and status) and an
outbound reachability probe of the control plane that reports `AGENT_CONTROL_PLANE_UNREACHABLE`
while the site keeps operating. Enrollment, the outbound control stream, signed manifest
reconciliation and artifact verification arrive in M4.

Run locally: `uv run neura-agent` (reads `NEURA_AGENT_*` environment variables).
