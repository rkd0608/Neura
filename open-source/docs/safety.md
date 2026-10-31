# Safety model

neura-robot's runtime is the **execution authority** on the robot: a policy proposes actions, the
runtime decides which ones the controller receives. This page says what it guarantees, how that is
tested, and where its responsibility ends.

## What it guarantees

| Rule | How |
|---|---|
| Nothing moves until an operator arms it | Only `Armed`, `Running` and `Waiting` dispatch actions; `arm` is an operator command, never automatic |
| Takeover stops the policy at once | `takeover` empties the queue, starts a new task epoch and holds; chunks for the old epoch are refused |
| No stale action executes | A chunk whose observation is older than `max_observation_age_at_acceptance_us` on arrival is refused; actions whose time has passed are trimmed, never executed late |
| No action from a finished task executes | Every chunk names its task epoch; anything not for the current one is refused |
| Answers match their requests | A chunk must answer the request outstanding (sequence, anchor, contract digests, payload digest); on wires without identifiers (openpi, LeRobot) the transport drops the connection or checks timesteps so a late answer can't pass as a new one |
| Bad values never reach the controller | Non-finite values fail decoding; actions outside the robot profile's command ranges fault the robot |
| Losing the policy is handled | When fresh actions run out the robot holds (`Waiting`); past the starvation grace period it faults and applies its fault response |
| Faults latch | A fault stays until an operator's `reset`, which first asks the robot to revalidate (e.g. torque back on, joint states fresh) |
| Losing the robot is handled | A command the controller can't take faults the robot (`CONTROLLER_COMMAND_FAILED`, e.g. a pulled servo bus); authority is checked every period, so stale ROS 2 joint states fault it too (`ACTUATOR_LOCK_LOST`) |

## How it is tested

Every end-to-end test runs an **independent invariant monitor** (`neura_runtime.invariants`). It
watches what is sent, what arrives and what the controller is told, keeps its own model of what
was allowed, and counts a violation whenever the runtime executed something stale, duplicated, out
of order, out of range or unauthorized, or cleared a latched fault without a reset. Every test
requires zero violations. The scenarios include:

- each transport (in-process, `neura-robot serve`, openpi's server, LeRobot's server) driving a
  robot, then failing;
- an emulated SO-101 servo bus with the bus pulled, and a joint whose overload protection trips;
- a ROS 2 Jazzy robot with 20% of packets dropped each way, then its link cut;
- incident records replayed through a fresh runtime, which must decide identically.

## Where its responsibility ends

neura-robot is **not a safety-rated controller** and is not certified to any functional-safety
standard. It runs on a general-purpose computer, in Python. It reduces the ways a learned policy can
make a robot do something unintended; it does not replace:

- the robot's emergency stop and safety-rated stop functions;
- torque, speed and force limits enforced by the robot's own controller;
- a risk assessment of the cell (ISO 10218, ISO/TS 15066 for collaborative operation);
- guarding, and a human able to stop the robot.

The robot profile's command ranges are a software guard, not a safety function: set them from the
robot's real limits, and keep the controller's own limits in place.
