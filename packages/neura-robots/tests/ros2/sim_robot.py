"""A simulated ROS 2 robot for the bridge's test: a position-controlled arm, or its cameras.

    python3 sim_robot.py --joints shoulder_pan,elbow_flex --start shoulder_pan=0.35   # the arm
    python3 sim_robot.py --cameras front,wrist                                          # cameras

The arm publishes ``/joint_states`` at 100 Hz and moves each joint toward the last
``/forward_position_controller/commands`` at a bounded speed, as ros2_control's forward position
controller with an arm behind it does; with no command it stays where it is. The cameras publish
still 640x480 ``rgb8`` frames at 30 Hz on ``/camera/<name>/image_raw``. It runs in the robot's ROS
environment: rclpy and the standard library only.
"""

from __future__ import annotations

import argparse
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--joints", default="", help="joint names, in command order")
    parser.add_argument("--start", default="", metavar="JOINT=RAD,...")
    parser.add_argument("--max-speed", type=float, default=3.0, help="radians per second")
    parser.add_argument("--cameras", default="")
    args = parser.parse_args()

    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
    from sensor_msgs.msg import Image, JointState
    from std_msgs.msg import Float64MultiArray

    rclpy.init()
    node = rclpy.create_node("sim_robot")
    joints = [j for j in args.joints.split(",") if j]
    start = {k: float(v) for k, v in (p.split("=") for p in args.start.split(",") if p)}
    position = {j: start.get(j, 0.0) for j in joints}
    target = dict(position)
    period = 0.01
    if joints:
        states = node.create_publisher(JointState, "/joint_states", qos_profile_sensor_data)

        def on_command(msg: Any) -> None:
            if len(msg.data) == len(joints):
                target.update(zip(joints, msg.data, strict=True))

        def step() -> None:
            most = args.max_speed * period
            for j in joints:
                position[j] += max(-most, min(most, target[j] - position[j]))
            msg = JointState(name=joints, position=[position[j] for j in joints])
            msg.header.stamp = node.get_clock().now().to_msg()
            states.publish(msg)

        reliable = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)
        node.create_subscription(
            Float64MultiArray, "/forward_position_controller/commands", on_command, reliable
        )
        node.create_timer(period, step)
    for name in (c for c in args.cameras.split(",") if c):
        frame = Image(width=640, height=480, encoding="rgb8", step=640 * 3)
        frame.data = bytes(640 * 480 * 3)
        topic = f"/camera/{name}/image_raw"
        publisher = node.create_publisher(Image, topic, qos_profile_sensor_data)
        node.create_timer(1 / 30, lambda p=publisher, f=frame: p.publish(f))
    print(f"simulating {len(joints)} joints and cameras {args.cameras or 'none'}", flush=True)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
