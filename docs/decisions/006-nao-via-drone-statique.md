# ADR 006 — Le NAO ne parle à l'extérieur qu'à travers un drone statique

**Date :** 2026-10-09  
**Statut :** Accepté (étapes 1 et 2 livrées : [`nao-relay/`](../../robotics/drones/static/onboard/nao-relay/README.md))

## Contexte

Aujourd'hui le NAO sort seul sur Internet ([ADR 002](002-nao-websocket-bridge.md)) :

```
NAO ── ws:// en clair, port 80 ──► Traefik (routeur rosbridge-ws, entrypoint http)
        jeton Authorization: Bearer      └─► rosbridge-proxy (Caddy, vérifie le jeton)
                                               └─► ros2-bridge :9090 → nao-core, body-gateway
```

Points faibles de ce chemin :

1. **Pas de TLS.** `nao_ws_bridge.py` n'ouvre que du `ws://` : le module `ssl`
   peut manquer sur OpenNAO (Python 2.7). Le jeton `ROSBRIDGE_TOKEN` traverse
   donc Internet en clair. Quiconque l'intercepte peut publier sur tous les
   topics ROS (parler, bouger le NAO, injecter du texte au cerveau).
2. **Le secret vit sur le robot**, dans `autostart.sh`, sur un système Gentoo
   ancien et non maintenu.
3. **Le NAO a une route vers Internet.** Son firmware (NAOqi 2.1/2.8) n'a plus
   de mises à jour de sécurité ; il peut aussi joindre les services cloud
   d'origine, NTP, etc. Toute faille du robot est exposée.
4. Le VPS garde un routeur Traefik en HTTP clair rien que pour le NAO.

Le drone statique ([ADR 004](004-drone-statique.md)) est désormais dans la même
pièce, sur une machine maintenue (PC Ubuntu, puis Raspberry Pi 5), et sort déjà
en `wss://` avec le même jeton. L'objection de l'ADR 002 contre un
intermédiaire (« matériel en plus, point de défaillance, +50 ms ») ne tient
plus : le matériel est déjà là, la perception dépend déjà de lui, et un saut
sur le réseau local coûte ~1 ms.

**Principe directeur (ADR 004) : simple, même imparfait.**

## Options étudiées

| Option | Principe | Changement NAO | Changement drone | Verdict |
|---|---|---|---|---|
| A. Relais WebSocket sur le drone | Le NAO se connecte en `ws://` au drone (LAN) ; le drone relaie en `wss://` vers le VPS et ajoute le jeton | Seulement l'URL `--vps` ; plus de jeton | Un proxy inverse (Caddy) en config | ✅ **Retenu** |
| B. ROS 2 / naoqi_driver sur le drone | Le drone fait tourner ROS 2 et pilote NAOqi (port 9559) en local | Aucun script sur le robot | ROS 2 Humble + SDK `qi` (Python 2.7) sur le drone | ❌ Le drone devient lourd (contraire à l'ADR 004), SDK `qi` difficile hors x86/Py2, double le code du `nao_ws_bridge.py` |
| C. Isolement réseau seul | Le NAO sur un réseau sans passerelle | — | Pare-feu | ⚠️ Nécessaire mais pas suffisant : il faut quand même un relais → complète A |
| D. VPN (WireGuard) drone ↔ VPS, NAO routé dedans | Le drone route le trafic du NAO dans un tunnel | Passerelle = drone | WireGuard + routage + NAT | ❌ Plus complexe que A pour le même résultat ; `wss://` chiffre déjà |
| E. Proxy générique (Squid/SOCKS) | Le NAO sort via un proxy HTTP | Le client WebSocket maison ne gère pas `CONNECT` | Squid | ❌ Demande de modifier le client ; filtre moins bien qu'un relais dédié |

## Décision

**A + C : un relais WebSocket sur le drone statique, et un NAO sans accès
direct à Internet.**

```
NAO ── ws:// (LAN, sans jeton) ──► drone statique :9090 (Caddy)
                                      │ ajoute Authorization: Bearer $ROSBRIDGE_TOKEN
                                      └─► wss://rosbridge.$DOMAIN (TLS) ─► VPS (inchangé)
```

### 1. Relais (étape 1, sans toucher au code)

Sur le drone, un **Caddy** en proxy inverse (paquet `caddy` sur Ubuntu/Raspberry
Pi OS, service systemd) :

```caddyfile
:9090 {
	@nao remote_ip {$NAO_IP}             # IP du NAO, seule source acceptée
	handle @nao {
		reverse_proxy {$NAO_RELAY_UPSTREAM} {   # https://rosbridge.$DOMAIN
			header_up Host {upstream_hostport}
			header_up Authorization "Bearer {$ROSBRIDGE_TOKEN}"
		}
	}
	respond 403
}
```

- Côté NAO : `--vps ws://<IP du drone>:9090/` et **plus de `--token`** ; le
  secret est retiré de `autostart.sh`.
- Le jeton n'existe plus que sur le drone et le VPS ; il ne voyage plus qu'en TLS.
- Caddy gère nativement l'upgrade WebSocket ; aucune ligne de Python à écrire.
- Le relais est indépendant de `static_drone.py` : un redémarrage du drone
  logiciel ne coupe pas le NAO.

### 2. Isolement (étape 2)

Méthode la plus simple : **un câble Ethernet direct NAO ↔ drone** (le NAO a un
port Ethernet à l'arrière de la tête ; le PC portable comme le Pi en ont un),
le drone restant connecté à Internet par son Wi-Fi.

- Le drone sert une IP fixe au NAO (`192.168.50.2/24`, drone `192.168.50.1`),
  par `dnsmasq` ou en IP statique côté NAO, **sans passerelle par défaut**.
- Wi-Fi du NAO désactivé (`connmanctl disable wifi`) pour qu'il n'ait aucune
  autre route.
- Sur le drone, `net.ipv4.ip_forward=0` et un pare-feu (`nftables`/`ufw`) sur
  l'interface Ethernet : n'accepter du NAO que **TCP 9090** (relais) et
  **UDP 123** (NTP servi par `chrony` sur le drone, l'horloge du NAO compte pour
  les journaux) ; tout le reste rejeté.
- Accès d'administration au NAO (SSH, Choregraphe sur 9559) : depuis le drone,
  ou depuis un poste via un rebond SSH
  (`ssh -J user@drone nao@192.168.50.2`, `ssh -L 9559:192.168.50.2:9559 user@drone`).

Variante sans câble (NAO en Wi-Fi) : NAO et drone sur un SSID ou VLAN invité
sans accès Internet, ou blocage au niveau de la box (contrôle parental /
filtrage par MAC). Moins fiable, à n'utiliser que si le câble est impossible.

### 3. Côté VPS (étape 3, une fois les étapes 1-2 validées)

- Supprimer le routeur Traefik `rosbridge-ws` (entrypoint `http`) : plus aucun
  client en clair.
- Option : un jeton distinct par drone (`ROSBRIDGE_TOKEN_STATIC_01`) pour pouvoir
  en révoquer un seul. Hors périmètre ici.

## Ce qu'on perd

- **Autonomie du NAO** : sans drone allumé, le NAO n'atteint plus le cerveau.
  C'était déjà en pratique le cas pour la voix et l'écoute depuis l'ADR 004.
- **Retour à la voix du NAO seul** (sans drone, cf. `docs/nao.md`) : il faudra
  rebrancher le NAO sur le réseau domestique et repasser `--vps` et `--token`.
- **Accès direct au NAO** depuis le réseau domestique (Choregraphe, SSH) : passe
  par le drone.
- **Un service de plus à surveiller sur le drone** (Caddy).

## Ce qu'on garde

- Le code du NAO (`nao_ws_bridge.py`) et du VPS : inchangés à l'étape 1.
- Le pattern « connexion sortante » de l'ADR 002, côté drone comme côté NAO.
- Le drone reste léger : un proxy inverse ne fait que relayer des octets.

## Conséquences

- Amende l'[ADR 002](002-nao-websocket-bridge.md) : l'option « intermédiaire »
  rejetée est retenue maintenant que le drone existe.
- Livré dans `robotics/drones/static/onboard/nao-relay/` : `Caddyfile`, service
  systemd `nao-relay`, `dnsmasq-nao.conf` et la doc d'installation (câble, IP
  fixe, DHCP, `chrony`, pare-feu, tests). Variables `NAO_IP` et
  `NAO_RELAY_UPSTREAM` dans le `.env` du drone. Le relais échoue fermé : sans
  `NAO_IP`, il refuse tout (403).
- `docs/nao.md` décrit la nouvelle URL `--vps` et le robot sans jeton.
- Reste l'étape 3 (supprimer le routeur Traefik `rosbridge-ws` sur le VPS),
  une fois le relais validé sur le vrai matériel.
- Si plusieurs drones statiques existent, un seul (configurable) porte le
  relais du NAO.

---

← [Toutes les décisions](../../README.md#décisions-darchitecture-adr) · [Robot NAO](../nao.md) · [ADR 004](004-drone-statique.md)
