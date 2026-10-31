# ROS 2 robots

A robot whose controllers run under ROS 2 is served the way an SO-101 is: `neura-arm` runs the
robot runtime against your policy, and nothing moves until the operator arms it. Between ROS and
neura-robot sits a small **relay node** in the robot's own ROS environment.

```
ROS 2 graph            robot computer
joint states, images ─► relay ─socket─► neura-arm ◄──► policy (any transport)
position controller  ◄─ (rclpy) ◄────── (runtime)
```

The relay needs only rclpy and the Python standard library, so it runs on the Python your ROS
distribution ships (Humble's 3.10, Jazzy's 3.12). `neura-arm` connects to it over a local socket.
Commands go to a position controller as `std_msgs/Float64MultiArray`, the message ros2_control's
`forward_command_controller` takes.

## Connect a robot

On the robot computer, in a shell with your ROS workspace sourced, start the relay. Name the
joints in the order your controller takes them, and the camera topics by the robot profile's
camera roles:

```bash
python3 relay.py --socket /run/neura/ros2.sock \
    --joint-states /joint_states \
    --command-topic /forward_position_controller/commands \
    --joints shoulder_pan,shoulder_lift,elbow_flex,wrist_flex,wrist_roll,gripper \
    --camera front=/camera/front/image_raw --camera wrist=/camera/wrist/image_raw
```

`relay.py` is `packages/neura-robots/src/neura_robots/ros2/relay.py` in the repository: copy it
to the robot computer. Then give `neura-arm` an `arm` of type `ros2`, which maps each ROS joint to
the robot profile's name and unit (`profile = ros × scale + offset`):

```yaml
arm:
  type: ros2
  socket: /run/neura/ros2.sock
  joints:
    # radians to degrees
    - {ros: shoulder_pan, name: shoulder_pan.pos, scale: 57.29577951308232}
    # ... one entry per command in the robot profile, in the relay's --joints order
    - {ros: gripper, name: gripper.pos, scale: 100.0}
  state_timeout_ms: 100

cameras:
  front: {type: ros2}
  wrist: {type: ros2}
```

The full example is `examples/robots/ros2-arm.yaml`. Check it without moving anything, then serve
it:

```bash
neura-arm check examples/robots/ros2-arm.yaml --robot-profile examples/contracts/so101.robot.yaml
neura-arm run examples/robots/ros2-arm.yaml --policy examples/policies/mid_range.py
```

`check` waits for the first joint states and a frame from every camera, and prints where every
joint is. Images may arrive as `rgb8` or `bgr8`; neura-robot delivers them in the color order the robot
profile declares.

## What keeps it safe

- **The runtime decides.** Every action is checked by the runtime (its range, its task epoch, its
  freshness) before the relay is sent it. ROS never receives a command the runtime refused.
- **Stale states stop the robot.** The robot can be driven only while its joint states are younger
  than `state_timeout_ms`. If they stop arriving (a node died, the network dropped), the runtime
  faults, sends nothing more, and the position controller keeps its last command.
- **A fault waits for a person.** When the states come back the fault stays latched: the operator
  types `reset`, then `arm`.
- **Only the latest command.** Commands are published reliably with a queue of one: a lost command
  is resent, one that waited behind a newer one is dropped.

These are tested with ROS 2 Jazzy: a simulated arm in one container, its cameras and the relay in
another, and `neura-arm` on the host. With a fifth of the packets dropped each way on the arm's
link, the arm is still driven and the joint states still arrive about 80 times a second. With the
link cut, the runtime faults within the timeout and sends nothing more. The invariant monitor sees
no violation.

## Not built yet

- Action interfaces (`control_msgs/FollowJointTrajectory`): position command topics only.
- Velocity or effort controllers.
- DDS security (SROS 2): run the relay on the robot computer, where the socket stays local.
