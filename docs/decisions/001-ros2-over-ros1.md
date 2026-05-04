# ADR 001 — ROS 2 plutôt que ROS 1

**Date :** 2026-04-18  
**Statut :** Accepté

## Contexte

Besoin d'un middleware robotique pour piloter un NAO et, à terme, des drones.
Deux options : ROS 1 (Noetic) et ROS 2 (Jazzy).

## Décision

Utiliser **ROS 2 Jazzy Jalisco**.

## Raisons

- **Fin de vie ROS 1** : ROS Noetic atteint sa fin de vie en mai 2025. Aucun correctif de sécurité après cette date.
- **Multi-robots natif** : ROS 2 utilise DDS (Data Distribution Service) décentralisé — pas de `roscore` unique, pas de SPOF.
- **Sécurité** : DDS supporte l'authentification et le chiffrement nativement (SROS 2).
- **Drones** : PX4, DJI et Parrot ont tous des drivers ROS 2 officiels. Aucune migration nécessaire pour étendre la stack.
- **Docker-friendly** : L'image officielle `ros:jazzy-ros-base` est maintenue et légère.

## Conséquences

- Le bridge NAO utilise `rosbridge_suite` pour traduire WebSocket JSON ↔ topics ROS 2, ce qui évite d'avoir NAOqi comme dépendance directe dans les containers VPS.
- Aucune compatibilité ROS 1 requise ; pas de `ros1_bridge` dans la stack.

---

← [Toutes les décisions](../../README.md#décisions-darchitecture-adr) · [Architecture](../architecture.md)
