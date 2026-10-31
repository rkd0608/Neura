# The ROS 2 side of the bridge's test (test_ros2.py): ROS 2 Jazzy, plus nftables to drop packets
# on the link between the simulated robot and the relay node. Build it with
#   docker build -t neura-ros2-test:dev packages/neura-robots/tests/ros2
# The test mounts the relay and the simulated robot (sim_robot.py) into its containers.
FROM ros:jazzy-ros-core@sha256:e4b2124bfa4d2021d50f094391d369349189bf090e25d904ff013ec5093ac565
RUN apt-get update && apt-get install -y --no-install-recommends nftables \
    && rm -rf /var/lib/apt/lists/*
