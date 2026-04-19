FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive \
    ROS_DOMAIN_ID=42 \
    OLLAMA_URL=http://localhost:11434 \
    CHROMA_HOST=localhost \
    CHROMA_PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        ros-humble-std-msgs \
        ros-humble-sensor-msgs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ros2_ws
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY nao_brain_agent.py .

CMD ["/bin/bash", "-c", \
     "source /opt/ros/humble/setup.bash && python3 /ros2_ws/nao_brain_agent.py"]
