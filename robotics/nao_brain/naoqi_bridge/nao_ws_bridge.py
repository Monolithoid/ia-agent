#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
nao_ws_bridge.py — Script embarque sur le robot NAO (Python 2.7).

Etablit une connexion WebSocket SORTANTE vers rosbridge sur le VPS,
s'abonne aux topics de commande et publie le statut du robot.

Usage :
    python /home/nao/nao_ws_bridge.py --vps ws://IP_VPS:9090 [options]

Options de filtrage des modules :
    --modules led,speech,gesture   Active uniquement les modules listés.
                                   Modules disponibles : led, speech, gesture,
                                   motion, move, stop (defaut: tous actifs).
    --no-audio-capture             Desactive le wake word et la capture VAD.

Exemples :
    # LED + parole uniquement, sans capture audio :
    python nao_ws_bridge.py --vps ws://IP:9090 --modules led,speech --no-audio-capture

    # Tout actif (comportement par defaut) :
    python nao_ws_bridge.py --vps ws://IP:9090

Dependances :
    Aucune dependance externe — client WebSocket pur Python 2.7 integre.
    naoqi (disponible nativement sur le robot)
"""

from __future__ import print_function

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time

try:
    from naoqi import ALProxy, ALModule, ALBroker
    HAS_NAOQI = True
except ImportError:
    HAS_NAOQI = False
    ALBroker = None
    class ALModule(object):
        def __init__(self, name):
            pass
    print("[bridge] WARNING: module naoqi introuvable — mode MOCK actif", file=sys.stderr)

NAO_IP = "127.0.0.1"
NAO_PORT = 9559
RECONNECT_DELAY = 5
STATUS_INTERVAL = 30

_PROVISION_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nao_provision.json"
)

# Mapping module → (topic, msg_type)
_MODULE_TOPICS = {
    "speech":   ("/nao/speech",   "std_msgs/String"),
    "gesture":  ("/nao/gesture",  "std_msgs/String"),
    "led":      ("/nao/leds",     "std_msgs/String"),
    "motion":   ("/nao/motion",   "std_msgs/String"),
    "move":     ("/nao/cmd_vel",  "geometry_msgs/Twist"),
    "stop":     ("/nao/stop",     "std_msgs/Empty"),
}
# Topics toujours actifs (contrôle du bridge lui-même)
_SYSTEM_TOPICS = [
    ("/nao/shutdown", "std_msgs/Empty"),
]
ALL_MODULES = frozenset(_MODULE_TOPICS.keys())

_BROKER      = None  # ALBroker local (doit rester vivant pour les callbacks)
_CAPTURE_REF = None  # empêche le GC de l'instance _AudioCapture courante


# ======================================================================
# Client WebSocket pur Python 2.7 (RFC 6455, ws:// uniquement)
# Remplace websocket-client pour eviter toute dependance au module ssl.
# ======================================================================

class _WebSocketApp(object):

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url, on_open=None, on_message=None,
                 on_error=None, on_close=None):
        self.url = url
        self.on_open = on_open
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self._sock = None
        self._closed = False

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def send(self, data):
        if isinstance(data, unicode):
            data = data.encode("utf-8")
        self._send_frame(data, opcode=0x1)

    def close(self):
        self._closed = True
        if self._sock:
            try:
                self._send_frame(b"", opcode=0x8)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass

    def run_forever(self, **kwargs):
        try:
            self._connect()
        except Exception as exc:
            if self.on_error:
                self.on_error(self, exc)
            if self.on_close:
                self.on_close(self)
            return

        if self.on_open:
            self.on_open(self)

        try:
            self._recv_loop()
        except Exception as exc:
            if not self._closed and self.on_error:
                self.on_error(self, exc)

        if self.on_close:
            self.on_close(self)

    # ------------------------------------------------------------------
    # Connexion TCP + handshake HTTP/WebSocket
    # ------------------------------------------------------------------

    def _connect(self):
        if not self.url.startswith("ws://"):
            raise ValueError("Seule ws:// est supportee (recu: {})".format(self.url))
        tail = self.url[5:]

        if "/" in tail:
            host_port, path = tail.split("/", 1)
            path = "/" + path
        else:
            host_port, path = tail, "/"

        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        else:
            host, port = host_port, 80

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(10)
        self._sock.connect((host, port))
        self._sock.settimeout(None)

        raw_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            "GET {path} HTTP/1.1\r\n"
            "Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).format(path=path, host=host, port=port, key=raw_key)
        self._sock.sendall(req.encode("ascii"))

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise IOError("Connexion fermee pendant le handshake")
            resp += chunk

        status_line = resp.split(b"\r\n")[0]
        if b"101" not in status_line:
            raise IOError("Echec handshake WebSocket : {}".format(
                status_line.decode("ascii", "replace")))

        expected = base64.b64encode(
            hashlib.sha1(
                (raw_key + self._GUID).encode("ascii")
            ).digest()
        ).decode("ascii")
        if expected not in resp.decode("ascii", "replace"):
            raise IOError("Sec-WebSocket-Accept invalide")

    # ------------------------------------------------------------------
    # Framing WebSocket (RFC 6455)
    # ------------------------------------------------------------------

    def _send_frame(self, data, opcode=0x1):
        if isinstance(data, unicode):
            data = data.encode("utf-8")
        mask_key = bytearray(os.urandom(4))
        payload = bytearray(data)
        length = len(payload)

        header = bytearray()
        header.append(0x80 | opcode)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask_key)

        masked = bytearray(length)
        for i, b in enumerate(payload):
            masked[i] = b ^ mask_key[i % 4]

        self._sock.sendall(bytes(header) + bytes(masked))

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise IOError("Connexion fermee")
            buf += chunk
        return buf

    def _recv_loop(self):
        while not self._closed:
            b0, b1 = bytearray(self._recv_exact(2))
            opcode = b0 & 0x0F
            is_masked = (b1 & 0x80) != 0
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]

            mask_key = bytearray(self._recv_exact(4)) if is_masked else None
            payload = bytearray(self._recv_exact(length))

            if mask_key:
                for i in range(length):
                    payload[i] ^= mask_key[i % 4]

            if opcode == 0x1:   # texte
                if self.on_message:
                    self.on_message(self, payload.decode("utf-8", "replace"))
            elif opcode == 0x2: # binaire
                if self.on_message:
                    self.on_message(self, bytes(payload))
            elif opcode == 0x8: # close
                self._closed = True
                break
            elif opcode == 0x9: # ping → pong
                self._send_frame(bytes(payload), opcode=0xA)


# ======================================================================
# Bridge NAO ↔ rosbridge
# ======================================================================

def _provision(config_path):
    """Applique la configuration post-flash définie dans config_path (JSON)."""
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception as e:
        print("[provision] Erreur lecture {}: {}".format(config_path, e), file=sys.stderr)
        return False

    print("[provision] Application de {} ...".format(config_path), file=sys.stderr)
    ok = True

    # -- WiFi ----------------------------------------------------------
    for ap in config.get("wifi", []):
        ssid       = ap.get("ssid", "").strip()
        passphrase = ap.get("passphrase", "")
        security   = str(ap.get("security", "wpa2-personal"))
        if not ssid:
            continue
        proxy = get_proxy("ALNetwork")
        if proxy:
            try:
                proxy.joinWifiNetwork(ssid, security, passphrase)
                print("[provision] WiFi '{}' configuré.".format(ssid), file=sys.stderr)
            except Exception as e:
                print("[provision] WiFi '{}' erreur : {}".format(ssid, e), file=sys.stderr)
                ok = False
        else:
            print("[provision] ALNetwork indisponible — WiFi ignoré.", file=sys.stderr)

    # -- Vie autonome --------------------------------------------------
    al_cfg = config.get("autonomous_life", {})
    if "state" in al_cfg:
        state = str(al_cfg["state"])
        valid_states = ("disabled", "solitary", "interactive", "safeguard")
        if state not in valid_states:
            print("[provision] autonomous_life.state '{}' invalide (valeurs : {}).".format(
                state, ", ".join(valid_states)), file=sys.stderr)
        else:
            proxy = get_proxy("ALAutonomousLife")
            if proxy:
                try:
                    proxy.setState(state)
                    print("[provision] ALAutonomousLife -> '{}'.".format(state), file=sys.stderr)
                except Exception as e:
                    print("[provision] ALAutonomousLife erreur : {}".format(e), file=sys.stderr)
                    ok = False

    # -- Langues -------------------------------------------------------
    lang_cfg = config.get("language", {})
    if "tts" in lang_cfg:
        lang = str(lang_cfg["tts"])
        proxy = get_proxy("ALTextToSpeech")
        if proxy:
            try:
                available = proxy.getAvailableLanguages()
                if lang in available:
                    proxy.setLanguage(lang)
                    print("[provision] TTS langue -> '{}'.".format(lang), file=sys.stderr)
                else:
                    print("[provision] TTS : langue '{}' non disponible (disponibles : {}).".format(
                        lang, ", ".join(available)), file=sys.stderr)
                    ok = False
            except Exception as e:
                print("[provision] TTS erreur : {}".format(e), file=sys.stderr)
                ok = False

    if "asr" in lang_cfg:
        lang = str(lang_cfg["asr"])
        proxy = get_proxy("ALSpeechRecognition")
        if proxy:
            try:
                proxy.setLanguage(lang)
                print("[provision] ASR langue -> '{}'.".format(lang), file=sys.stderr)
            except Exception as e:
                print("[provision] ASR erreur : {}".format(e), file=sys.stderr)
                ok = False

    # -- Système -------------------------------------------------------
    sys_cfg = config.get("system", {})
    if "volume" in sys_cfg:
        vol = int(sys_cfg["volume"])
        if not (0 <= vol <= 100):
            print("[provision] Volume {} hors plage [0-100].".format(vol), file=sys.stderr)
        else:
            proxy = get_proxy("ALAudioDevice")
            if proxy:
                try:
                    proxy.setOutputVolume(vol)
                    print("[provision] Volume -> {}.".format(vol), file=sys.stderr)
                except Exception as e:
                    print("[provision] Volume erreur : {}".format(e), file=sys.stderr)
                    ok = False

    status = "OK" if ok else "terminé avec des erreurs"
    print("[provision] Provisioning {}.".format(status), file=sys.stderr)
    return ok


def get_proxy(service):
    try:
        return ALProxy(service, NAO_IP, NAO_PORT)
    except Exception as e:
        print("[bridge] Proxy {} indisponible : {}".format(service, e), file=sys.stderr)
        return None


def _ws_log(direction, msg):
    """Affiche sur stderr un résumé lisible d'un échange WebSocket."""
    op    = msg.get("op", "?")
    topic = msg.get("topic", "")
    inner = msg.get("msg", msg.get("data", {}))
    summary = json.dumps(inner, ensure_ascii=False)
    # Tronquer les gros payloads (audio base64, etc.)
    if len(summary) > 200:
        summary = u"{}… [tronqué, {} octets]".format(summary[:200], len(summary))
    ts = time.strftime("%H:%M:%S")
    print(u"[WS {dir}] {ts}  op={op}  topic={topic}  {summary}".format(
        dir=direction, ts=ts, op=op, topic=topic, summary=summary,
    ).encode("utf-8", "replace"), file=sys.stderr)


def _ensure_broker():
    global _BROKER
    if _BROKER is not None:
        return True
    if not HAS_NAOQI or ALBroker is None:
        return False
    try:
        _BROKER = ALBroker("naoBridgeBroker", "0.0.0.0", 0, NAO_IP, NAO_PORT)
        return True
    except Exception as e:
        print("[bridge] ALBroker init error: {}".format(e), file=sys.stderr)
        return False


class _AudioCapture(ALModule):
    """Module NAOqi recevant les trames audio et détectant la fin de parole par VAD."""

    _RATE         = 16000
    _SPEECH_RMS   = 600    # seuil RMS déclenchant la capture
    _SILENCE_RMS  = 200    # seuil RMS en-dessous duquel c'est du silence
    _SILENCE_SEC  = 1.5    # secondes de silence consécutives pour stopper
    _SAMPLES_CALL = 170    # échantillons/trame NAO à 16kHz (valeur par défaut)
    _MAX_SEC      = 10.0   # timeout de sécurité

    def __init__(self, name):
        ALModule.__init__(self, name)
        self._name     = name
        self._pcm      = []
        self._speaking = False
        self._sil_n    = 0
        self._sil_need = int(self._SILENCE_SEC * self._RATE / self._SAMPLES_CALL)
        self._done     = threading.Event()

    def processRemote(self, nb_ch, nb_samp, _ts, buffer):
        if self._done.is_set():
            return
        try:
            samples = [buffer[i] for i in range(0, nb_ch * nb_samp, nb_ch)]
        except TypeError:
            all_s = struct.unpack_from("<{}h".format(nb_ch * nb_samp), bytes(buffer))
            samples = list(all_s[::nb_ch])
        if not samples:
            return
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        if not self._speaking:
            if rms > self._SPEECH_RMS:
                self._speaking = True
                self._pcm.extend(samples)
        else:
            self._pcm.extend(samples)
            if rms < self._SILENCE_RMS:
                self._sil_n += 1
                if self._sil_n >= self._sil_need:
                    self._done.set()
            else:
                self._sil_n = 0

    def to_wav(self):
        import io as _io
        if not self._pcm:
            return b""
        raw = struct.pack("<{}h".format(len(self._pcm)), *self._pcm)
        buf = _io.BytesIO()
        buf.write(b"RIFF")
        buf.write(struct.pack("<I", 36 + len(raw)))
        buf.write(b"WAVE")
        buf.write(b"fmt ")
        buf.write(struct.pack("<IHHIIHH", 16, 1, 1,
                              self._RATE, self._RATE * 2, 2, 16))
        buf.write(b"data")
        buf.write(struct.pack("<I", len(raw)))
        buf.write(raw)
        return buf.getvalue()


class NaoBridge(object):

    def __init__(self, vps_url, modules=None, audio_capture=True):
        """
        modules       : ensemble de noms de modules à activer (None = tous).
        audio_capture : active le wake word et la capture VAD si True.
        """
        self.vps_url = vps_url
        self._modules = ALL_MODULES if modules is None else frozenset(modules)
        self._audio_capture_enabled = audio_capture
        self.ws = None
        self._stop = threading.Event()
        self._busy_until = 0.0       # timestamp jusqu'auquel le wake word est ignoré
        self._wakeword_started = False  # évite de relancer le thread à chaque reconnexion
        self._asr = None             # proxy ASR conservé pour unsubscribe à l'arrêt
        self._audio_capture = None   # module _AudioCapture en cours d'enregistrement

    # ------------------------------------------------------------------
    # Connexion / reconnexion
    # ------------------------------------------------------------------

    def run(self):
        while not self._stop.is_set():
            print("[bridge] Connexion a {} ...".format(self.vps_url), file=sys.stderr)
            try:
                self.ws = _WebSocketApp(
                    self.vps_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever()
            except Exception as e:
                print("[bridge] Erreur WebSocket : {}".format(e), file=sys.stderr)

            if not self._stop.is_set():
                print("[bridge] Reconnexion dans {}s ...".format(RECONNECT_DELAY), file=sys.stderr)
                time.sleep(RECONNECT_DELAY)

    def stop(self):
        self._stop.set()
        self._cleanup_resources()
        if self.ws:
            self.ws.close()

    def _cleanup_resources(self):
        """Libère les ressources NAOqi : désabonnement ASR, arrêt éventuel de la capture VAD."""
        if self._audio_capture:
            self._audio_capture._done.set()
            audio_dev = get_proxy("ALAudioDevice")
            if audio_dev:
                try:
                    audio_dev.unsubscribe(self._audio_capture._name)
                    print("[bridge] Capture VAD arrêtée.", file=sys.stderr)
                except Exception as e:
                    print("[bridge] Erreur arrêt VAD: {}".format(e), file=sys.stderr)
            self._audio_capture = None

        if self._asr:
            try:
                self._asr.unsubscribe("nao_ws_bridge_ww")
                print("[bridge] ASR désabonné.", file=sys.stderr)
            except Exception as e:
                print("[bridge] Erreur désabonnement ASR: {}".format(e), file=sys.stderr)
            self._asr = None

    # ------------------------------------------------------------------
    # Evenements WebSocket
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Envoi WebSocket avec logging
    # ------------------------------------------------------------------

    def _send(self, payload_dict):
        _ws_log(u"→", payload_dict)
        if self.ws and not self._stop.is_set():
            self.ws.send(json.dumps(payload_dict))

    def _on_open(self, ws):
        active = sorted(self._modules)
        print("[bridge] Connecte. Modules actifs : {}. Capture audio : {}.".format(
            ", ".join(active) if active else "aucun",
            "oui" if self._audio_capture_enabled else "non",
        ), file=sys.stderr)

        # Topics filtrés selon les modules activés
        for mod, (topic, msg_type) in _MODULE_TOPICS.items():
            if mod in self._modules:
                sub_msg = {"op": "subscribe", "topic": topic, "type": msg_type}
                _ws_log(u"→", sub_msg)
                ws.send(json.dumps(sub_msg))

        # Topics système toujours souscrits
        for topic, msg_type in _SYSTEM_TOPICS:
            sub_msg = {"op": "subscribe", "topic": topic, "type": msg_type}
            _ws_log(u"→", sub_msg)
            ws.send(json.dumps(sub_msg))

        # Topics publiés par le bridge
        advertised = ["/nao/status"]
        if self._audio_capture_enabled:
            advertised.append("/nao/audio")
        for topic in advertised:
            adv_msg = {"op": "advertise", "topic": topic, "type": "std_msgs/String"}
            _ws_log(u"→", adv_msg)
            ws.send(json.dumps(adv_msg))

        print("[bridge] Pret.", file=sys.stderr)
        threading.Thread(target=self._status_loop).start()
        if self._audio_capture_enabled and not self._wakeword_started:
            self._wakeword_started = True
            threading.Thread(target=self._wakeword_loop).start()

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        _ws_log(u"←", msg)
        if msg.get("op") != "publish":
            return
        topic = msg.get("topic", "")
        data = msg.get("msg", {})
        self._dispatch(topic, data)

    def _on_error(self, ws, error):
        print("[bridge] Erreur : {}".format(error), file=sys.stderr)

    def _on_close(self, ws):
        print("[bridge] Connexion fermee.", file=sys.stderr)

    # ------------------------------------------------------------------
    # Dispatch des commandes recues
    # ------------------------------------------------------------------

    def _dispatch(self, topic, data):
        if topic == "/nao/speech" and "speech" in self._modules:
            text = data.get("data", "")
            if text:
                self._say(text)
        elif topic == "/nao/cmd_vel" and "move" in self._modules:
            linear = data.get("linear", {})
            angular = data.get("angular", {})
            self._move(
                float(linear.get("x", 0.0)),
                float(linear.get("y", 0.0)),
                float(angular.get("z", 0.0)),
            )
        elif topic == "/nao/gesture" and "gesture" in self._modules:
            posture = data.get("data", "Stand")
            self._posture(posture)
        elif topic == "/nao/leds" and "led" in self._modules:
            try:
                payload = json.loads(data.get("data", "{}"))
            except ValueError:
                return
            self._leds(
                str(payload.get("name", "AllLeds")),
                float(payload.get("r", 0.0)),
                float(payload.get("g", 0.0)),
                float(payload.get("b", 0.0)),
            )
        elif topic == "/nao/stop" and "stop" in self._modules:
            self._stop_move()
        elif topic == "/nao/shutdown":
            # Toujours traité, quel que soit le filtrage des modules
            print("[bridge] Commande /nao/shutdown reçue — arrêt propre.", file=sys.stderr)
            threading.Thread(target=self.stop).start()
        elif topic == "/nao/motion" and "motion" in self._modules:
            try:
                payload = json.loads(data.get("data", "{}"))
            except ValueError:
                return
            self._motion_joint(
                str(payload.get("joint", "HeadYaw")),
                float(payload.get("angle", 0.0)),
                float(payload.get("speed", 0.05)),
            )

    # ------------------------------------------------------------------
    # Commandes NAOqi
    # ------------------------------------------------------------------

    def _say(self, text):
        if not HAS_NAOQI:
            print("[MOCK] say: {}".format(text), file=sys.stderr)
            return
        proxy = get_proxy("ALTextToSpeech")
        if proxy:
            try:
                proxy.setLanguage("French")
                proxy.say(text.encode("utf-8") if isinstance(text, unicode) else text)
            except Exception as e:
                print("[bridge] say error: {}".format(e), file=sys.stderr)

    def _move(self, x, y, theta):
        if not HAS_NAOQI:
            print("[MOCK] move: x={} y={} theta={}".format(x, y, theta), file=sys.stderr)
            return
        proxy = get_proxy("ALMotion")
        if proxy:
            try:
                proxy.moveToward(x, y, theta)
            except Exception as e:
                print("[bridge] move error: {}".format(e), file=sys.stderr)

    def _posture(self, name):
        if not HAS_NAOQI:
            print("[MOCK] posture: {}".format(name), file=sys.stderr)
            return
        proxy = get_proxy("ALRobotPosture")
        if proxy:
            try:
                proxy.goToPosture(str(name), 0.5)
            except Exception as e:
                print("[bridge] posture error: {}".format(e), file=sys.stderr)

    def _leds(self, group, r, g, b):
        if not HAS_NAOQI:
            print("[MOCK] leds: {} rgb({},{},{})".format(group, r, g, b), file=sys.stderr)
            return
        proxy = get_proxy("ALLeds")
        if proxy:
            try:
                proxy.fadeRGB(group, r, g, b, 0.5)
            except Exception as e:
                print("[bridge] leds error: {}".format(e), file=sys.stderr)

    def _stop_move(self):
        if not HAS_NAOQI:
            print("[MOCK] stop", file=sys.stderr)
            return
        proxy = get_proxy("ALMotion")
        if proxy:
            try:
                proxy.stopMove()
            except Exception as e:
                print("[bridge] stop error: {}".format(e), file=sys.stderr)

    def _motion_joint(self, joint, angle, speed):
        if not HAS_NAOQI:
            print("[MOCK] motion_joint: {} -> {:.3f} rad speed={}".format(joint, angle, speed),
                  file=sys.stderr)
            return
        proxy = get_proxy("ALMotion")
        if proxy:
            try:
                proxy.setStiffnesses("Head", 1.0)
                proxy.angleInterpolationWithSpeed([joint], [angle], speed, True)
            except Exception as e:
                print("[bridge] motion_joint error: {}".format(e), file=sys.stderr)

    # ------------------------------------------------------------------
    # Publication du statut (batterie) toutes les STATUS_INTERVAL s
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Wake word → enregistrement audio → publication /nao/audio
    # ------------------------------------------------------------------

    def _wakeword_loop(self):
        memory = get_proxy("ALMemory")
        asr    = get_proxy("ALSpeechRecognition")
        if not memory or not asr:
            print("[bridge] ASR non disponible — wake word désactivé.", file=sys.stderr)
            return
        try:
            try:
                asr.setLanguage("French")
            except Exception:
                asr.setLanguage("English")
                print("[bridge] ASR: French non disponible, bascule sur English.", file=sys.stderr)
            asr.setVocabulary(["ok nao", "okay nao"], True)
            asr.subscribe("nao_ws_bridge_ww")
            self._asr = asr
            print("[bridge] Wake word 'ok nao' actif.", file=sys.stderr)
        except Exception as e:
            print("[bridge] ASR init error: {}".format(e), file=sys.stderr)
            return

        last_word = ""
        _exiting_busy = False
        while not self._stop.is_set():
            try:
                if time.time() < self._busy_until:
                    _exiting_busy = True
                    time.sleep(0.2)
                    continue

                if _exiting_busy:
                    # Première itération après la période occupée :
                    # on vide le cache ALMemory pour éviter une fausse détection
                    # sur la valeur residuelle du dernier "ok nao".
                    _exiting_busy = False
                    last_word = ""
                    try:
                        memory.insertData("WordRecognized", ["", 0.0])
                    except Exception:
                        pass
                    time.sleep(0.2)
                    continue

                value = memory.getData("WordRecognized")
                if value and len(value) >= 2 and float(value[1]) > 0.3:
                    phrase = value[0].lower().strip()
                    if phrase != last_word and ("ok nao" in phrase or "okay nao" in phrase):
                        last_word = phrase
                        self._busy_until = time.time() + 20.0  # bloque pendant écoute + TTS
                        # Efface immédiatement le cache ALMemory pour éviter une re-détection
                        # si body_gateway répond lentement et dépasse les 20 s.
                        try:
                            memory.insertData("WordRecognized", ["", 0.0])
                        except Exception:
                            pass
                        threading.Thread(target=self._record_and_publish).start()
            except Exception as e:
                print("[bridge] wakeword_loop error: {}".format(e), file=sys.stderr)
            time.sleep(0.2)

    def _record_and_publish(self):
        global _CAPTURE_REF

        if not _ensure_broker():
            print("[bridge] Broker NAOqi non disponible — enregistrement annulé.", file=sys.stderr)
            return

        module_name = "NaoCap{:05d}".format(int(time.time() * 10) % 100000)
        capture = _AudioCapture(module_name)
        _CAPTURE_REF = capture
        self._audio_capture = capture

        audio_dev = get_proxy("ALAudioDevice")
        if not audio_dev:
            print("[bridge] ALAudioDevice non disponible.", file=sys.stderr)
            _CAPTURE_REF = None
            self._audio_capture = None
            return

        try:
            print("[bridge] Ecoute VAD (max {}s) ...".format(_AudioCapture._MAX_SEC),
                  file=sys.stderr)
            audio_dev.setClientPreferences(module_name, _AudioCapture._RATE, 0, 0)
            audio_dev.subscribe(module_name)

            capture._done.wait(_AudioCapture._MAX_SEC + 1.0)

            audio_dev.unsubscribe(module_name)
            self._audio_capture = None
            _CAPTURE_REF = None

            if not capture._speaking:
                print("[bridge] Aucune parole détectée.", file=sys.stderr)
                return

            audio_bytes = capture.to_wav()
            if not audio_bytes:
                return

            encoded = base64.b64encode(audio_bytes)
            if isinstance(encoded, bytes):
                encoded = encoded.decode("ascii")

            if not self._stop.is_set():
                duration = len(capture._pcm) / float(_AudioCapture._RATE)
                print("[bridge] Audio VAD prêt ({} octets, {:.1f}s) — envoi ...".format(
                    len(audio_bytes), duration), file=sys.stderr)
                self._send({
                    "op":    "publish",
                    "topic": "/nao/audio",
                    "msg":   {"data": encoded},
                })
        except Exception as e:
            print("[bridge] _record_and_publish error: {}".format(e), file=sys.stderr)
            try:
                audio_dev.unsubscribe(module_name)
            except Exception:
                pass
            self._audio_capture = None
            _CAPTURE_REF = None

    # ------------------------------------------------------------------
    # Publication du statut (batterie) toutes les STATUS_INTERVAL s
    # ------------------------------------------------------------------

    def _status_loop(self):
        while not self._stop.is_set() and self.ws:
            level = -1
            if HAS_NAOQI:
                proxy = get_proxy("ALBattery")
                if proxy:
                    try:
                        level = proxy.getBatteryCharge()
                    except Exception:
                        pass
            payload = json.dumps({"battery": level})
            try:
                self._send({
                    "op": "publish",
                    "topic": "/nao/status",
                    "msg": {"data": payload},
                })
            except Exception:
                break
            self._stop.wait(STATUS_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NAO WebSocket bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Modules disponibles : {}\n\n"
            "Exemples :\n"
            "  Tout actif (defaut) :\n"
            "    python nao_ws_bridge.py --vps ws://IP:9090\n\n"
            "  LED + parole seulement, sans capture audio :\n"
            "    python nao_ws_bridge.py --vps ws://IP:9090 --modules led,speech --no-audio-capture\n\n"
            "  Gestes + LED uniquement :\n"
            "    python nao_ws_bridge.py --vps ws://IP:9090 --modules gesture,led"
        ).format(",".join(sorted(ALL_MODULES)))
    )
    parser.add_argument("--vps", default=None,
                        help="URL rosbridge (ex: ws://IP_VPS:9090). Obligatoire sauf avec --provision seul.")
    parser.add_argument(
        "--provision",
        nargs="?",
        const=_PROVISION_DEFAULT,
        default=None,
        metavar="CONFIG_FILE",
        help=(
            "Applique la configuration post-flash (WiFi, vie autonome, langue, volume) "
            "avant de démarrer le bridge. "
            "Sans argument utilise nao_provision.json à côté du script. "
            "Sans --vps, quitte après le provisioning."
        ),
    )
    parser.add_argument("--nao-ip", default="127.0.0.1",
                        help="IP du NAO (defaut: 127.0.0.1)")
    parser.add_argument("--nao-port", type=int, default=9559,
                        help="Port NAOqi (defaut: 9559)")
    parser.add_argument(
        "--modules",
        default=None,
        metavar="MOD1,MOD2,...",
        help=(
            "Liste de modules a activer, separee par des virgules. "
            "Valeurs possibles : {}. "
            "Par defaut tous les modules sont actifs."
        ).format(", ".join(sorted(ALL_MODULES)))
    )
    parser.add_argument(
        "--no-audio-capture",
        dest="audio_capture",
        action="store_false",
        default=True,
        help="Desactive le wake word et la capture VAD (actifs par defaut)."
    )
    args = parser.parse_args()

    if not args.provision and not args.vps:
        parser.error("--vps est obligatoire sauf en mode --provision seul.")

    NAO_IP = args.nao_ip
    NAO_PORT = args.nao_port

    # Provisioning post-flash
    if args.provision:
        _provision(args.provision)
        if not args.vps:
            sys.exit(0)

    # Valider et parser les modules demandés
    if args.modules is not None:
        requested = set(m.strip().lower() for m in args.modules.split(",") if m.strip())
        unknown = requested - ALL_MODULES
        if unknown:
            parser.error(
                "Module(s) inconnu(s) : {}. Modules valides : {}".format(
                    ", ".join(sorted(unknown)),
                    ", ".join(sorted(ALL_MODULES)),
                )
            )
        active_modules = requested
    else:
        active_modules = None  # tous

    bridge = NaoBridge(args.vps, modules=active_modules, audio_capture=args.audio_capture)
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\n[bridge] Ctrl+C reçu — arrêt propre en cours ...", file=sys.stderr)
        bridge.stop()
        print("[bridge] Arrêté.", file=sys.stderr)
