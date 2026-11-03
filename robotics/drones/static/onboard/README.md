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
| `wakeword.py` | Mot de réveil local avec openWakeWord (`WAKE_MODE=local`) |
| [`models/`](models/README.md) | Modèle « ok nao » : où le mettre, comment l'entraîner (notebook Colab) |
| `tests/test_direction.py` | Tests unitaires de `direction.py` |
| `.env.example` | Configuration d'exemple, à copier en `.env` |
| `requirements.txt` | Dépendances Python |
| `requirements-wakeword.txt` | openWakeWord, à installer avec `--no-deps` |
| `static-drone.service` | Service systemd, pour un démarrage automatique plus tard |
| `static-drone-user.service` | Service systemd utilisateur, pour un PC Ubuntu de bureau (accès au son de la session) |
| `tests/test_camera.py` | Tests unitaires du choix de la caméra et de la fréquence audio |
| `tests/test_wakeword.py` | Tests unitaires du détecteur de mot de réveil (sans openWakeWord) |
| [`pc-portable-ubuntu.md`](pc-portable-ubuntu.md) | **Installation sur un PC portable Ubuntu** (webcam + micro) |
| `setup-ubuntu.sh` | Installation automatique sur Ubuntu (idempotente), voir [pc-portable-ubuntu.md](pc-portable-ubuntu.md#installation-automatique) |
| `setup-lib.sh` | Fonctions communes aux scripts d'installation |
| `nao-relay/` | Relais du NAO : le NAO passe par le drone pour joindre le VPS ([installation](nao-relay/README.md), [ADR 006](../../../../docs/decisions/006-nao-via-drone-statique.md)) |

## Topics

`{id}` vaut `DRONE_ID` (défaut `static_01`).

| Topic | Sens | Type | Contenu |
|---|---|---|---|
| `/drones/{id}/status` | → VPS | `std_msgs/String` | JSON toutes les 30 s : `capabilities`, `mics`, `respeaker`, `aec`, `direction_mode`, `wake_mode`, `camera_hfov_deg`, `speaking`, `uptime_s` |
| `/drones/{id}/audio` | → VPS | `std_msgs/String` | JSON `{"wav": base64, "azimuth_deg": angle ou null, "direction_source": "respeaker\|loudest_mic\|image\|none"}`, plus `"wake": "local"` et `"wake_score"` pour la phrase qui suit un mot de réveil détecté sur le drone |
| `/drones/{id}/wake` | → VPS | `std_msgs/String` | `WAKE_MODE=local` : JSON `{"model", "score", "threshold"}` dès que « ok nao » est entendu |
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
Le script `setup-ubuntu.sh` fait cette installation automatiquement.

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
.venv/bin/pip install --no-deps -r requirements-wakeword.txt
```

openWakeWord s'installe sans ses dépendances (déjà dans `requirements.txt`) :
sinon pip réclame `tflite-runtime`, introuvable pour Python 3.12 (Ubuntu
24.04), et se rabat sur une vieille version.

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

### « Ok nao » : sur le drone ou sur le VPS

`WAKE_MODE` dans `.env` choisit où le mot de réveil est reconnu.

**`WAKE_MODE=local` (recommandé)** : comme une enceinte connectée, le drone
écoute en continu et reconnaît « ok nao » lui-même avec
[openWakeWord](https://github.com/dscripka/openWakeWord). Il garde en
mémoire les dernières `WAKE_PRE_ROLL_S` secondes (0,5 s) : au mot de réveil,
l'enregistrement part de là, pour ne pas couper « ok nao quelle heure… » dit
d'une traite. Il attend ensuite le début de la phrase (jusqu'à
`WAKE_LISTEN_S`, 5 s) et s'arrête après `SILENCE_S` de silence. Seule cette
phrase part au VPS, marquée `"wake": "local"` : body-gateway ne la refiltre
pas. Plus rapide (Whisper ne transcrit que les questions) et plus fiable
(Whisper n'a plus à reconnaître un « ok nao » isolé).

- Modèle : `WAKE_MODEL=ok_nao` charge `models/ok_nao.onnx`, à entraîner une
  fois ([models/README.md](models/README.md), notebook Colab). Pour essayer
  avant : `WAKE_MODEL=hey_jarvis` et dire « hey Jarvis ».
- Seuil : `WAKE_THRESHOLD` (0,5). Avec `LOG_LEVEL=DEBUG`, le journal affiche
  le score max de chaque seconde (`Score « ok_nao » max sur 1 s : 0.83`) :
  le placer entre le score du bruit de la pièce et celui de « ok nao ».
- Charge : quelques millisecondes de calcul toutes les 80 ms, sur PC comme
  sur Raspberry Pi 5.

**`WAKE_MODE=server` (défaut, sans modèle)** : le drone envoie chaque phrase
au VPS, Whisper la transcrit, puis body-gateway cherche « ok nao » dedans. Le
plus fiable est alors de tout dire d'une traite : « ok nao, quelle heure
est-il ? ». En deux fois, attendre `j'écoute` avant la question. Si Whisper
écrit le mot de réveil autrement (« Ok, now », …), ajouter cette orthographe
à `WAKE_ALIASES` côté VPS (body-gateway).

La régie montre ce qui s'est passé :

| Dans la régie | Sens |
|---|---|
| `mot de réveil, j'écoute : ok nao sur le drone (ok_nao, score 0.83)` | `local` : le drone a entendu « ok nao », la phrase suivante part au LLM |
| `mot de réveil, j'écoute : …` | `server` : « ok nao » seul, la phrase suivante (dans les 10 s) part au LLM |
| carte voix avec la question | la question est partie au LLM |
| `ignoré (sans mot de réveil) : …` | `server` : Whisper n'a pas reconnu « ok nao » : le texte dit ce qu'il a compris |
| rien | le drone n'a rien envoyé : voir son journal (`Trop court, ignoré`, `Rien entendu après le mot de réveil`, score sous le seuil) |

Le résultat arrive après `SILENCE_S` de silence plus le temps de Whisper
sur le VPS (affiché dans le journal de static-driver : `STT [static_01] (1.8 s)`).

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

### 9. Relais du NAO (ADR 006)

Le NAO ne sait pas faire de TLS : il envoie ses requêtes au drone, qui les
relaie vers le VPS en `wss://` et ajoute le jeton (relais Caddy). Voir
[nao-relay/README.md](nao-relay/README.md).

## Tests

Sans matériel, avec numpy seulement (`test_wakeword.py` demande aussi scipy) :

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
