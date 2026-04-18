FROM ros:jazzy-ros-base

RUN apt-get update && apt-get install -y \
    ros-jazzy-rosbridge-suite \
    ros-jazzy-geometry-msgs \
    ros-jazzy-std-msgs \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 9090

CMD ["/bin/bash", "-c", \
    "source /opt/ros/jazzy/setup.bash && \
     ros2 launch rosbridge_server rosbridge_websocket_launch.xml port:=9090 address:=0.0.0.0"]
