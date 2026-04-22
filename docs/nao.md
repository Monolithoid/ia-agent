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

## Deux scripts, deux rôles

| Script | Tourne sur | Rôle |
|---|---|---|
| `robotics/nao_brain/naoqi_bridge/nao_ws_bridge.py` | **Robot NAO** | WebSocket client → rosbridge VPS |
| `robotics/nao_brain/naoqi_bridge/app.py` | **VPS (Docker)** | Serveur Flask HTTP, utilise le SDK naoqi pour parler au NAO via TCP:9559 |

> `app.py` n'est **pas** destiné à tourner sur le robot — c'est un service VPS. Le déployer sur le NAO provoque une erreur `ImportError: No module named flask` car Flask n'est pas disponible sur OpenNAO.

### `nao_ws_bridge.py` (script embarqué sur le NAO)

- Se connecte à rosbridge via WebSocket **sortant** avec reconnexion automatique
- S'abonne aux topics `/nao/speech`, `/nao/cmd_vel`, `/nao/gesture`, `/nao/leds`, `/nao/stop`
- Publie sur `/nao/status` (batterie toutes les 30 s)
- Utilise le module `naoqi` natif du robot (pas besoin de l'installer)

---

## Déploiement sur le robot

### 1. Copier le script

```bash
# Depuis la racine du dépôt, sur ta machine de développement
scp robotics/nao_brain/naoqi_bridge/nao_ws_bridge.py nao@NAO_IP:/home/nao/nao_ws_bridge.py
```

### 2. Installer les dépendances Python

Sur NAO 2.1.x, `pip` n'est pas disponible et son installation via `get-pip.py` échoue car le robot ne peut pas atteindre PyPI. Comme tous ces paquets sont Python pur, on transfère les wheels depuis la machine de dev.

`websocket-client` dépend de `six` — les deux doivent être installés.

**a) Télécharger les wheels sur ta machine de dev**

```bash
pip download --no-deps websocket-client==0.59.0 six -d /tmp/nao-pkg/
scp /tmp/nao-pkg/*.whl nao@NAO_IP:/tmp/
```

**b) Extraire les paquets sur le NAO**

Un wheel est une archive zip — Python 2.7 peut l'extraire sans pip :

```bash
ssh nao@NAO_IP
python -c "
import zipfile, glob
for whl in glob.glob('/tmp/*.whl'):
    zipfile.ZipFile(whl).extractall('/home/nao/pylib')
    print('extrait: ' + whl)
"
echo 'export PYTHONPATH=/home/nao/pylib:$PYTHONPATH' >> /home/nao/.bashrc
export PYTHONPATH=/home/nao/pylib:$PYTHONPATH
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
export PYTHONPATH=/home/nao/pylib:$PYTHONPATH
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
| `pip command not found` ou `No matching distribution found for pip<21.0` | pip absent / PyPI inaccessible sur OpenNAO 2.1.x | Utiliser l'installation par wheel sans pip — voir section 2 |
| `ImportError: No module named websocket` | wheel non extrait ou PYTHONPATH absent | Vérifier que `/home/nao/pylib` contient `websocket/` et que `PYTHONPATH` inclut `/home/nao/pylib` |
| Le robot ne parle pas | rosbridge non démarré ou topic incorrect | `docker logs ros2-bridge` sur le VPS |
| Reconnexions en boucle | Timeout réseau NAO | Normal — le script reconnecte automatiquement |
