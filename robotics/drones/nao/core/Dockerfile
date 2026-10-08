FROM ros:humble-ros-base

# Build context : robotics/drones/nao/
# Réunit naoqi-driver, cognitive-renderer et motion-engine en un seul conteneur.

ENV DEBIAN_FRONTEND=noninteractive \
    ROS_DOMAIN_ID=42 \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
    PYTHONUNBUFFERED=1 \
    ROBOT_ID=nao_01 \
    NAOQI_BRIDGE_URL=http://localhost:8090

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        python3-numpy \
        ros-humble-std-msgs \
        ros-humble-sensor-msgs \
        ros-humble-rmw-cyclonedds-cpp \
    && rm -rf /var/lib/apt/lists/*

# Dépendances pip (naoqi-driver : requests + edge-tts)
COPY driver/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /ros2_ws

# Scripts des trois nœuds
COPY driver/naoqi_driver_node.py       ./naoqi_driver_node.py
COPY cognitive_renderer/cognitive_renderer_node.py ./cognitive_renderer_node.py
COPY motion_engine/motion_engine/                       ./motion_engine/

# Entrypoint
COPY core/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

CMD ["./entrypoint.sh"]
