# Robot NAO

Documentation du bridge embarqué sur le robot NAO et de son intégration avec la stack VPS.

---

## Principe de connexion

Le NAO ne peut pas recevoir de connexions entrantes depuis internet (pare-feu réseau local, NAT). La solution retenue est un **script Python 2.7 embarqué sur le robot** qui établit une connexion WebSocket **sortante** vers le rosbridge du VPS.

```
NAO (réseau local)
  └─► nao_ws_bridge.py (Python 2.7)
        └─► WebSocket sortant ws://VPS_IP:9090
              └─► ros2-bridge (ROS 2 Jazzy, VPS)
```

Une fois connecté, le bridge s'abonne aux topics ROS 2 et exécute les commandes reçues via NAOqi (SDK natif du robot). Il publie aussi les données de statut (batterie, température) vers le VPS.

> Voir [ADR 002](decisions/002-nao-websocket-bridge.md) pour les alternatives rejetées (VPN, Raspberry Pi intermédiaire).

---

## Fichier `nao_ws_bridge.py`

Situé dans `robotics/nao_brain/naoqi_bridge/` dans le dépôt, ce script Python 2.7 :

- Se connecte à rosbridge via WebSocket avec reconnexion automatique
- S'abonne aux topics `/nao/speech`, `/nao/cmd_vel`, `/nao/gesture`, `/nao/leds`, `/nao/stop`
- Publie sur `/nao/status` (batterie + température toutes les 30 s)
- Utilise le SDK NAOqi (`qi` ou `naoqi`) pour piloter le robot

---

## Déploiement sur le robot

### 1. Copier le script

```bash
# Depuis la racine du dépôt, sur ta machine de développement
scp robotics/nao_brain/naoqi_bridge/app.py nao@NAO_IP:/home/nao/nao_ws_bridge.py
```

### 2. Installer la dépendance Python

Sur NAO 2.1.x, `pip` n'est pas préinstallé. Il faut d'abord l'ajouter.

**a) Installer pip**

```bash
ssh nao@NAO_IP
wget --no-check-certificate \
     https://bootstrap.pypa.io/pip/2.7/get-pip.py \
     -O /home/nao/get-pip.py
python /home/nao/get-pip.py --user
export PATH=$PATH:/home/nao/.local/bin
```

Pour rendre le `PATH` permanent, ajouter la ligne `export` dans `/home/nao/.bashrc`.

> **Si le robot ne peut pas accéder à HTTPS**, transférer `get-pip.py` depuis ta machine de dev :
> ```bash
> # Sur ta machine de dev
> wget https://bootstrap.pypa.io/pip/2.7/get-pip.py
> scp get-pip.py nao@NAO_IP:/home/nao/
> # Puis sur le NAO
> python /home/nao/get-pip.py --user
> export PATH=$PATH:/home/nao/.local/bin
> ```

**b) Installer websocket-client**

```bash
pip install --user websocket-client==0.59.0
```

### 3. Lancer le bridge manuellement

```bash
# Remplacer VPS_IP par l'IP publique ou le domaine du VPS
python /home/nao/nao_ws_bridge.py --vps wss://rosbridge.$DOMAIN
```

Les logs indiquent la connexion réussie :
```
[bridge] Connecting to ws://VPS_IP:9090 ...
[bridge] Connected. Subscribing to topics...
[bridge] Ready.
```

---

## Lancement automatique au démarrage

### Via Choregraphe

Créer `/home/nao/autostart.sh` :
```bash
#!/bin/bash
sleep 10   # attendre que le réseau soit établi
python /home/nao/nao_ws_bridge.py --vps wss://rosbridge.mon-domain.com &
```

> Remplacer `rosbridge.mon-domain.com` par la valeur de `$ROSBRIDGE_DOMAIN`.

Puis dans Choregraphe : **Edit** → **Preferences** → **Autorun** → ajouter ce script.

### Via autorun.ini (alternative)

Éditer `/home/nao/.config/autostart/` ou le fichier `autorun.ini` de la préférence NAOqi selon la version du firmware.

---

## Topics disponibles

| Topic | Direction | Action NAO |
|---|---|---|
| `/nao/speech` | VPS → NAO | `ALTextToSpeech.say()` |
| `/nao/cmd_vel` | VPS → NAO | `ALMotion.moveToward()` |
| `/nao/gesture` | VPS → NAO | `ALRobotPosture.goToPosture()` |
| `/nao/leds` | VPS → NAO | `ALLeds.fadeRGB()` |
| `/nao/stop` | VPS → NAO | `ALMotion.stopMove()` |
| `/nao/status` | NAO → VPS | Batterie + température (JSON) |

---

## Vérification de la connexion

**Sur le VPS :**
```bash
# Vérifier que le rosbridge voit le robot connecté
docker logs ros2-bridge -f
# Chercher une ligne du type : "Client connected from NAO_IP"

# Tester une commande (NAO doit parler)
curl -X POST http://localhost:8001/nao/say \
  -H "X-API-Key: $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Test de connexion"}'
```

**Sur le robot :**
```bash
ssh nao@NAO_IP
# Vérifier que le processus tourne
ps aux | grep nao_ws_bridge
```

---

## Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| Bridge ne se connecte pas | Port 9090 bloqué ou VPS_IP incorrect | Vérifier `ufw allow 9090` sur le VPS et l'IP utilisée |
| `pip command not found` | pip absent sur OpenNAO 2.1.x | Voir section « Installer la dépendance Python » ci-dessus (wget + --user) |
| `ImportError: No module named websocket` | websocket-client non installé | `pip install --user websocket-client==0.59.0` |
| `websocket-client` installé mais `import websocket` échoue | PATH `~/.local/bin` absent | `export PATH=$PATH:/home/nao/.local/bin` puis relancer |
| Le robot ne parle pas | rosbridge non démarré ou topic incorrect | `docker logs ros2-bridge` sur le VPS |
| Reconnexions en boucle | Timeout réseau NAO | Normal — le script reconnecte automatiquement |
