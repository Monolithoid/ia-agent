#!/usr/bin/env python3
"""
fake-static-drone.py — Faux drone statique : teste de bout en bout le chemin
/drones/{id}/* → static-driver → body-gateway → static-driver → /drones/{id}/*
avec de faux messages rosbridge, sans Raspberry Pi.

Python 3 standard uniquement (client WebSocket minimal inclus).

Usage (sur le VPS, depuis la racine du dépôt) :
    python3 tests/fake-static-drone.py                       # ws://localhost:9090 direct
    python3 tests/fake-static-drone.py --url ws://rosbridge.$DOMAIN --token "$ROSBRIDGE_TOKEN"

Étapes :
  1. /drones/{id}/status            → /bodies/{id}/connected = true
  2. /drones/{id}/speaking = true   → /bodies/{id}/sensors/speaking = true
                                    → yeux du NAO « speaking » (/naoqi/leds FaceLeds,
                                      cognitive_renderer), puis retour au profil (AllLeds)
  3. /drones/{id}/image             → /bodies/{id}/sensors/image (même JSON)
  4. /bodies/{id}/actuators/say     → edge-tts → /drones/{id}/audio_out (MP3)
  5. /drones/{id}/audio (le MP3 de l'étape 4, angle -30°)
       → /bodies/{id}/sensors/sound_direction
       → /bodies/{nao}/actuators/motion HeadYaw (body-gateway)
       → Whisper → /bodies/{id}/sensors/speech
  6. réponse du LLM prononcée par le drone → /drones/{id}/audio_out

Code de sortie : nombre d'étapes en échec.
"""

import argparse
import base64
import json
import os
import queue
import socket
import ssl
import struct
import sys
import threading
import time
from urllib.parse import urlparse

GREEN, RED, CYAN, NC = "\033[0;32m", "\033[0;31m", "\033[0;36m", "\033[0m"


# ======================================================================
# Minimal WebSocket client (RFC 6455, text frames only)
# ======================================================================

class WebSocket:

    def __init__(self, url, headers=None, timeout=10):
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {u.path or '/'} HTTP/1.1", f"Host: {u.hostname}",
                 "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("connexion fermée pendant la poignée de main")
            response += chunk
        head, self._buf = response.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode()
        if " 101 " not in status + " ":
            raise ConnectionError(f"refus du serveur : {status}")
        sock.settimeout(None)
        self._sock = sock
        self._lock = threading.Lock()

    def send(self, text):
        data = text.encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self._lock:
            self._sock.sendall(bytes(header) + mask + payload)

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("connexion fermée")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """Return the next text message (fragments joined)."""
        parts = []
        while True:
            b0, b1 = self._read(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("fermeture par le serveur")
            if opcode == 0x9:       # ping → pong
                with self._lock:
                    m = os.urandom(4)
                    self._sock.sendall(bytes([0x8A, 0x80 | len(data)]) + m
                                       + bytes(b ^ m[i % 4] for i, b in enumerate(data)))
                continue
            if opcode in (0x0, 0x1, 0x2):
                parts.append(data)
                if b0 & 0x80:
                    return b"".join(parts).decode()

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


# ======================================================================
# rosbridge helpers
# ======================================================================

class Rosbridge:

    def __init__(self, url, token):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.ws = WebSocket(url, headers)
        self.inbox = {}             # topic → queue of msg dicts
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("op") == "publish" and msg.get("topic") in self.inbox:
                    self.inbox[msg["topic"]].put(msg.get("msg", {}))
        except (ConnectionError, OSError, ValueError):
            pass

    def subscribe(self, topic, msg_type):
        self.inbox[topic] = queue.Queue()
        self.ws.send(json.dumps({"op": "subscribe", "topic": topic, "type": msg_type}))

    def advertise(self, topic, msg_type):
        self.ws.send(json.dumps({"op": "advertise", "topic": topic, "type": msg_type}))

    def publish(self, topic, msg):
        self.ws.send(json.dumps({"op": "publish", "topic": topic, "msg": msg}))

    def drain(self, topic):
        q = self.inbox[topic]
        while not q.empty():
            q.get_nowait()

    def wait(self, topic, predicate=lambda m: True, timeout=10.0):
        """Return the first message on `topic` matching `predicate`, or None."""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                msg = self.inbox[topic].get(timeout=remaining)
            except queue.Empty:
                return None
            if predicate(msg):
                return msg


# ======================================================================
# Test scenario
# ======================================================================

def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=os.environ.get("ROSBRIDGE_URL", "ws://localhost:9090"))
    p.add_argument("--token", default=os.environ.get("ROSBRIDGE_TOKEN", ""))
    p.add_argument("--id", default="static_01", help="identifiant du drone statique")
    p.add_argument("--nao", default="nao_01", help="identifiant du NAO")
    p.add_argument("--text", default="OK Nao, bonjour, comment vas-tu ?",
                   help="phrase synthétisée puis renvoyée comme audio entendu")
    p.add_argument("--azimuth", type=float, default=-30.0)
    p.add_argument("--llm-timeout", type=float, default=120.0)
    args = p.parse_args()

    failures = 0

    def result(ok, desc, info=""):
        nonlocal failures
        if ok:
            print(f"  {GREEN}✓ PASS{NC}  {desc}")
        else:
            failures += 1
            print(f"  {RED}✗ FAIL{NC}  {desc}")
        if info:
            print(f"        {CYAN}{info}{NC}")
        return ok

    drone, body = f"/drones/{args.id}", f"/bodies/{args.id}"
    try:
        rb = Rosbridge(args.url, args.token)
    except (OSError, ConnectionError) as e:
        result(False, f"connexion rosbridge {args.url}", str(e))
        return 1
    result(True, f"connexion rosbridge {args.url}")

    for topic, kind in [(f"{body}/connected", "std_msgs/Bool"),
                        (f"{body}/sensors/speaking", "std_msgs/Bool"),
                        (f"{body}/sensors/image", "std_msgs/String"),
                        (f"{body}/sensors/sound_direction", "std_msgs/String"),
                        (f"{body}/sensors/speech", "std_msgs/String"),
                        (f"{drone}/audio_out", "std_msgs/String"),
                        (f"/bodies/{args.nao}/actuators/motion", "std_msgs/String"),
                        ("/naoqi/leds", "std_msgs/String")]:
        rb.subscribe(topic, kind)
    for topic, kind in [(f"{drone}/status", "std_msgs/String"),
                        (f"{drone}/audio", "std_msgs/String"),
                        (f"{drone}/speaking", "std_msgs/Bool"),
                        (f"{drone}/image", "std_msgs/String"),
                        (f"{body}/actuators/say", "std_msgs/String")]:
        rb.advertise(topic, kind)
    time.sleep(2.0)     # laisse rosbridge créer publishers et abonnements

    # 1. Signal de vie → connecté
    status = {"drone_id": args.id, "type": "static", "capabilities": ["audio_in", "audio_out", "image", "direction"],
              "mics": 4, "respeaker": True, "aec": False, "direction_mode": "respeaker",
              "camera_hfov_deg": 66, "speaking": False, "uptime_s": 1}
    rb.publish(f"{drone}/status", {"data": json.dumps(status)})
    m = rb.wait(f"{body}/connected", lambda m: m.get("data") is True, timeout=20)
    result(m is not None, f"/drones/{args.id}/status → {body}/connected = true")

    # 2. speaking relayé
    def led_group(m):
        try:
            d = json.loads(m.get("data", "{}"))
        except ValueError:
            return None
        return d.get("group") or d.get("name")

    rb.drain("/naoqi/leds")
    rb.publish(f"{drone}/speaking", {"data": True})
    m = rb.wait(f"{body}/sensors/speaking", lambda m: m.get("data") is True)
    result(m is not None, f"/drones/{args.id}/speaking → {body}/sensors/speaking")
    m = rb.wait("/naoqi/leds", lambda m: led_group(m) == "FaceLeds")
    result(m is not None, "speaking → yeux du NAO animés (cognitive_renderer)")
    rb.publish(f"{drone}/speaking", {"data": False})
    m = rb.wait("/naoqi/leds", lambda m: led_group(m) == "AllLeds")
    result(m is not None, "fin de parole → NAO revient à son profil")

    # 3. image relayée telle quelle
    image = {"jpeg": base64.b64encode(b"\xff\xd8fake-jpeg\xff\xd9").decode(),
             "width": 2, "height": 2, "reason": "request"}
    rb.publish(f"{drone}/image", {"data": json.dumps(image)})
    m = rb.wait(f"{body}/sensors/image",
                lambda m: json.loads(m.get("data", "{}")).get("jpeg") == image["jpeg"])
    result(m is not None, f"/drones/{args.id}/image → {body}/sensors/image")

    # 4. /say → edge-tts → audio_out
    rb.publish(f"{body}/actuators/say", {"data": args.text})
    m = rb.wait(f"{drone}/audio_out", lambda m: bool(m.get("data")), timeout=30)
    mp3_b64 = m.get("data") if m else ""
    result(m is not None, f"{body}/actuators/say → edge-tts → /drones/{args.id}/audio_out",
           f"{len(base64.b64decode(mp3_b64))} octets MP3" if m else "edge-tts injoignable ?")
    if not mp3_b64:
        print(f"        {CYAN}Pas d'audio de test : étapes 5 et 6 ignorées{NC}")
        rb.ws.close()
        return failures

    # 5. audio entendu → direction, tête du NAO, transcription
    rb.drain(f"{drone}/audio_out")
    audio = {"wav": mp3_b64, "azimuth_deg": args.azimuth, "direction_source": "respeaker"}
    rb.publish(f"{drone}/audio", {"data": json.dumps(audio)})

    m = rb.wait(f"{body}/sensors/sound_direction",
                lambda m: json.loads(m.get("data", "{}")).get("azimuth_deg") == args.azimuth)
    result(m is not None, f"/drones/{args.id}/audio → {body}/sensors/sound_direction",
           m.get("data", "") if m else "")

    m = rb.wait(f"/bodies/{args.nao}/actuators/motion",
                lambda m: json.loads(m.get("data", "{}")).get("joint") == "HeadYaw")
    result(m is not None, f"body-gateway tourne la tête de {args.nao} vers la voix",
           m.get("data", "") if m else "body-gateway démarré avec ce drone dans BODY_REGISTRY ?")

    m = rb.wait(f"{body}/sensors/speech", timeout=60)
    result(m is not None, f"Whisper → {body}/sensors/speech",
           repr(m.get("data")) if m else "WHISPER_URL configuré dans nao-core ?")

    # 6. réponse du LLM prononcée par le drone statique
    m = rb.wait(f"{drone}/audio_out", lambda m: bool(m.get("data")), timeout=args.llm_timeout)
    result(m is not None, f"réponse du LLM prononcée par {args.id} (/drones/{args.id}/audio_out)",
           "" if m else "mot de réveil absent de la transcription, ou LLM trop lent ?")

    rb.ws.close()
    return failures


if __name__ == "__main__":
    sys.exit(main())
