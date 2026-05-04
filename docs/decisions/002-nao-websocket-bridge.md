# ADR 002 — Bridge WebSocket embarqué sur NAO (sans VPN ni Raspberry Pi)

**Date :** 2026-04-18  
**Statut :** Accepté

## Contexte

Le protocole NAOqi (port 9559, binaire propriétaire) ne traverse pas Internet nativement.
Il faut un moyen de piloter NAO depuis le VPS.

Trois approches étudiées :

| Option | Description | Rejet |
|--------|-------------|-------|
| VPN (WireGuard/OpenVPN) | Tunnel L3 entre NAO et VPS | NAO v5/v6 : configuration réseau limitée, OpenVPN instable sur Gentoo embarqué |
| Raspberry Pi intermédiaire | RPi en bordure, fait le bridge | Matériel supplémentaire, SPOF physique, latence +50 ms |
| **Script WebSocket sur NAO** | Python 2.7 embarqué, connexion sortante vers VPS | ✅ Retenu |

## Décision

Embarquer **`nao_ws_bridge.py`** directement sur NAO.

Le script :
1. Se connecte au démon NAOqi local (`localhost:9559`) via le SDK `qi`
2. Ouvre une connexion WebSocket **sortante** vers `rosbridge_suite` sur le VPS (port 9090)
3. Souscrit aux topics `/nao/*` et traduit les messages JSON en appels NAOqi

## Raisons

- **Connexion sortante** : NAO initie la connexion — aucune règle firewall entrante requise sur le robot.
- **Zéro matériel** : Pas de RPi, pas de routeur intermédiaire.
- **Protocole standard** : rosbridge WebSocket + JSON est simple, débogable avec `wscat`, pas de dépendance binaire côté VPS.
- **Résilience** : Le script implémente une reconnexion automatique avec backoff exponentiel.

## Contraintes

- NAO v5/v6 tourne Python 2.7 — le script est écrit en Python 2.7 compatible.
- `websocket-client==0.59.0` est la dernière version supportant Python 2.7.
- Le port 9090 du VPS doit être accessible publiquement (règle `ufw allow 9090`).

## Conséquences

- Le VPS n'a pas besoin de connaître l'IP de NAO — c'est NAO qui se connecte.
- Si NAO est hors réseau, les commandes s'accumulent dans la file rosbridge et sont livrées à la reconnexion (selon la config QoS).
- Extensible aux drones : même pattern, script différent côté drone.

---

← [Toutes les décisions](../../README.md#décisions-darchitecture-adr) · [Robot NAO](../nao.md)
