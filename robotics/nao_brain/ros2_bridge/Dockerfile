FROM ros:humble
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ros-humble-rosbridge-server \
    ros-humble-rmw-cyclonedds-cpp \
 && rm -rf /var/lib/apt/lists/*
ENV RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
EXPOSE 9090
CMD ["ros2", "launch", "rosbridge_server", "rosbridge_websocket_launch.xml", "port:=9090"]
