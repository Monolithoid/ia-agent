# Drone statique — programme embarqué

Programme Python 3 qui transforme un petit ordinateur Linux posé dans la pièce
en **oreilles, yeux et voix** du robot ([ADR 004](../../../../docs/decisions/004-drone-statique.md)).
Il ouvre une connexion WebSocket **sortante** vers rosbridge sur le VPS, comme
le NAO ([ADR 002](../../../../docs/decisions/002-nao-websocket-bridge.md)) :
aucun port à ouvrir côté maison.

| Fichier | Rôle |
|---|---|
| `static_drone.py` | Programme principal |
| `direction.py` | Calcul de la direction du son (numpy seul, testable sans matériel) |
| `tests/test_direction.py` | Tests unitaires de `direction.py` |
| `.env.example` | Configuration d'exemple, à copier en `.env` |
| `requirements.txt` | Dépendances Python |
| `static-drone.service` | Service systemd, pour un démarrage automatique plus tard |
| `static-drone-user.service` | Service systemd utilisateur, pour un PC Ubuntu de bureau (accès au son de la session) |
| `tests/test_camera.py` | Tests unitaires du choix de la caméra et de la fréquence audio |
| [`pc-portable-ubuntu.md`](pc-portable-ubuntu.md) | **Installation sur un PC portable Ubuntu** (webcam + micro) |

## Topics

`{id}` vaut `DRONE_ID` (défaut `static_01`).

| Topic | Sens | Type | Contenu |
|---|---|---|---|
| `/drones/{id}/status` | → VPS | `std_msgs/String` | JSON toutes les 30 s : `capabilities`, `mics`, `respeaker`, `aec`, `direction_mode`, `camera_hfov_deg`, `speaking`, `uptime_s` |
| `/drones/{id}/audio` | → VPS | `std_msgs/String` | JSON `{"wav": base64, "azimuth_deg": angle ou null, "direction_source": "respeaker\|loudest_mic\|image\|none"}` |
| `/drones/{id}/audio_out` | ← VPS | `std_msgs/String` | MP3 en base64 (comme `/nao/audio_out`) |
| `/drones/{id}/speaking` | → VPS | `std_msgs/Bool` | `true` au début de la lecture, `false` à la fin |
| `/drones/{id}/image` | → VPS | `std_msgs/String` | JSON `{"jpeg": base64, "width", "height", "reason": "periodic\|request"}` |
| `/drones/{id}/image_request` | ← VPS | `std_msgs/Empty` | Demande une image tout de suite |

Les angles sont en degrés : **0 = axe de la caméra, positif = à droite**,
entre -180 et 180.

## Direction du son

Règle fixe de l'ADR 004, choisie au démarrage selon le matériel :

1. **ReSpeaker USB 4 micros** branché → `respeaker` : moyenne des `DOAANGLE`
   lus pendant la parole, plus `DOA_OFFSET`.
2. Sinon, **au moins 2 micros** déclarés dans `MICS` → `loudest_mic` : angle du
   micro dont l'énergie pendant la parole est la plus forte.
3. Sinon, **webcam** → `image` : x moyen des pixels qui ont bougé pendant la
   parole, `angle = (x / largeur − 0.5) × CAMERA_HFOV`.
4. Sinon → `none` (`azimuth_deg` vaut `null`).

Si la méthode ne donne rien pour une phrase (pas de mouvement, par exemple),
le message part quand même avec `direction_source: "none"`.

## Matériel

- Un **PC Ubuntu** (22.04 ou plus) ou un **Raspberry Pi 5** sous Raspberry Pi
  OS 64 bits (Bookworm).
- Pour entendre, l'un des deux :
  - un **ReSpeaker USB Mic Array v2.0** (4 micros, détection de parole et
    direction intégrées) ;
  - un ou plusieurs **micros USB**. Pour la direction par micro le plus fort,
    il faut que les micros arrivent comme **canaux d'une même entrée** (micro
    stéréo USB, petite interface audio à 2 entrées…).
- Une **webcam USB**.
- Un **haut-parleur** : sur la prise jack du ReSpeaker (annulation d'écho), ou
  sur la sortie audio / un haut-parleur USB.

## Installation pas à pas

Sur un **PC portable Ubuntu** avec webcam et micro, suivre plutôt
[pc-portable-ubuntu.md](pc-portable-ubuntu.md) (choix du micro et de la
webcam, service utilisateur, mise en veille).

### 1. Système

Sur le Pi, installer Raspberry Pi OS 64 bits avec Raspberry Pi Imager et
activer SSH. Puis, sur le Pi ou le PC Ubuntu :

```bash
sudo apt update
sudo apt install -y git python3-venv libportaudio2 libusb-1.0-0 mpg123 alsa-utils
```

### 2. Code

```bash
cd ~
git clone https://github.com/Monolithoid/ia-agent.git
cd ia-agent/robotics/drones/static/onboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Raspberry Pi OS interdit `pip install` hors environnement virtuel : passer par
`.venv` comme ci-dessus.

### 3. Droits sur le matériel

```bash
sudo usermod -aG audio,video,plugdev $USER
```

Pour que le programme lise le ReSpeaker sans `sudo`, ajouter une règle udev
(identifiants USB `2886:0018`, ceux de `tuning.py` de Seeed) :

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="0018", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/60-respeaker.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Se déconnecter puis se reconnecter (ou redémarrer) pour que les groupes
s'appliquent.

### 4. Vérifier le matériel

```bash
lsusb                                   # le ReSpeaker apparaît en 2886:0018
arecord -l                              # entrées audio
aplay -l                                # sorties audio
.venv/bin/python -m sounddevice         # entrées vues par le programme
ls /dev/video*                          # webcam
mpg123 -q un_fichier.mp3                # test du haut-parleur
```

Le ReSpeaker existe avec deux firmwares : 1 canal (audio traité) ou 6 canaux
(canal 0 traité, canaux 1 à 4 bruts, canal 5 lecture). Les deux conviennent :
le programme enregistre le canal 0.

### 5. Configurer

```bash
cp .env.example .env
nano .env
```

À renseigner au minimum :

- `ROSBRIDGE_URL` (ex. `wss://rosbridge.mondomaine.fr`) ou `DOMAIN` ;
- `ROSBRIDGE_TOKEN` : la même valeur que sur le VPS.

Selon le matériel :

- **ReSpeaker** : rien à faire, il est détecté. Si le haut-parleur est sur sa
  prise jack, mettre `RESPEAKER_AEC=1` et diriger la lecture vers lui, par
  exemple `PLAYER_CMD=mpg123 -q -a hw:ArrayUAC10,0 -` (nom de carte donné par
  `aplay -l`).
- **Plusieurs micros** : `MICS=gauche:-60,droite:60` (un `nom:angle` par
  canal, dans l'ordre des canaux) et, si besoin, `AUDIO_INPUT_DEVICE` avec le
  nom ou l'index affiché par `python -m sounddevice`. Si l'entrée refuse
  `SAMPLE_RATE` (16 kHz), sa fréquence par défaut est utilisée.
- **Caméra** : `CAMERA_INDEX` = index, chemin (`/dev/video2`) ou nom partiel
  affiché par `v4l2-ctl --list-devices` ; `CAMERA_HFOV` = champ de vision
  horizontal de la webcam (défaut 66°).

### 6. Lancer à la main

```bash
.venv/bin/python static_drone.py
```

Le journal doit afficher la méthode de direction retenue puis
`Connecté à wss://…`. Parler devant les micros : une ligne
`Audio envoyé : … angle … (…)` apparaît à chaque phrase. `Ctrl+C` pour arrêter.

Pour plus de détails : `LOG_LEVEL=DEBUG .venv/bin/python static_drone.py`.

### 7. Réglages

- **Seuil de volume** (sans ReSpeaker) : lancer avec `LOG_LEVEL=DEBUG`, se
  taire, puis parler, et placer `RMS_THRESHOLD` entre les deux. Trop bas : le
  bruit de fond déclenche l'enregistrement ; trop haut : les phrases sont
  coupées.
- **Orientation** : se placer bien en face de la caméra et parler. L'angle doit
  être proche de 0. Sinon, ajuster `DOA_OFFSET` de l'écart observé. Si l'angle
  augmente quand on va vers la gauche, mettre `DOA_INVERT=1` (ReSpeaker).
- **Écho** : sans annulation d'écho, l'écoute est coupée pendant la lecture et
  `PLAYBACK_TAIL_S` secondes après.

### 8. Démarrage automatique (plus tard)

Sur un PC Ubuntu de bureau, utiliser plutôt le service utilisateur
([pc-portable-ubuntu.md, étape 9](pc-portable-ubuntu.md#9-démarrage-automatique)).

Sur le Pi, adapter `User` et les chemins de `static-drone.service`, puis :

```bash
sudo cp static-drone.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now static-drone
journalctl -u static-drone -f
```

## Tests

Sans matériel, avec numpy seulement :

```bash
python3 -m unittest discover -s tests -v
```

## Limites connues

- Hors connexion, les messages audio et image sont perdus (pas de file
  d'attente) ; la reconnexion est automatique (1 s, puis jusqu'à
  `RECONNECT_MAX_S`).
- La direction par l'image suit le **mouvement**, pas forcément la personne
  qui parle, et reste bornée au champ de la caméra.
- `loudest_mic` ne gère qu'une entrée audio à plusieurs canaux, pas plusieurs
  micros USB séparés.
- Côté VPS, `body-gateway` ne connaît pas encore le corps `static` (voir les
  conséquences de l'ADR 004).
