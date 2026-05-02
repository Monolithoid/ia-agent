#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
nao_ws_bridge.py — Script embarque sur le robot NAO (Python 2.7).

Etablit une connexion WebSocket SORTANTE vers rosbridge sur le VPS,
s'abonne aux topics de commande et publie le statut du robot.

Usage :
    python /home/nao/nao_ws_bridge.py --vps ws://IP_VPS:9090

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
    from naoqi import ALProxy
    HAS_NAOQI = True
except ImportError:
    HAS_NAOQI = False
    print("[bridge] WARNING: module naoqi introuvable — mode MOCK actif", file=sys.stderr)

NAO_IP = "127.0.0.1"
NAO_PORT = 9559
RECONNECT_DELAY = 5
STATUS_INTERVAL = 30


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

def get_proxy(service):
    try:
        return ALProxy(service, NAO_IP, NAO_PORT)
    except Exception as e:
        print("[bridge] Proxy {} indisponible : {}".format(service, e), file=sys.stderr)
        return None


class NaoBridge(object):

    def __init__(self, vps_url):
        self.vps_url = vps_url
        self.ws = None
        self._stop = threading.Event()
        self._busy_until = 0.0       # timestamp jusqu'auquel le wake word est ignoré
        self._wakeword_started = False  # évite de relancer le thread à chaque reconnexion
        self._asr = None             # proxy ASR conservé pour unsubscribe à l'arrêt
        self._recorder = None        # proxy recorder conservé pour stopMicrophonesRecording

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
        """Libère les ressources NAOqi : désabonnement ASR, arrêt éventuel de l'enregistrement."""
        if self._recorder:
            try:
                self._recorder.stopMicrophonesRecording()
                print("[bridge] Enregistrement audio arrêté.", file=sys.stderr)
            except Exception as e:
                print("[bridge] Erreur arrêt recorder: {}".format(e), file=sys.stderr)
            self._recorder = None

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

    def _on_open(self, ws):
        print("[bridge] Connecte. Abonnement aux topics ...", file=sys.stderr)
        topics = [
            ("/nao/speech",    "std_msgs/String"),
            ("/nao/cmd_vel",   "geometry_msgs/Twist"),
            ("/nao/gesture",   "std_msgs/String"),
            ("/nao/leds",      "std_msgs/String"),
            ("/nao/stop",      "std_msgs/Empty"),
            ("/nao/motion",    "std_msgs/String"),
            ("/nao/shutdown",  "std_msgs/Empty"),
        ]
        for topic, msg_type in topics:
            ws.send(json.dumps({"op": "subscribe", "topic": topic, "type": msg_type}))

        ws.send(json.dumps({"op": "advertise", "topic": "/nao/status", "type": "std_msgs/String"}))
        ws.send(json.dumps({"op": "advertise", "topic": "/nao/audio",  "type": "std_msgs/String"}))
        print("[bridge] Pret.", file=sys.stderr)
        threading.Thread(target=self._status_loop).start()
        if not self._wakeword_started:
            self._wakeword_started = True
            threading.Thread(target=self._wakeword_loop).start()

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
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
        if topic == "/nao/speech":
            text = data.get("data", "")
            if text:
                self._say(text)
        elif topic == "/nao/cmd_vel":
            linear = data.get("linear", {})
            angular = data.get("angular", {})
            self._move(
                float(linear.get("x", 0.0)),
                float(linear.get("y", 0.0)),
                float(angular.get("z", 0.0)),
            )
        elif topic == "/nao/gesture":
            posture = data.get("data", "Stand")
            self._posture(posture)
        elif topic == "/nao/leds":
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
        elif topic == "/nao/stop":
            self._stop_move()
        elif topic == "/nao/shutdown":
            print("[bridge] Commande /nao/shutdown reçue — arrêt propre.", file=sys.stderr)
            threading.Thread(target=self.stop).start()
        elif topic == "/nao/motion":
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
        audio_path = "/home/nao/nao_audio.wav"
        recorder = get_proxy("ALAudioRecorder")
        if not recorder:
            print("[bridge] ALAudioRecorder non disponible.", file=sys.stderr)
            return
        try:
            print("[bridge] Enregistrement 4s ...", file=sys.stderr)
            self._recorder = recorder
            recorder.startMicrophonesRecording(audio_path, "wav", 16000, [0, 0, 1, 0])
            time.sleep(4.0)
            recorder.stopMicrophonesRecording()
            self._recorder = None
            time.sleep(0.3)  # laisse le fichier se fermer proprement

            with open(audio_path, "rb") as f:
                audio_bytes = f.read()

            encoded = base64.b64encode(audio_bytes)
            if isinstance(encoded, bytes):
                encoded = encoded.decode("ascii")

            if self.ws and not self._stop.is_set():
                self.ws.send(json.dumps({
                    "op":    "publish",
                    "topic": "/nao/audio",
                    "msg":   {"data": encoded},
                }))
                print("[bridge] Audio publié ({} octets).".format(len(audio_bytes)), file=sys.stderr)
        except Exception as e:
            print("[bridge] _record_and_publish error: {}".format(e), file=sys.stderr)

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
                self.ws.send(json.dumps({
                    "op": "publish",
                    "topic": "/nao/status",
                    "msg": {"data": payload},
                }))
            except Exception:
                break
            self._stop.wait(STATUS_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NAO WebSocket bridge")
    parser.add_argument("--vps", required=True, help="URL rosbridge (ex: ws://IP_VPS:9090)")
    parser.add_argument("--nao-ip", default="127.0.0.1", help="IP du NAO (defaut: 127.0.0.1)")
    parser.add_argument("--nao-port", type=int, default=9559)
    args = parser.parse_args()

    NAO_IP = args.nao_ip
    NAO_PORT = args.nao_port

    bridge = NaoBridge(args.vps)
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\n[bridge] Ctrl+C reçu — arrêt propre en cours ...", file=sys.stderr)
        bridge.stop()
        print("[bridge] Arrêté.", file=sys.stderr)
