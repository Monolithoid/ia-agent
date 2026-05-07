FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive \
    ROS_DOMAIN_ID=42 \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        ros-humble-std-msgs \
        ros-humble-rmw-cyclonedds-cpp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ros2_ws
COPY cognitive_renderer_node.py .

CMD ["/bin/bash", "-c", \
     "source /opt/ros/humble/setup.bash && python3 /ros2_ws/cognitive_renderer_node.py"]
