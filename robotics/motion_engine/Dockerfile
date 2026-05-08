FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive \
    ROS_DOMAIN_ID=42 \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-numpy \
        ros-humble-std-msgs \
        ros-humble-rmw-cyclonedds-cpp \
    && rm -rf /var/lib/apt/lists/*

# Build context = robotics/motion_engine/ (package root)
# Copying here places the motion_engine/ Python package directly under /ros2_ws/
WORKDIR /ros2_ws
COPY motion_engine/ ./motion_engine/

CMD ["/bin/bash", "-c", \
     "source /opt/ros/humble/setup.bash && python3 -u -m motion_engine.node"]
