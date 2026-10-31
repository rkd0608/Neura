"""The ROS 2 side of the bridge: a node that relays a robot's joint states and camera images to
Neura's robot-side process, and its position commands back, over a local Unix socket.

    python3 relay.py --socket /run/neura/ros2.sock --joint-states /joint_states \\
        --command-topic /forward_position_controller/commands \\
        --joints shoulder_pan,shoulder_lift,elbow,wrist_flex,wrist_roll,gripper \\
        --camera front=/camera/front/image_raw --camera wrist=/camera/wrist/image_raw

It runs on the robot computer's own ROS Python (Humble's 3.10, Jazzy's 3.12): it needs only the
standard library and rclpy, and imports nothing from Neura, whose robot-side process
(``neura-arm``, Python 3.13) connects to the socket. Commands are published as
``std_msgs/Float64MultiArray`` in ``--joints`` order, as ``ros2_control``'s forward position
controller takes them; a controller holds its last command, so the robot holds when Neura stops.
Every reading is stamped on arrival with this machine's monotonic clock, the clock the runtime
judges freshness by.

The wire format, both ways: a 4-byte big-endian length, a JSON header of that length, then
``header["size"]`` bytes of payload. Neura's side imports ``send_frame`` and ``read_frame`` from
here, so the format has one source.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import struct
import threading
import time
from typing import IO, Any

_LENGTH = struct.Struct(">I")
MAX_HEADER = 64 * 1024


def send_frame(sock: socket.socket, header: dict[str, Any], payload: bytes = b"") -> None:
    data = json.dumps({**header, "size": len(payload)}, separators=(",", ":")).encode()
    sock.sendall(_LENGTH.pack(len(data)) + data + payload)


def read_frame(stream: IO[bytes]) -> tuple[dict[str, Any], bytes] | None:
    """The next frame, or None when the other side has gone."""
    prefix = stream.read(_LENGTH.size)
    if len(prefix) < _LENGTH.size:
        return None
    (length,) = _LENGTH.unpack(prefix)
    if length > MAX_HEADER:
        raise ValueError(f"a {length}-byte header: not a relay frame")
    header: dict[str, Any] = json.loads(stream.read(length))
    payload = stream.read(int(header.get("size", 0)))
    return header, payload


def monotonic_us() -> int:
    return time.monotonic_ns() // 1000


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - runs inside ROS 2
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--socket", required=True, help="the Unix socket neura-arm connects to")
    parser.add_argument("--joint-states", default="/joint_states")
    parser.add_argument("--command-topic", default="/forward_position_controller/commands")
    parser.add_argument("--joints", required=True, help="ROS joint names, in command order")
    parser.add_argument("--camera", action="append", default=[], metavar="NAME=TOPIC")
    args = parser.parse_args(argv)

    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
    from sensor_msgs.msg import Image, JointState
    from std_msgs.msg import Float64MultiArray

    joints = [j for j in args.joints.split(",") if j]
    cameras = dict(c.split("=", 1) for c in args.camera)
    lock = threading.Lock()
    peer: list[socket.socket] = []  # Neura's connection, while there is one

    def send(header: dict[str, Any], payload: bytes = b"") -> None:
        with lock:
            if not peer:
                return
            try:
                send_frame(peer[0], header, payload)
            except OSError:
                peer.clear()  # Neura went away; the next one connects again

    def on_state(msg: Any) -> None:
        position = dict(zip(msg.name, msg.position, strict=False))
        if all(j in position for j in joints):
            send(
                {
                    "kind": "state",
                    "t_us": monotonic_us(),
                    "position": {j: position[j] for j in joints},
                }
            )

    def on_image(name: str, msg: Any) -> None:
        header = {"kind": "image", "camera": name, "t_us": monotonic_us()}
        header.update(width=msg.width, height=msg.height, encoding=msg.encoding)
        send(header, bytes(msg.data))

    rclpy.init()
    node = rclpy.create_node("neura_relay")
    # The latest command only: a command that waited behind a newer one is stale.
    commands = node.create_publisher(
        Float64MultiArray,
        args.command_topic,
        QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE),
    )
    node.create_subscription(JointState, args.joint_states, on_state, qos_profile_sensor_data)
    for name, topic in cameras.items():
        node.create_subscription(
            Image, topic, lambda msg, n=name: on_image(n, msg), qos_profile_sensor_data
        )
    with contextlib.suppress(FileNotFoundError):
        os.unlink(args.socket)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(args.socket)
    server.listen(1)

    def serve() -> None:
        while rclpy.ok():
            connection, _ = server.accept()
            with lock:
                peer[:] = [connection]
            stream = connection.makefile("rb")
            while (frame := read_frame(stream)) is not None:
                header, _ = frame
                if header.get("kind") == "command":
                    commands.publish(Float64MultiArray(data=[float(p) for p in header["position"]]))
            with lock:
                peer.clear()
            connection.close()

    threading.Thread(target=serve, daemon=True).start()
    print(f"relaying {len(joints)} joints and {len(cameras)} cameras on {args.socket}", flush=True)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
