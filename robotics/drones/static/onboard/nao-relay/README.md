# Relais du NAO sur le drone statique

Le NAO ne parle plus directement au VPS : il envoie ses requêtes au drone
statique, sur le réseau local, et le drone les relaie en TLS
([ADR 006](../../../../../docs/decisions/006-nao-via-drone-statique.md)).

```
NAO (Wi-Fi) ── ws://, sans jeton ──► drone :9090 (Caddy)
                                        │ + Authorization: Bearer $ROSBRIDGE_TOKEN
                                        └─► wss://rosbridge.$DOMAIN (TLS) ─► VPS
```

Pourquoi : le Python 2.7 du NAO ne sait pas faire de TLS. Sans relais, le VPS
devait accepter rosbridge en HTTP clair (port 80), avec le jeton visible sur
Internet. Avec le relais, le VPS n'accepte plus que `wss://`, et le jeton
n'est plus stocké sur le robot. Le réseau local est considéré comme sûr.

| Fichier | Rôle |
|---|---|
| `Caddyfile` | Le relais : proxy inverse WebSocket, ajoute le jeton |
| `nao-relay.service` | Service systemd du relais |
| `setup-ubuntu.sh` | Installation automatique sur Ubuntu (idempotente) |
| `run.sh` | Lance le relais à la main, sans service |

Les commandes valent pour le PC Ubuntu comme pour le Raspberry Pi. Le drone
doit déjà fonctionner ([installation du drone](../README.md)).

## Installation automatique

Sur Ubuntu, `setup-ubuntu.sh` fait les étapes 2 et 3 et vérifie le relais
(tableau de l'étape 5). Il est idempotent.

```bash
bash ~/ia-agent/robotics/drones/static/onboard/nao-relay/setup-ubuntu.sh
bash ~/ia-agent/robotics/drones/static/onboard/nao-relay/run.sh   # lance le relais, Ctrl+C pour l'arrêter
```

Par défaut, le relais ne démarre pas tout seul : on le lance avec `run.sh`
quand le NAO doit joindre le VPS. Sur un PC dédié, `setup-ubuntu.sh --service`
installe le service `nao-relay`, lancé au boot ; relancer le script ne le
redémarre que si `.env`, le `Caddyfile` ou le service ont changé.

À lancer avec l'utilisateur du drone, **sans sudo**, après l'installation du
drone (`../setup-ubuntu.sh`) : il lit `ROSBRIDGE_TOKEN` dans le `.env` du
drone et propose `NAO_RELAY_UPSTREAM` d'après `ROSBRIDGE_URL`. À la fin, il
affiche l'adresse du drone et la ligne à mettre dans `autostart.sh` du NAO.
Restent à faire à la main : la réservation DHCP (étape 1) et le NAO (étape 4).

## 1. Une adresse fixe pour le drone

Le NAO doit retrouver le drone à la même adresse à chaque démarrage. Le plus
simple : une **réservation DHCP** pour le drone dans l'interface de la box.
Relever ensuite son adresse :

```bash
hostname -I          # ex. 192.168.1.20
```

## 2. Installer Caddy

```bash
sudo apt update
sudo apt install -y caddy
# Caddy démarre avec une config par défaut : on l'arrête, nao-relay la remplace.
sudo systemctl disable --now caddy
```

`caddy` est dans les dépôts d'Ubuntu 24.04 et de Raspberry Pi OS Bookworm. Sur
Ubuntu 22.04, ajouter d'abord le dépôt officiel :

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

## 3. Configurer et lancer le relais

Dans le `.env` du drone (`../.env`, voir `../.env.example`) :

```bash
NAO_RELAY_UPSTREAM=https://rosbridge.mondomaine.fr   # même hôte que ROSBRIDGE_URL, en https://
ROSBRIDGE_TOKEN=...                                  # déjà présent pour le drone
```

Par défaut le relais accepte tout le réseau local (192.168.x, 172.16-31.x,
10.x) et refuse le reste. Pour n'accepter que le NAO : `NAO_IP=192.168.1.42`.

Adapter `User` et les chemins de `nao-relay.service`, puis :

```bash
sudo cp nao-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nao-relay
journalctl -u nao-relay -f
```

Si le pare-feu du drone est actif (`sudo ufw status`), ouvrir le port au
réseau local : `sudo ufw allow from 192.168.0.0/16 to any port 9090 proto tcp`.

## 4. Côté NAO

Dans `/home/nao/autostart.sh`, l'URL pointe vers le drone et le jeton
disparaît :

```bash
#!/bin/bash
sleep 10   # wait for network
python /home/nao/nao_ws_bridge.py --vps ws://192.168.1.20:9090/ --provision &
```

Remplacer `192.168.1.20` par l'adresse du drone, supprimer la ligne
`export ROSBRIDGE_TOKEN=...`, puis redémarrer le bridge (ou le NAO). Le
journal du bridge doit afficher `[bridge] Connected.` puis `[bridge] Ready.`

## 5. Vérifications

| Test | Où | Attendu |
|---|---|---|
| `journalctl -u nao-relay -f` | drone | une ligne `status: 101` à chaque connexion du NAO |
| `ros2 topic echo /nao/status` | VPS | la batterie du NAO toutes les 30 s |
| `curl -i http://rosbridge.mondomaine.fr/` | n'importe où | plus de rosbridge en HTTP clair (404 ou redirection) |

Si le NAO n'arrive pas à se connecter, essayer depuis un autre ordinateur du
réseau local :

```bash
curl -i -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  http://192.168.1.20:9090/
```

`101 Switching Protocols` : le relais et le VPS répondent. `403` : l'adresse
n'est pas acceptée (voir `NAO_IP`). `401` : jeton refusé par le VPS
(`ROSBRIDGE_TOKEN` du drone différent de celui du VPS). `502` : le drone
n'atteint pas `NAO_RELAY_UPSTREAM`.
