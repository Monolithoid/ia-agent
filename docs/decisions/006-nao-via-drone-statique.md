# ADR 006 — Le NAO passe par le drone statique pour joindre le VPS

**Date :** 2026-10-09  
**Statut :** Accepté

## Contexte

Jusqu'ici le NAO se connectait seul au VPS ([ADR 002](002-nao-websocket-bridge.md)) :

```
NAO ── ws:// en clair, port 80 ──► Traefik (routeur rosbridge-ws, entrypoint http)
        jeton Authorization: Bearer      └─► rosbridge-proxy (Caddy, vérifie le jeton)
                                               └─► ros2-bridge :9090
```

`nao_ws_bridge.py` ne sait faire que du `ws://` : le Python 2.7 d'OpenNAO n'a
pas de TLS utilisable. Le VPS devait donc garder un routeur Traefik en **HTTP
clair** rien que pour le NAO, et le jeton `ROSBRIDGE_TOKEN` traversait
Internet sans chiffrement. Le jeton était aussi stocké sur le robot.

Le drone statique ([ADR 004](004-drone-statique.md)) est dans la même pièce,
sur une machine à jour (PC Ubuntu, puis Raspberry Pi 5), et sort déjà en
`wss://` avec le même jeton.

**Objectif : plus de HTTP en clair sur le VPS.** Le réseau local est considéré
comme sûr : on ne cherche pas à isoler le NAO.

## Décision

Le NAO reste en Wi-Fi sur le réseau local et envoie ses requêtes à un **relais
Caddy sur le drone statique**, qui les transmet au VPS en TLS et ajoute le jeton.

```
NAO (Wi-Fi) ── ws://, sans jeton ──► drone :9090 (Caddy)
                                        │ + Authorization: Bearer $ROSBRIDGE_TOKEN
                                        └─► wss://rosbridge.$DOMAIN (TLS) ─► VPS
```

- Côté NAO : `--vps ws://<IP du drone>:9090/`, plus de `--token`. Aucun
  changement de code.
- Côté drone : `robotics/drones/static/onboard/nao-relay/` (`Caddyfile` +
  service systemd), configuré par le `.env` du drone (`NAO_RELAY_UPSTREAM`,
  `ROSBRIDGE_TOKEN`). Le relais n'accepte que les adresses privées (ou
  `NAO_IP` si on la précise) : le jeton ne part jamais vers Internet, même si
  le port 9090 se retrouvait exposé.
- Côté VPS : le routeur Traefik `rosbridge-ws` (entrypoint `http`) est
  supprimé ; rosbridge n'est plus joignable qu'en `wss://`.

## Options écartées

| Option | Pourquoi |
|---|---|
| TLS sur le NAO (`stunnel` embarqué) | Dépend d'`opkg`/OpenSSL sur un firmware ancien ; reste documenté dans [nao.md](../nao.md#ssl--wss) pour un NAO sans drone |
| ROS 2 / naoqi_driver sur le drone | Alourdit le drone (contraire à l'ADR 004) et double le code du bridge embarqué |
| Isolement réseau du NAO (câble, VLAN, pare-feu) | Pas l'objectif : le réseau local est considéré comme sûr |
| VPN drone ↔ VPS | Plus complexe pour le même résultat ; `wss://` chiffre déjà |

## Ce qu'on perd

- **NAO sans drone** : il ne peut plus joindre le VPS directement (plus de
  HTTP clair). Il faut le relais, ou `stunnel` sur le robot.
- **Un service de plus sur le drone** (Caddy), et le drone doit garder une
  adresse fixe sur le réseau local (réservation DHCP).

## Ce qu'on garde

- Le code du NAO (`nao_ws_bridge.py`) : inchangé.
- Le pattern « connexion sortante » de l'ADR 002, côté drone comme côté NAO.
- Le drone reste léger : un proxy inverse ne fait que relayer des octets.

## Conséquences

- Amende l'[ADR 002](002-nao-websocket-bridge.md) : un intermédiaire, rejeté
  alors, est retenu maintenant que le drone existe.
- **Ordre de déploiement** : installer le relais et changer `autostart.sh` sur
  le NAO **avant** de redéployer le VPS sans le routeur HTTP, sinon le NAO perd
  la connexion entre les deux.

---

← [Toutes les décisions](../../README.md#décisions-darchitecture-adr) · [Robot NAO](../nao.md) · [ADR 004](004-drone-statique.md)
