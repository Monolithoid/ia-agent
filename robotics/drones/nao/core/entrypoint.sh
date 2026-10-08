#!/usr/bin/env bash
# Démarre les trois nœuds ROS2 en parallèle.
# Si l'un s'arrête, le conteneur s'arrête → Docker le redémarre entier.
set -euo pipefail

source /opt/ros/humble/setup.bash

echo "[nao-core] démarrage des nœuds…"

python3 -u /ros2_ws/naoqi_driver_node.py      &  PID_DRIVER=$!
python3 -u /ros2_ws/cognitive_renderer_node.py &  PID_RENDERER=$!
python3 -u -m motion_engine.node               &  PID_MOTION=$!

echo "[nao-core] naoqi-driver(${PID_DRIVER})  cognitive-renderer(${PID_RENDERER})  motion-engine(${PID_MOTION})"

# Quitter dès qu'un nœud s'arrête (Docker redémarre le conteneur entier)
wait -n
echo "[nao-core] un nœud s'est arrêté — extinction du conteneur"
exit 1
