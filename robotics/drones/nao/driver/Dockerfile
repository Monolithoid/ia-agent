FROM ros:humble-ros-base

# Build context : robotics/ (pour inclure robotics/shared/)
#   docker build -f robotics/drones/nao/driver/Dockerfile robotics

ENV DEBIAN_FRONTEND=noninteractive \
    ROS_DOMAIN_ID=42 \
    ROBOT_ID=nao_01 \
    NAOQI_BRIDGE_URL=http://localhost:8090 \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        ros-humble-std-msgs \
        ros-humble-sensor-msgs \
        ros-humble-rmw-cyclonedds-cpp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ros2_ws
COPY drones/nao/driver/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY shared/speech_services.py drones/nao/driver/naoqi_driver_node.py drones/nao/driver/motion_policy.py ./

CMD ["/bin/bash", "-c", \
     "source /opt/ros/humble/setup.bash && exec python3 /ros2_ws/naoqi_driver_node.py"]
