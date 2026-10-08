#!/usr/bin/env bash
# Démarre les nœuds ROS2 en parallèle : naoqi-driver, cognitive-renderer,
# motion-engine, et static-driver (drone statique) sauf si DRONE_ID est vide.
# Si l'un s'arrête, le conteneur s'arrête → Docker le redémarre entier.
set -euo pipefail

# setup.bash lit des variables non définies (AMENT_TRACE_SETUP_FILES…) :
# incompatible avec « set -u » sur l'image ros:humble-ros-base actuelle.
set +u
source /opt/ros/humble/setup.bash
set -u

echo "[nao-core] démarrage des nœuds…"

python3 -u /ros2_ws/naoqi_driver_node.py      &  PID_DRIVER=$!
python3 -u /ros2_ws/cognitive_renderer_node.py &  PID_RENDERER=$!
python3 -u -m motion_engine.node               &  PID_MOTION=$!

echo "[nao-core] naoqi-driver(${PID_DRIVER})  cognitive-renderer(${PID_RENDERER})  motion-engine(${PID_MOTION})"

if [ -n "${DRONE_ID:-}" ]; then
    python3 -u /ros2_ws/static_driver_node.py  &  PID_STATIC=$!
    echo "[nao-core] static-driver(${PID_STATIC}) pour ${DRONE_ID}"
else
    echo "[nao-core] DRONE_ID vide — static-driver non démarré"
fi

# Quitter dès qu'un nœud s'arrête (Docker redémarre le conteneur entier)
wait -n
echo "[nao-core] un nœud s'est arrêté — extinction du conteneur"
exit 1
