# The ROS 2 side of the URSim test (test_ursim.py): ROS 2 Jazzy with Universal Robots' official
# ROS 2 driver (ur_robot_driver) from the ROS package repository. URSim, Universal Robots'
# simulator running the real controller software, is its own image. Build this one with
#   docker build -t neura-ur-driver-test:dev packages/neura-robots/tests/ursim
FROM ros:jazzy-ros-core@sha256:e4b2124bfa4d2021d50f094391d369349189bf090e25d904ff013ec5093ac565
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then \
      cp /run/secrets/ca_bundle /usr/local/share/ca-certificates/build.crt && update-ca-certificates; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends ros-jazzy-ur-robot-driver \
    && rm -f /usr/local/share/ca-certificates/build.crt && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*
