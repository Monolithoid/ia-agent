# Drone statique

Corps fixe (PC Ubuntu ou Raspberry Pi 5) posé dans la pièce : micros, caméra,
haut-parleur ([ADR 004](../../../docs/decisions/004-drone-statique.md)).
Il se connecte en sortant au même cerveau (`robotics/brain/`) que les autres
drones, via rosbridge.

- [`onboard/`](onboard/README.md) — programme embarqué, installation pas à pas.
