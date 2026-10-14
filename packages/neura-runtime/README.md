# neura-runtime

The part of Neura that runs on the robot computer (design §5–§8). It is the only execution
authority: it owns the action cursor, the task epoch, what was dispatched, and fault responses.

- `RobotRuntime`: sans-IO core. Driven by `tick(now_us)` once per control period and by incoming
  messages. Never blocks, never reads a clock itself, never compares timestamps from other machines.
- `SessionClient`: sans-IO session handshake, lease heartbeats and RTT measurement around the runtime.
- `grpc_client.GatewayConnection`: the asyncio/gRPC adapter that moves `SessionClient` messages.

Integrations implement `Controller` (commands to the existing robot controller) and `SensorSource`
(observation capture).
