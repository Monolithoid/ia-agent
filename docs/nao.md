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

```bash
ssh nao@NAO_IP

# Installer websocket-client (Python 2.7)
pip install websocket-client==0.59.0
```

> Si `pip` n'est pas disponible sur le NAO :
> ```bash
> curl https://bootstrap.pypa.io/pip/2.7/get-pip.py | python
> ```

### 3. Lancer le bridge manuellement

```bash
# Remplacer VPS_IP par l'IP publique ou le domaine du VPS
python /home/nao/nao_ws_bridge.py --vps ws://VPS_IP:9090
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
python /home/nao/nao_ws_bridge.py --vps ws://VPS_IP:9090 &
```

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
| `ImportError: No module named websocket` | websocket-client non installé | `pip install websocket-client==0.59.0` |
| Le robot ne parle pas | rosbridge non démarré ou topic incorrect | `docker logs ros2-bridge` sur le VPS |
| Reconnexions en boucle | Timeout réseau NAO | Normal — le script reconnecte automatiquement |
