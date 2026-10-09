# Relais du NAO sur le drone statique

Le NAO ne sort plus seul sur Internet : il parle au drone statique par un câble
Ethernet, et le drone relaie vers le VPS
([ADR 006](../../../../../docs/decisions/006-nao-via-drone-statique.md)).

```
NAO 192.168.50.2 ── câble, ws:// sans jeton ──► drone 192.168.50.1:9090 (Caddy)
                                                   │ + Authorization: Bearer $ROSBRIDGE_TOKEN
                                                   └─► Wi-Fi ─► https://rosbridge.$DOMAIN (TLS)
```

- Le jeton `ROSBRIDGE_TOKEN` n'est plus sur le robot et ne circule plus qu'en TLS.
- Le NAO n'a ni passerelle ni DNS : il ne peut joindre que le drone.
- Le relais refuse (403) toute connexion qui ne vient pas de `NAO_IP`.

| Fichier | Rôle |
|---|---|
| `Caddyfile` | Le relais : proxy inverse WebSocket, ajoute le jeton |
| `nao-relay.service` | Service systemd du relais |
| `dnsmasq-nao.conf` | DHCP du câble : donne `192.168.50.2` au NAO, sans passerelle |

Les commandes ci-dessous valent pour le PC Ubuntu comme pour le Raspberry Pi.
Le drone doit déjà fonctionner ([installation du drone](../README.md)) et
rester connecté à Internet par son **Wi-Fi** : la prise **Ethernet** est
réservée au NAO.

## 1. Installer Caddy, dnsmasq et chrony

```bash
sudo apt update
sudo apt install -y caddy dnsmasq chrony
# Caddy et dnsmasq démarrent avec une config par défaut : on les arrête,
# nos propres fichiers les remplacent.
sudo systemctl disable --now caddy
```

`caddy` est dans les dépôts d'Ubuntu 24.04 et de Raspberry Pi OS Bookworm. Sur
Ubuntu 22.04, il faut d'abord ajouter le dépôt officiel :

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

## 2. Donner une IP fixe à la prise Ethernet du drone

Repérer le nom de l'interface Ethernet (`enp…` sur un PC, `eth0` sur le Pi) :

```bash
ip link
```

Puis, avec NetworkManager (Ubuntu Desktop et Raspberry Pi OS Bookworm),
en remplaçant `eth0` :

```bash
sudo nmcli con add type ethernet ifname eth0 con-name nao-lan \
  ipv4.method manual ipv4.addresses 192.168.50.1/24 ipv4.never-default yes \
  ipv6.method disabled
sudo nmcli con up nao-lan
```

`ipv4.never-default yes` garde la route Internet sur le Wi-Fi.

## 3. DHCP pour le NAO

```bash
sudo cp dnsmasq-nao.conf /etc/dnsmasq.d/nao.conf
sudo nano /etc/dnsmasq.d/nao.conf      # remplacer eth0 par le nom de l'interface
sudo systemctl restart dnsmasq
```

Le NAO reçoit `192.168.50.2`, sans passerelle ni DNS.

## 4. Heure pour le NAO (chrony)

```bash
echo 'allow 192.168.50.0/24' | sudo tee /etc/chrony/conf.d/nao.conf
sudo systemctl restart chrony
```

## 5. Pare-feu du drone

N'accepter du câble que le relais, le DHCP et l'heure, et ne rien router :

```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH                       # garder l'accès SSH au drone
sudo ufw default deny incoming
sudo ufw default deny routed                 # le drone ne route rien pour le NAO
sudo ufw allow in on eth0 from 192.168.50.2 to any port 9090 proto tcp   # relais
sudo ufw allow in on eth0 to any port 67 proto udp                       # DHCP
sudo ufw allow in on eth0 from 192.168.50.2 to any port 123 proto udp    # heure
sudo ufw enable
sudo ufw status verbose
```

Remplacer `eth0` par le nom de l'interface. Vérifier aussi que le routage est
coupé : `sysctl net.ipv4.ip_forward` doit afficher `0` (Docker le met à `1` ;
`deny routed` bloque alors le passage).

## 6. Configurer et lancer le relais

Dans le `.env` du drone (`../.env`, voir `../.env.example`) :

```bash
NAO_IP=192.168.50.2
NAO_RELAY_UPSTREAM=https://rosbridge.mondomaine.fr   # même hôte que ROSBRIDGE_URL, en https://
ROSBRIDGE_TOKEN=...                                  # déjà présent pour le drone
```

Adapter `User` et les chemins de `nao-relay.service`, puis :

```bash
sudo cp nao-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nao-relay
journalctl -u nao-relay -f
```

Test depuis le drone lui-même : le relais doit **refuser** (403), car la
demande ne vient pas de l'IP du NAO.

```bash
curl -i http://127.0.0.1:9090/
```

## 7. Côté NAO

1. **Brancher le câble** entre la prise Ethernet à l'arrière de la tête du NAO
   et celle du drone. Un appui sur le bouton du torse doit annoncer
   `192.168.50.2` (sinon : `journalctl -u dnsmasq` sur le drone).
2. **Couper le Wi-Fi du NAO**, depuis le drone :

   ```bash
   ssh nao@192.168.50.2
   connmanctl disable wifi
   ```

   Si la commande est refusée (droits), passer par la page web du NAO
   (`http://192.168.50.2`, réglages réseau). Sans Wi-Fi ni passerelle, le NAO n'a plus aucune route
   vers Internet.
3. **Changer `autostart.sh`** : l'URL pointe vers le drone et le jeton
   disparaît.

   ```bash
   #!/bin/bash
   sleep 10   # wait for network
   python /home/nao/nao_ws_bridge.py --vps ws://192.168.50.1:9090/ --provision &
   ```

   Supprimer la ligne `export ROSBRIDGE_TOKEN=...` et ne plus passer `--token`.

4. **Redémarrer le bridge** (ou le NAO) et vérifier son journal :
   `[bridge] Connected.` puis `[bridge] Ready.`

## 8. Vérifications

| Test | Où | Attendu |
|---|---|---|
| `ping -c1 8.8.8.8` | NAO | `Network is unreachable` |
| `curl -i http://127.0.0.1:9090/` | drone | `403 Forbidden` |
| `journalctl -u nao-relay -f` | drone | une ligne `status: 101` à chaque connexion du NAO |
| `ros2 topic echo /nao/status` | VPS | la batterie du NAO toutes les 30 s |

## Accès au NAO pour l'administration

Le NAO n'est plus joignable depuis le réseau de la maison. Passer par le drone :

```bash
ssh -J utilisateur@drone nao@192.168.50.2               # SSH
ssh -L 9559:192.168.50.2:9559 utilisateur@drone         # puis Choregraphe sur localhost:9559
```

## Revenir en arrière

Débrancher le câble, réactiver le Wi-Fi du NAO (`connmanctl enable wifi`),
remettre dans `autostart.sh` l'ancienne URL `ws://rosbridge.$DOMAIN/` et le
jeton, puis `sudo systemctl disable --now nao-relay` sur le drone.
