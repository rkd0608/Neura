# neura-robot documentation

## Concepts in one page

**Contracts.** Three small YAML documents say what may run on a robot, and are checked against
each other before anything connects:

- a **model artifact**: what the policy reads (state vectors, camera images, an instruction) and
  what it returns (chunks of actions, their units and order);
- a **robot profile**: what the robot accepts: each command's name, unit and range, its cameras,
  its control period and the fault responses it supports;
- an **execution profile**: how the two run together: the actions per chunk, how a new chunk
  merges into the queue, how old an observation may be when its chunk arrives, how long the robot
  may go without fresh actions.

Examples are in [`examples/contracts`](../examples/contracts). A mismatch (a unit, a range, an
action the robot doesn't take) is refused with a reason before the robot is touched.

**The runtime** (`neura_runtime.runtime.RobotRuntime`) runs on the robot computer, once per control
period. It asks for an action chunk when its queue runs low, checks every chunk that arrives, merges
the good ones and hands one action per period to the controller. It never does I/O itself: the
same code runs on a real arm, in a simulator and in a replay.

**States.** `Disconnected` → `Ready` (connected, waiting for an operator) → `Armed` → `Running` ⇄
`Waiting` (out of fresh actions, holding) → back to `Ready` on `stop` or `takeover`. Any fault goes
to `Faulted`, which only an operator's `reset` (after the robot revalidates) leaves.

**A request becomes an action** like this: the runtime sends an observation stamped with the
robot's clock, its task epoch and a sequence number; the policy answers with a chunk naming the same
epoch and sequence; the runtime accepts it only if the observation is still fresh enough, the epoch
is still current and the chunk fits where it was asked to go; actions already in the past are
trimmed, never executed late.

**Transports** connect the runtime to a policy: in this process, `neura-robot serve` on another
machine, openpi's server or LeRobot's server. The runtime cannot tell them apart.

## Pages

| | |
|---|---|
| [Configuration](configuration.md) | `neura-arm` configuration files, field by field |
| [Policies](policies.md) | `@neura.policy`, `neura-robot serve`, openpi and LeRobot |
| [Safety model](safety.md) | What the runtime guarantees, and what it doesn't |
| [Incident records](incidents.md) | Recording, the MCAP layout, replay |
| [ROS 2](ros2.md) | The relay node and ROS 2 robots |
| [Development](development.md) | The repository, the tests, and how to add a robot or a transport |
