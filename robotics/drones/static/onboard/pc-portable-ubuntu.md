# Drone statique sur un PC portable Ubuntu

Première version du drone statique : un **PC portable sous Ubuntu** (22.04 ou
24.04, bureau) avec une **webcam** et un **micro**, en attendant le Raspberry
Pi de l'[ADR 004](../../../../docs/decisions/004-drone-statique.md). Le
programme est le même ([README](README.md)) ; cette page ne liste que ce qui
change sur un PC portable.

Ce qui diffère d'un Raspberry Pi :

| Sujet | Sur le PC portable |
|---|---|
| Son | Ubuntu passe par PipeWire (24.04) ou PulseAudio (22.04). Le plus simple est de choisir micro et haut-parleur dans **Paramètres > Son** et de laisser le programme utiliser l'entrée et la sortie par défaut. |
| Caméra | La caméra intégrée prend souvent `/dev/video0` (et `video1`) ; une webcam USB arrive alors en `/dev/video2`. `CAMERA_INDEX` accepte un nom (`C920`) pour ne pas dépendre de l'ordre. |
| Direction du son | Un micro (ou le micro intégré) → direction par **l'image** (mouvement dans la webcam). Le micro stéréo intégré n'est pas utilisable en `loudest_mic` : ses deux capsules sont trop proches. |
| Écho | Haut-parleur et micro sont collés : l'écoute est coupée pendant que le drone parle (comportement par défaut, sans ReSpeaker). |
| Démarrage automatique | Service systemd **utilisateur** (`static-drone-user.service`), pour avoir accès au son de la session. Le service système `static-drone.service` n'y a pas accès. |
| Alimentation | Empêcher la mise en veille, y compris capot fermé. |

## 1. Paquets

```bash
sudo apt update
sudo apt install -y git python3-venv libportaudio2 libusb-1.0-0 mpg123 \
  alsa-utils v4l-utils pulseaudio-utils
```

`pulseaudio-utils` fournit `pactl`, qui fonctionne aussi avec PipeWire.

## 2. Code

```bash
cd ~
git clone https://github.com/Monolithoid/ia-agent.git
cd ia-agent/robotics/drones/static/onboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Ubuntu 24.04 interdit `pip install` hors environnement virtuel : toujours
passer par `.venv`.

## 3. Droits

```bash
sudo usermod -aG audio,video $USER
```

Se déconnecter puis se reconnecter. Les groupes servent quand le service tourne
sans session ouverte (étape 9).

## 4. Choisir le micro et le haut-parleur

Brancher la webcam et le micro, puis dans **Paramètres > Son** choisir :

- **Entrée** : le micro USB (ou celui de la webcam, ou le micro intégré) ;
- **Sortie** : le haut-parleur voulu.

Même chose en ligne de commande :

```bash
pactl list short sources                # entrées (ignorer les « .monitor »)
pactl set-default-source <nom>          # ex. alsa_input.usb-…-00.mono-fallback
pactl list short sinks                  # sorties
pactl set-default-sink <nom>
```

Laisser `AUDIO_INPUT_DEVICE` vide dans `.env` : le programme ouvre l'entrée
`default`, qui suit ce réglage, et PipeWire convertit en 16 kHz.

Vérifier :

```bash
.venv/bin/python -m sounddevice         # « default » et « pipewire »/« pulse » apparaissent
arecord -d 3 -f S16_LE -r 16000 /tmp/t.wav && aplay /tmp/t.wav
mpg123 -q un_fichier.mp3                # haut-parleur
```

Si l'enregistrement est muet : `alsamixer`, F6 pour choisir la carte, F4 pour
les entrées, monter le niveau et activer la capture (barre d'espace).

> Ouvrir directement un micro USB (`AUDIO_INPUT_DEVICE=hw:…` ou son index
> « hw ») marche aussi mais court-circuite PipeWire : le micro peut être dit
> « occupé », et il refuse souvent 16 kHz. Le programme se rabat alors sur la
> fréquence native du micro (journal : `16000 Hz refusé … : 48000 Hz utilisé`).

## 5. Choisir la webcam

```bash
v4l2-ctl --list-devices
```

Exemple sur un portable avec webcam USB :

```
Integrated Camera: Integrated C (usb-0000:00:14.0-8):
        /dev/video0
        /dev/video1
HD Pro Webcam C920 (usb-0000:00:14.0-1):
        /dev/video2
        /dev/video3
```

Chaque caméra a deux nœuds ; le premier donne l'image. Dans `.env`, au choix :

```bash
CAMERA_INDEX=C920                       # nom partiel (recommandé)
CAMERA_INDEX=2                          # index
CAMERA_INDEX=/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920_XXXX-video-index0
```

Au lancement, le journal affiche `Caméra : HD Pro Webcam C920`. Mettre
`CAMERA_HFOV` au champ de vision horizontal de la webcam (fiche technique ;
70° environ pour une C920, 60 à 80° pour une caméra intégrée).

## 6. Configurer

```bash
cp .env.example .env
nano .env
```

Pour un PC portable avec un micro et une webcam :

```bash
ROSBRIDGE_URL=wss://rosbridge.mondomaine.fr
ROSBRIDGE_TOKEN=…                       # même valeur que sur le VPS
RESPEAKER=0                             # pas de ReSpeaker
MICS=                                   # un seul micro → direction par l'image
CAMERA_INDEX=C920
CAMERA_HFOV=70
```

Le reste garde ses valeurs par défaut.

## 7. Lancer à la main

```bash
.venv/bin/python static_drone.py
```

Le journal doit afficher, dans l'ordre :

```
Caméra : HD Pro Webcam C920
Direction : image (0 micros déclarés, caméra : oui)
Entrée audio : default (1 canaux, 16000 Hz)
Connecté à wss://…
```

Puis, à chaque phrase : `Audio envoyé : … angle … (image)`.

## 8. Réglages

- **Seuil de volume** : `LOG_LEVEL=DEBUG .venv/bin/python static_drone.py`, se
  taire puis parler, et placer `RMS_THRESHOLD` entre les deux valeurs. Le
  ventilateur du portable compte dans le bruit de fond : un micro USB posé un
  peu plus loin aide.
- **Écho** : si la fin des phrases du drone est encore entendue, augmenter
  `PLAYBACK_TAIL_S` (ex. `0.6`).
- **Orientation** : se placer face à la webcam et bouger en parlant : l'angle
  doit être proche de 0, positif à droite de l'image.

## 9. Démarrage automatique

Le service tourne en **utilisateur** pour accéder au son de la session :

```bash
mkdir -p ~/.config/systemd/user
cp static-drone-user.service ~/.config/systemd/user/static-drone.service
systemctl --user daemon-reload
systemctl --user enable --now static-drone
sudo loginctl enable-linger $USER       # démarre au boot, sans ouvrir de session
journalctl --user -u static-drone -f
```

Si le dépôt n'est pas dans `~/ia-agent`, adapter les deux chemins du fichier.

Sans session graphique ouverte, PipeWire démarre quand même grâce à
`enable-linger`, mais garde ses propres réglages : choisir micro et sortie une
fois avec `pactl set-default-source` / `set-default-sink` (étape 4), ils sont
mémorisés.

## 10. Le portable ne doit pas dormir

```bash
# Pas de veille automatique
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# Capot fermé : ne rien faire (même sur secteur ou avec un écran externe)
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/; s/^#\?HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/; s/^#\?HandleLidSwitchDocked=.*/HandleLidSwitchDocked=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind   # ferme la session graphique : à faire en dernier
```

Avec le capot fermé, le **micro et la caméra intégrés** sont souvent coupés ou
masqués : utiliser alors une webcam et un micro USB.

Garder le portable **sur secteur**. Pour annuler : `sudo systemctl unmask
sleep.target suspend.target hibernate.target hybrid-sleep.target` et remettre
`HandleLidSwitch=suspend`.

## Dépannage

| Symptôme | Piste |
|---|---|
| `Caméra 'C920' introuvable` | `v4l2-ctl --list-devices` : reprendre un mot du nom affiché. Vérifier le groupe `video` (`groups`). |
| `Caméra 0 introuvable` alors que `ls /dev/video*` en montre | Caméra déjà utilisée (navigateur, Cheese, visio) : la fermer. |
| `Entrée audio … introuvable` | `AUDIO_INPUT_DEVICE` ne correspond à aucun nom de `python -m sounddevice` : le vider pour utiliser l'entrée par défaut. |
| Rien n'est envoyé quand on parle | Micro muet ou mauvais micro par défaut (étape 4), ou `RMS_THRESHOLD` trop haut (étape 8). |
| Pas de son quand le drone parle | `mpg123 -q un_fichier.mp3` dans la même session ; vérifier la sortie par défaut (`pactl get-default-sink`). En service, voir l'étape 9. |
| Le service marche à la main mais pas au démarrage | `journalctl --user -u static-drone` ; vérifier `loginctl show-user $USER -p Linger` (doit valoir `yes`). |
