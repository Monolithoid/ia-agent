#!/usr/bin/env python3
"""
static_drone.py — Programme embarqué du drone statique (ADR 004).

Tourne sur un PC Ubuntu ou un Raspberry Pi 5 (Raspberry Pi OS 64 bits) placé
dans la pièce. Il ouvre une connexion WebSocket SORTANTE vers rosbridge sur le
VPS (même principe que le NAO, ADR 002) et assure :

    - signal de vie        → /drones/{id}/status        (toutes les 30 s)
    - écoute               → /drones/{id}/audio         (WAV base64 + angle)
    - parole               ← /drones/{id}/audio_out     (MP3 base64)
                           → /drones/{id}/speaking      (true / false)
    - ce qui a été compris ← /drones/{id}/heard         (bip si « ok nao » entendu)
    - image                → /drones/{id}/image         (JPEG base64)
                           ← /drones/{id}/image_request

Configuration par variables d'environnement (voir .env.example). Un fichier
.env placé à côté du script est lu au démarrage, sans écraser les variables
déjà définies.

Lancement :
    python3 static_drone.py
"""

import base64
import collections
import io
import json
import logging
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
import wave

import numpy as np

import direction

log = logging.getLogger("static_drone")

HERE = os.path.dirname(os.path.abspath(__file__))

# Identifiants USB du ReSpeaker USB Mic Array v2.0 (tuning.py de Seeed,
# github.com/respeaker/usb_4_mic_array).
RESPEAKER_VID = 0x2886
RESPEAKER_PID = 0x0018


# ======================================================================
# Configuration
# ======================================================================

def load_dotenv(path):
    """Lit un fichier KEY=VALUE simple, sans écraser l'environnement existant."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


def _env(name, default=None):
    value = os.environ.get(name, "").strip()
    return value if value else default


def _env_float(name, default):
    return float(_env(name, default))


def _env_int(name, default):
    return int(_env(name, default))


def _env_bool(name, default=False):
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "oui", "on")


class Config:
    def __init__(self):
        self.drone_id = _env("DRONE_ID", "static_01")
        domain = _env("DOMAIN")
        self.rosbridge_url = _env("ROSBRIDGE_URL",
                                  "wss://rosbridge.{}".format(domain) if domain else None)
        self.rosbridge_token = _env("ROSBRIDGE_TOKEN")

        # Audio
        self.audio_input_device = _env("AUDIO_INPUT_DEVICE")    # nom (partiel) ou index
        self.sample_rate = _env_int("SAMPLE_RATE", 16000)
        self.record_channel = _env_int("RECORD_CHANNEL", 0)
        self.mics = direction.parse_mics(_env("MICS", ""))
        self.rms_threshold = _env_float("RMS_THRESHOLD", 500)    # sur des échantillons int16
        self.silence_s = _env_float("SILENCE_S", 0.8)
        self.min_speech_s = _env_float("MIN_SPEECH_S", 0.2)
        self.max_record_s = _env_float("MAX_RECORD_S", 15)
        self.pre_roll_s = _env_float("PRE_ROLL_S", 0.3)
        self.block_ms = _env_int("BLOCK_MS", 30)

        # ReSpeaker
        self.respeaker = _env("RESPEAKER", "auto").lower()       # auto | 0 | 1
        self.respeaker_aec = _env_bool("RESPEAKER_AEC", False)
        self.doa_offset = _env_float("DOA_OFFSET", 0)
        self.doa_invert = _env_bool("DOA_INVERT", False)

        # Haut-parleur
        self.player_cmd = _env("PLAYER_CMD", "mpg123 -q -")
        self.playback_tail_s = _env_float("PLAYBACK_TAIL_S", 0.3)
        # Bip quand le VPS a reconnu le mot de réveil (vide = pas de bip)
        self.beep_cmd = _env("BEEP_CMD", "aplay -q -")

        # Caméra
        self.camera_index = _env("CAMERA_INDEX", "0")
        self.camera_hfov = _env_float("CAMERA_HFOV", 66)
        self.camera_width = _env_int("CAMERA_WIDTH", 320)
        self.camera_height = _env_int("CAMERA_HEIGHT", 240)
        self.camera_fps = _env_float("CAMERA_FPS", 5)
        self.jpeg_quality = _env_int("JPEG_QUALITY", 70)
        self.motion_threshold = _env_int("MOTION_THRESHOLD", 25)
        self.image_interval = _env_float("IMAGE_INTERVAL", 10)  # 0 = jamais en périodique

        # Intervalles réseau
        self.status_interval = _env_float("STATUS_INTERVAL", 30)
        self.reconnect_max_s = _env_float("RECONNECT_MAX_S", 30)

    def topic(self, name):
        return "/drones/{}/{}".format(self.drone_id, name)


# ======================================================================
# Client rosbridge (WebSocket sortant, reconnexion automatique)
# ======================================================================

class Rosbridge:
    """Client rosbridge JSON : advertise / publish / subscribe.

    Les annonces et abonnements déclarés sont rejoués à chaque reconnexion.
    """

    def __init__(self, url, token, reconnect_max_s=30):
        self.url = url
        self.token = token
        self.reconnect_max_s = reconnect_max_s
        self._advertised = {}       # topic → type
        self._subscribed = {}       # topic → (type, callback)
        self._ws = None
        self._connected = threading.Event()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

    @property
    def connected(self):
        return self._connected.is_set()

    def advertise(self, topic, msg_type):
        self._advertised[topic] = msg_type

    def subscribe(self, topic, msg_type, callback):
        self._subscribed[topic] = (msg_type, callback)

    def publish(self, topic, msg):
        """Publie un message ; renvoie False (message perdu) si déconnecté."""
        return self._send({"op": "publish", "topic": topic, "msg": msg})

    def _send(self, payload):
        ws = self._ws
        if ws is None or not self.connected:
            log.debug("Hors ligne, message ignoré : %s", payload.get("topic"))
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(payload))
            return True
        except Exception as exc:
            log.warning("Envoi impossible (%s) : %s", payload.get("topic"), exc)
            return False

    def _on_open(self, ws):
        self._connected.set()
        for topic, msg_type in self._advertised.items():
            self._send({"op": "advertise", "topic": topic, "type": msg_type})
        for topic, (msg_type, _) in self._subscribed.items():
            self._send({"op": "subscribe", "topic": topic, "type": msg_type})
        log.info("Connecté à %s", self.url)

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("op") != "publish":
            return
        entry = self._subscribed.get(msg.get("topic"))
        if entry:
            try:
                entry[1](msg.get("msg", {}))
            except Exception:
                log.exception("Erreur en traitant %s", msg.get("topic"))

    def _on_error(self, ws, error):
        import websocket
        if isinstance(error, websocket.WebSocketConnectionClosedException):
            return      # fermeture normale, gérée par la boucle de reconnexion
        log.warning("Erreur WebSocket : %s", error)

    def _on_close(self, ws, *args):
        self._connected.clear()

    def run_forever(self):
        """Boucle de connexion avec backoff exponentiel (1 s → reconnect_max_s)."""
        import websocket   # websocket-client

        header = ["Authorization: Bearer {}".format(self.token)] if self.token else []
        delay = 1.0
        while not self._stop.is_set():
            self._ws = websocket.WebSocketApp(
                self.url, header=header,
                on_open=self._on_open, on_message=self._on_message,
                on_error=self._on_error, on_close=self._on_close)
            started = time.monotonic()
            self._ws.run_forever(ping_interval=20, ping_timeout=10)
            self._connected.clear()
            if self._stop.is_set():
                break
            # Une connexion restée ouverte un moment remet le délai à zéro.
            if time.monotonic() - started > 60:
                delay = 1.0
            log.info("Déconnecté, nouvelle tentative dans %.0f s", delay)
            self._stop.wait(delay)
            delay = min(delay * 2, self.reconnect_max_s)

    def stop(self):
        self._stop.set()
        if self._ws:
            self._ws.close()


# ======================================================================
# ReSpeaker USB Mic Array v2.0 (paramètres DSP via pyusb)
# ======================================================================

class ReSpeaker:
    """Lecture de DOAANGLE et SPEECHDETECTED, comme Tuning.read() de Seeed.

    Paramètres repris de tuning.py (github.com/respeaker/usb_4_mic_array) :
        DOAANGLE       : id 21, offset 0,  int, 0..359, lecture seule
        SPEECHDETECTED : id 19, offset 22, int, 0/1,    lecture seule
    """

    PARAMETERS = {
        "DOAANGLE": (21, 0),
        "SPEECHDETECTED": (19, 22),
    }
    TIMEOUT = 100000

    def __init__(self, dev):
        self.dev = dev
        self._lock = threading.Lock()

    @classmethod
    def find(cls):
        try:
            import usb.core
        except ImportError:
            log.info("pyusb absent : pas de ReSpeaker")
            return None
        try:
            dev = usb.core.find(idVendor=RESPEAKER_VID, idProduct=RESPEAKER_PID)
        except Exception as exc:    # pas de backend libusb, etc.
            log.warning("Recherche USB impossible : %s", exc)
            return None
        return cls(dev) if dev else None

    def _read_int(self, name):
        import usb.util
        param_id, offset = self.PARAMETERS[name]
        cmd = 0x80 | offset | 0x40      # lecture d'un entier
        with self._lock:
            response = self.dev.ctrl_transfer(
                usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                0, cmd, param_id, 8, self.TIMEOUT)
        return int(np.frombuffer(bytes(response), dtype="<i4")[0])

    def doa(self):
        return self._read_int("DOAANGLE")

    def speech_detected(self):
        return self._read_int("SPEECHDETECTED") == 1


# ======================================================================
# Caméra : dernière image + mouvement
# ======================================================================

class Camera:
    def __init__(self, cfg):
        self.cfg = cfg
        self._cap = None
        self._lock = threading.Lock()
        self._frame = None          # dernière image BGR
        self._prev_gray = None
        self._motion = None         # (horodatage, angle)

    def open(self):
        try:
            import cv2
        except ImportError:
            log.warning("OpenCV absent : pas de caméra")
            return False
        wanted = self.cfg.camera_index
        index = find_camera(wanted)
        if index is None:
            log.warning("Caméra %r introuvable (v4l2-ctl --list-devices)", wanted)
            return False
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            log.warning("Caméra %s introuvable", index)
            return False
        log.info("Caméra : %s", camera_name(index) or index)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.camera_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.camera_height)
        self._cap = cap
        return True

    @property
    def available(self):
        return self._cap is not None

    def run(self, stop):
        import cv2
        period = 1.0 / max(self.cfg.camera_fps, 0.1)
        while not stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                stop.wait(1.0)
                continue
            if frame.shape[1] != self.cfg.camera_width:
                frame = cv2.resize(frame, (self.cfg.camera_width, self.cfg.camera_height))
            gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0)
            angle = None
            if self._prev_gray is not None:
                angle = direction.motion_angle(self._prev_gray, gray, self.cfg.camera_hfov,
                                               threshold=self.cfg.motion_threshold)
            with self._lock:
                self._frame = frame
                if angle is not None:
                    self._motion = (time.monotonic(), angle)
            self._prev_gray = gray
            stop.wait(period)
        self._cap.release()

    def last_motion_since(self, t0):
        """Dernier angle de mouvement observé après l'instant t0 (monotonic)."""
        with self._lock:
            if self._motion and self._motion[0] >= t0:
                return self._motion[1]
        return None

    def jpeg(self):
        import cv2
        with self._lock:
            frame = self._frame
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        return (buf.tobytes(), frame.shape[1], frame.shape[0]) if ok else None


def _video_index(name):
    """'video2' → 2, sinon None."""
    return int(name[5:]) if name.startswith("video") and name[5:].isdigit() else None


def camera_name(index, sysfs="/sys/class/video4linux"):
    """Nom de la caméra /dev/video{index} donné par le noyau, ou None."""
    try:
        with open(os.path.join(sysfs, "video{}".format(index), "name"), encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, TypeError):
        return None


def find_camera(wanted, sysfs="/sys/class/video4linux"):
    """Caméra à ouvrir d'après CAMERA_INDEX.

    - « 2 » → index 2 ;
    - un chemin (/dev/video2, /dev/v4l/by-id/…) → index du /dev/videoN visé ;
    - sinon un nom (partiel, sans casse), cherché dans les noms des caméras du
      noyau (ceux de v4l2-ctl --list-devices) : premier nœud qui correspond.
      Une webcam crée souvent deux nœuds (image + métadonnées) : le premier est
      celui de l'image.
    Renvoie un int, un chemin à passer tel quel à OpenCV, ou None.
    """
    wanted = (wanted or "0").strip()
    if wanted.isdigit():
        return int(wanted)
    if wanted.startswith("/"):
        index = _video_index(os.path.basename(os.path.realpath(wanted)))
        if index is not None:
            return index
        return wanted if os.path.exists(wanted) else None
    try:
        nodes = sorted((n for n in os.listdir(sysfs) if _video_index(n) is not None),
                       key=_video_index)
    except OSError:
        return None
    for node in nodes:
        name = camera_name(_video_index(node), sysfs)
        if name and wanted.lower() in name.lower():
            return _video_index(node)
    return None


# ======================================================================
# Outils audio
# ======================================================================

def pcm_to_wav(pcm, rate):
    """int16 mono → octets WAV."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    return buf.getvalue()


def beep_wav(freqs, rate=16000, note_s=0.08, volume=0.3):
    """Petit bip WAV, une note par fréquence (Hz), avec fondu pour ne pas claquer."""
    n = int(rate * note_s)
    t = np.arange(n) / float(rate)
    fade = np.minimum(1.0, np.minimum(t, note_s - t) / 0.01)
    notes = [np.sin(2 * np.pi * f * t) * fade for f in freqs]
    return pcm_to_wav(np.concatenate(notes) * volume * 32767, rate)


# Deux notes montantes : « je t'écoute » ; une note : question partie au LLM.
BEEP_LISTENING = (660, 880)
BEEP_ACCEPTED = (880,)


def find_input_device(sd, wanted):
    """Index de l'entrée audio : AUDIO_INPUT_DEVICE, sinon ReSpeaker, sinon défaut."""
    if wanted is not None and wanted.isdigit():
        return int(wanted)
    needle = (wanted or "respeaker").lower()
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and needle in dev["name"].lower():
            return i
    if wanted:
        raise RuntimeError("Entrée audio {!r} introuvable".format(wanted))
    return None     # périphérique par défaut


def choose_sample_rate(sd, device, channels, wanted, info):
    """SAMPLE_RATE si l'entrée l'accepte, sinon sa fréquence par défaut.

    Les micros USB ouverts en direct (hw:…) refusent souvent 16 kHz ; le WAV
    envoyé indique sa fréquence, Whisper s'adapte.
    """
    try:
        sd.check_input_settings(device=device, channels=channels, samplerate=wanted,
                                dtype="int16")
        return wanted
    except Exception as exc:
        fallback = int(info["default_samplerate"])
        log.warning("%d Hz refusé par %r (%s) : %d Hz utilisé", wanted, info["name"],
                    exc, fallback)
        return fallback


# ======================================================================
# Drone statique
# ======================================================================

class StaticDrone:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop = threading.Event()
        self.speaking = threading.Event()
        self.started = time.monotonic()
        self.bridge = Rosbridge(cfg.rosbridge_url, cfg.rosbridge_token, cfg.reconnect_max_s)
        self.respeaker = None
        self.camera = Camera(cfg)
        self.channels = 1
        self.source = direction.SOURCE_NONE
        self.sample_rate = cfg.sample_rate
        self._audio_q = queue.Queue(maxsize=200)
        self._play_lock = threading.Lock()
        self._rms_peak = 0.0
        self._rms_logged = 0.0

    # ------------------------------------------------------------------
    # Matériel
    # ------------------------------------------------------------------

    def detect_hardware(self):
        if self.cfg.respeaker in ("auto", "1"):
            self.respeaker = ReSpeaker.find()
            if self.respeaker is None and self.cfg.respeaker == "1":
                raise RuntimeError("RESPEAKER=1 mais aucun ReSpeaker USB trouvé")
        if self.respeaker:
            log.info("ReSpeaker USB détecté")
        self.camera.open()
        self.source = direction.choose_direction_source(
            self.respeaker is not None, len(self.cfg.mics), self.camera.available)
        log.info("Direction : %s (%d micros déclarés, caméra : %s)", self.source,
                 len(self.cfg.mics), "oui" if self.camera.available else "non")

    def mute_while_speaking(self):
        """Coupe l'écoute pendant la lecture, sauf ReSpeaker avec annulation d'écho."""
        return not (self.respeaker is not None and self.cfg.respeaker_aec)

    # ------------------------------------------------------------------
    # Écoute
    # ------------------------------------------------------------------

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            log.debug("Audio : %s", status)
        try:
            self._audio_q.put_nowait(indata.copy())
        except queue.Full:
            pass

    def listen_loop(self):
        import sounddevice as sd

        device = find_input_device(sd, self.cfg.audio_input_device)
        info = sd.query_devices(device, "input")
        max_ch = int(info["max_input_channels"])
        if self.respeaker is not None:
            # Firmware 6 canaux : canal 0 = audio traité, 1-4 = micros bruts.
            # Firmware 1 canal : canal 0 = audio traité.
            self.channels = min(max_ch, 6)
        else:
            self.channels = min(max_ch, max(self.cfg.record_channel + 1, len(self.cfg.mics), 1))
        if self.source == direction.SOURCE_LOUDEST_MIC and self.channels < len(self.cfg.mics):
            raise RuntimeError("MICS déclare {} micros mais {!r} n'a que {} canaux".format(
                len(self.cfg.mics), info["name"], self.channels))
        rate = self.sample_rate = choose_sample_rate(sd, device, self.channels,
                                                     self.cfg.sample_rate, info)
        log.info("Entrée audio : %s (%d canaux, %d Hz)", info["name"], self.channels, rate)

        block = int(rate * self.cfg.block_ms / 1000)
        pre_roll = collections.deque(maxlen=max(1, int(self.cfg.pre_roll_s * 1000 / self.cfg.block_ms)))
        recording = None            # liste de blocs mono pendant un enregistrement
        levels = None               # énergie cumulée par micro
        doas = []
        # Horloge audio (somme des blocs reçus) : insensible aux retards de traitement.
        now = t_start = t_voice = 0.0
        wall_start = 0.0
        voiced_s = 0.0

        with sd.InputStream(device=device, channels=self.channels, samplerate=rate,
                            blocksize=block, dtype="int16", callback=self._audio_callback):
            while not self.stop.is_set():
                try:
                    frames = self._audio_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                now += frames.shape[0] / float(rate)

                if self.speaking.is_set() and self.mute_while_speaking():
                    recording = None
                    pre_roll.clear()
                    continue

                mono = frames[:, min(self.cfg.record_channel, frames.shape[1] - 1)]
                speech = self._is_speech(mono)

                if recording is None:
                    pre_roll.append(mono)
                    if not speech:
                        continue
                    recording = list(pre_roll)
                    pre_roll.clear()
                    levels = np.zeros(max(len(self.cfg.mics), 1))
                    doas = []
                    t_start = t_voice = now
                    wall_start = time.monotonic()
                    voiced_s = 0.0
                    log.info("Parole détectée")
                else:
                    recording.append(mono)

                if speech:
                    t_voice = now
                    voiced_s += frames.shape[0] / float(rate)
                    self._sample_direction(frames, levels, doas)

                if now - t_voice >= self.cfg.silence_s or now - t_start >= self.cfg.max_record_s:
                    if voiced_s >= self.cfg.min_speech_s:
                        found = self._final_direction(levels, doas, wall_start)
                        self.publish_audio(np.concatenate(recording), found)
                    else:
                        log.info("Trop court, ignoré (%.2f s)", voiced_s)
                    recording = None

    def _is_speech(self, mono):
        if self.respeaker is not None:
            try:
                return self.respeaker.speech_detected()
            except Exception as exc:
                log.warning("Lecture SPEECHDETECTED impossible, seuil RMS utilisé : %s", exc)
        level = float(direction.channel_rms(mono)[0])
        # En DEBUG, le niveau max de chaque seconde aide à régler RMS_THRESHOLD.
        self._rms_peak = max(self._rms_peak, level)
        if time.monotonic() - self._rms_logged >= 1.0:
            log.debug("RMS max sur 1 s : %.0f (seuil %.0f)", self._rms_peak, self.cfg.rms_threshold)
            self._rms_peak, self._rms_logged = 0.0, time.monotonic()
        return level >= self.cfg.rms_threshold

    def _sample_direction(self, frames, levels, doas):
        """Collecte de quoi calculer la direction, bloc par bloc pendant la parole."""
        if self.source == direction.SOURCE_RESPEAKER:
            try:
                doas.append(self.respeaker.doa())
            except Exception as exc:
                log.debug("Lecture DOAANGLE impossible : %s", exc)
        elif self.source == direction.SOURCE_LOUDEST_MIC:
            n = len(self.cfg.mics)
            levels += direction.channel_rms(frames[:, :n]) ** 2

    def _final_direction(self, levels, doas, wall_start):
        """Renvoie (angle ou None, direction_source) selon la règle de l'ADR 004."""
        if self.source == direction.SOURCE_RESPEAKER:
            mean = direction.circular_mean_deg(doas)
            if mean is not None:
                return direction.respeaker_angle(mean, self.cfg.doa_offset,
                                                 self.cfg.doa_invert), self.source
        elif self.source == direction.SOURCE_LOUDEST_MIC:
            angle = direction.loudest_mic(levels, [a for _, a in self.cfg.mics])
            if angle is not None:
                return direction.normalize_angle(angle + self.cfg.doa_offset), self.source
        elif self.source == direction.SOURCE_IMAGE:
            angle = self.camera.last_motion_since(wall_start - 1.0)
            if angle is not None:
                return round(angle, 1), self.source
        return None, direction.SOURCE_NONE

    def publish_audio(self, pcm, angle_source):
        angle, source = angle_source
        wav = pcm_to_wav(pcm, self.sample_rate)
        payload = {
            "wav": base64.b64encode(wav).decode("ascii"),
            "azimuth_deg": None if angle is None else round(angle, 1),
            "direction_source": source,
        }
        sent = self.bridge.publish(self.cfg.topic("audio"), {"data": json.dumps(payload)})
        log.info("Audio %s : %.1f s, angle %s (%s)", "envoyé" if sent else "perdu (hors ligne)",
                 len(pcm) / float(self.sample_rate), payload["azimuth_deg"], source)

    # ------------------------------------------------------------------
    # Parole
    # ------------------------------------------------------------------

    def on_audio_out(self, msg):
        b64 = msg.get("data", "")
        if b64:
            threading.Thread(target=self.play_mp3, args=(base64.b64decode(b64),),
                             daemon=True).start()

    def play_mp3(self, mp3):
        with self._play_lock:   # une seule lecture à la fois, dans l'ordre d'arrivée
            self.speaking.set()
            self.bridge.publish(self.cfg.topic("speaking"), {"data": True})
            try:
                subprocess.run(shlex.split(self.cfg.player_cmd), input=mp3,
                               stdout=subprocess.DEVNULL, check=False, timeout=120)
            except Exception as exc:
                log.warning("Lecture impossible (%s) : %s", self.cfg.player_cmd, exc)
            finally:
                # Laisse l'écho de la pièce s'éteindre avant de réécouter.
                time.sleep(self.cfg.playback_tail_s)
                self.speaking.clear()
                self.bridge.publish(self.cfg.topic("speaking"), {"data": False})

    def on_heard(self, msg):
        """Retour du VPS : texte transcrit et décision du filtre « ok nao »."""
        try:
            data = json.loads(msg.get("data", ""))
        except ValueError:
            return
        status, text = data.get("status", ""), data.get("text", "")
        if status == "listening":
            log.info("Mot de réveil entendu, j'écoute : %r", text)
            self.beep(BEEP_LISTENING)
        elif status == "accepted":
            log.info("Compris, envoyé au LLM : %r", text)
            self.beep(BEEP_ACCEPTED)
        else:
            log.info("Entendu sans mot de réveil, ignoré : %r", text)

    def beep(self, freqs):
        """Bip court, sans couper l'écoute : la question peut suivre tout de suite.

        Trop court pour dépasser MIN_SPEECH_S s'il est capté par le micro.
        """
        if not self.cfg.beep_cmd:
            return

        def play():
            try:
                subprocess.run(shlex.split(self.cfg.beep_cmd), input=beep_wav(freqs),
                               stdout=subprocess.DEVNULL, check=False, timeout=5)
            except Exception as exc:
                log.warning("Bip impossible (%s) : %s", self.cfg.beep_cmd, exc)

        threading.Thread(target=play, daemon=True).start()

    # ------------------------------------------------------------------
    # Image
    # ------------------------------------------------------------------

    def publish_image(self, reason):
        if not self.camera.available:
            return
        result = self.camera.jpeg()
        if result is None:
            return
        jpeg, width, height = result
        payload = {"jpeg": base64.b64encode(jpeg).decode("ascii"),
                   "width": width, "height": height, "reason": reason}
        self.bridge.publish(self.cfg.topic("image"), {"data": json.dumps(payload)})

    def on_image_request(self, msg):
        threading.Thread(target=self.publish_image, args=("request",), daemon=True).start()

    def image_loop(self):
        while not self.stop.wait(self.cfg.image_interval):
            self.publish_image("periodic")

    # ------------------------------------------------------------------
    # Signal de vie
    # ------------------------------------------------------------------

    def status(self):
        capabilities = ["audio_in", "audio_out"]
        if self.camera.available:
            capabilities.append("image")
        if self.source != direction.SOURCE_NONE:
            capabilities.append("direction")
        if self.respeaker is not None:
            n_mics = 4
        else:
            n_mics = max(len(self.cfg.mics), 1)
        return {
            "drone_id": self.cfg.drone_id,
            "type": "static",
            "capabilities": capabilities,
            "mics": n_mics,
            "respeaker": self.respeaker is not None,
            "aec": self.respeaker is not None and self.cfg.respeaker_aec,
            "direction_mode": self.source,
            "camera_hfov_deg": self.cfg.camera_hfov if self.camera.available else None,
            "speaking": self.speaking.is_set(),
            "uptime_s": int(time.monotonic() - self.started),
        }

    def status_loop(self):
        while not self.stop.is_set():
            if self.bridge.connected:
                self.bridge.publish(self.cfg.topic("status"), {"data": json.dumps(self.status())})
            self.stop.wait(self.cfg.status_interval)

    # ------------------------------------------------------------------
    # Démarrage
    # ------------------------------------------------------------------

    def run(self):
        self.detect_hardware()
        b = self.bridge
        b.advertise(self.cfg.topic("status"), "std_msgs/String")
        b.advertise(self.cfg.topic("audio"), "std_msgs/String")
        b.advertise(self.cfg.topic("speaking"), "std_msgs/Bool")
        b.advertise(self.cfg.topic("image"), "std_msgs/String")
        b.subscribe(self.cfg.topic("audio_out"), "std_msgs/String", self.on_audio_out)
        b.subscribe(self.cfg.topic("image_request"), "std_msgs/Empty", self.on_image_request)
        b.subscribe(self.cfg.topic("heard"), "std_msgs/String", self.on_heard)

        threads = [threading.Thread(target=b.run_forever, name="rosbridge"),
                   threading.Thread(target=self.status_loop, name="status"),
                   threading.Thread(target=self._guard(self.listen_loop), name="listen")]
        if self.camera.available:
            threads.append(threading.Thread(target=self.camera.run, args=(self.stop,),
                                            name="camera"))
            if self.cfg.image_interval > 0:
                threads.append(threading.Thread(target=self.image_loop, name="image"))
        for t in threads:
            t.daemon = True
            t.start()
        self.stop.wait()
        b.stop()

    def _guard(self, fn):
        """Arrête le programme si une boucle critique échoue (systemd relancera)."""
        def wrapper():
            try:
                fn()
            except Exception:
                log.exception("Arrêt sur erreur dans %s", fn.__name__)
                self.stop.set()
        return wrapper


def main():
    load_dotenv(os.path.join(HERE, ".env"))
    logging.basicConfig(level=_env("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    if not cfg.rosbridge_url:
        log.error("ROSBRIDGE_URL (ou DOMAIN) est obligatoire")
        return 2
    if not cfg.rosbridge_token:
        log.warning("ROSBRIDGE_TOKEN vide : rosbridge-proxy refusera la connexion")

    drone = StaticDrone(cfg)
    stopped = []

    def on_signal(*_):
        stopped.append(True)
        drone.stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info("Drone %s → %s", cfg.drone_id, cfg.rosbridge_url)
    drone.run()
    # Code non nul si l'arrêt vient d'une erreur : systemd relancera.
    return 0 if stopped else 1


if __name__ == "__main__":
    sys.exit(main())
