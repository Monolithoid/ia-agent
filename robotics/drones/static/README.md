# Drone statique

Corps fixe (PC Ubuntu ou Raspberry Pi 5) posé dans la pièce : micros, caméra,
haut-parleur ([ADR 004](../../../docs/decisions/004-drone-statique.md)).
Il se connecte en sortant au même cerveau (`robotics/brain/`) que les autres
drones, via rosbridge.

- [`onboard/`](onboard/README.md) — programme embarqué, installation pas à pas
  ([sur un PC portable Ubuntu](onboard/pc-portable-ubuntu.md)).
- [`driver/`](driver/static_driver_node.py) — nœud ROS 2 côté VPS (conteneur
  `nao-core`) : `/drones/{id}/*` ↔ `/bodies/{id}/*`, Whisper et edge-tts
  ([détails](../../../docs/bridge-ia-ros2-nao.md#static_driver)).
- Test sans matériel : `python3 tests/fake-static-drone.py` depuis la racine
  du dépôt, sur le VPS ([maillon 9](../../../docs/test-chain.md#maillon-9--drone-statique-simulé-faux-messages-rosbridge)).
