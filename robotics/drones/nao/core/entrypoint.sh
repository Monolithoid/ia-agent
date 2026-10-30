#!/usr/bin/env bash
# Démarre les nœuds ROS2 en parallèle : naoqi-driver, cognitive-renderer,
# motion-engine (si MOTION_ENGINE=1), static-driver (drone statique) sauf si DRONE_ID est vide, et
# un second static-driver pour la voix OBS si OBS_DRONE_ID est défini (ADR 005).
# Si l'un s'arrête, le conteneur s'arrête → Docker le redémarre entier.
# docker stop (SIGTERM) est relayé à tous les nœuds, puis on attend leur fin.
set -euo pipefail

# setup.bash lit des variables non définies (AMENT_TRACE_SETUP_FILES…) :
# incompatible avec « set -u » sur l'image ros:humble-ros-base actuelle.
set +u
source /opt/ros/humble/setup.bash
set -u

echo "[nao-core] démarrage des nœuds…"

PIDS=()

python3 -u /ros2_ws/naoqi_driver_node.py      &  PID_DRIVER=$!;   PIDS+=($!)
python3 -u /ros2_ws/cognitive_renderer_node.py &  PID_RENDERER=$!; PIDS+=($!)
echo "[nao-core] naoqi-driver(${PID_DRIVER})  cognitive-renderer(${PID_RENDERER})"

# motion-engine envoie des angles libres à 20 Hz : coupé dans l'univers moteur
# contraint (profil calibré depuis la régie).  MOTION_ENGINE=1 le relance.
if [ "${MOTION_ENGINE:-0}" = "1" ]; then
    python3 -u -m motion_engine.node           &  PID_MOTION=$!; PIDS+=($!)
    echo "[nao-core] motion-engine(${PID_MOTION})"
else
    echo "[nao-core] motion-engine coupé (MOTION_ENGINE=1 pour le lancer)"
fi

if [ -n "${DRONE_ID:-}" ]; then
    python3 -u /ros2_ws/static_driver_node.py  &  PID_STATIC=$!; PIDS+=($!)
    echo "[nao-core] static-driver(${PID_STATIC}) pour ${DRONE_ID}"
else
    echo "[nao-core] DRONE_ID vide — static-driver non démarré"
fi

# Voix du stream (ADR 005) : même driver, autre corps (synthèse edge-tts → OBS).
if [ -n "${OBS_DRONE_ID:-}" ]; then
    DRONE_ID="$OBS_DRONE_ID" python3 -u /ros2_ws/static_driver_node.py  &  PID_OBS=$!; PIDS+=($!)
    echo "[nao-core] static-driver(${PID_OBS}) pour ${OBS_DRONE_ID} (voix OBS)"
fi

# bash est le PID 1 du conteneur : sans trap, le noyau ignore SIGTERM et
# docker stop attend son délai avant SIGKILL, sans que les nœuds soient prévenus.
stop() {
    echo "[nao-core] arrêt demandé — SIGTERM aux nœuds (${PIDS[*]})"
    kill -TERM "${PIDS[@]}" 2>/dev/null || true
    wait
    exit 0
}
trap stop TERM INT

# Quitter dès qu'un nœud s'arrête (Docker redémarre le conteneur entier)
wait -n
echo "[nao-core] un nœud s'est arrêté — extinction du conteneur"
exit 1
