# The ROS 2 side of the URSim test (test_ursim.py): ROS 2 Jazzy and Universal Robots' official
# ROS 2 driver (ur_robot_driver), whose controller talks to URSim, Universal Robots' own
# simulator running the real controller software (its own image). Build this one with
#   docker build -t neura-ur-driver-test:dev packages/neura-robots/tests/ursim
# Behind a proxy, add --network host --build-arg HTTPS_PROXY (and, if it intercepts TLS,
# --secret id=ca_bundle,src=/path/to/ca.pem). The test mounts the relay node into the container.
FROM ros:jazzy-ros-core@sha256:e4b2124bfa4d2021d50f094391d369349189bf090e25d904ff013ec5093ac565
# Packages are verified against the ROS and Ubuntu signing keys in the base image, so they come
# over HTTPS from a mirror: packages.ros.org itself serves plain HTTP only.
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then \
      cp /run/secrets/ca_bundle /usr/local/share/ca-certificates/build.crt && update-ca-certificates; \
    fi \
    && sed -i 's#http://packages.ros.org/ros2/ubuntu#https://mirror.umd.edu/packages.ros.org/ros2/ubuntu#' \
        /etc/apt/sources.list.d/ros2.sources \
    && sed -i 's#http://#https://#g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ros-jazzy-ur-robot-driver ros-jazzy-ur-controllers \
        ros-jazzy-forward-command-controller \
    && rm -f /usr/local/share/ca-certificates/build.crt && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*
