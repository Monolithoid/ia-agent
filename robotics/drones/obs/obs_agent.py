#!/usr/bin/env python3
"""
obs_agent — NAO's voice on the stream: a voice body like the static drone's
speaker, but its "speaker" is the OBS mixer (ADR 005).

It connects outbound to rosbridge with ROSBRIDGE_TOKEN, exactly like the
static drone (ADR 004), and uses the same topics, so static_driver (started a
second time with DRONE_ID=obs_01 in nao-core) does the edge-tts synthesis:

  /bodies/{id}/actuators/say  (String, text)        → caption
  /drones/{id}/audio_out      (String, MP3 base64)  → played by the overlay
  /drones/{id}/status         (String JSON, 30 s)   ← only while an overlay is open,
                                                      so speech is sent here only
                                                      when the stream can hear it
  /drones/{id}/speaking       (Bool)                ← overlay playback start/end,
                                                      the NAO animates meanwhile

The overlay is an OBS browser source: /overlay?token=OBS_TOKEN.  With "Control
audio via OBS" checked, the voice is its own source in the OBS mixer.

Environment variables:
  DRONE_ID          body identifier, default "obs_01"
  ROSBRIDGE_URL     default ws://rosbridge-proxy:80
  ROSBRIDGE_TOKEN   bearer token checked by rosbridge-proxy (required)
  OBS_TOKEN         secret in the overlay URL (required)
  HTTP_PORT         overlay port, default 8096
"""

import base64
import collections
import hmac
import itertools
import json
import os
import pathlib
import queue
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DRONE_ID        = os.environ.get("DRONE_ID", "obs_01")
ROSBRIDGE_URL   = os.environ.get("ROSBRIDGE_URL", "ws://rosbridge-proxy:80")
ROSBRIDGE_TOKEN = os.environ.get("ROSBRIDGE_TOKEN", "")
OBS_TOKEN       = os.environ.get("OBS_TOKEN", "")
HTTP_PORT       = int(os.environ.get("HTTP_PORT", "8096"))

STATUS_PERIOD_S = 30.0
CAPTION_MAX_AGE_S = 120.0
OVERLAY_HTML = pathlib.Path(__file__).with_name("overlay.html")


def log(text: str):
    print(f"[obs-agent] {text}", flush=True)


def topic(name: str) -> str:
    return f"/drones/{DRONE_ID}/{name}"


# ─────────────────────────────────────────────────────────────────────────────
# rosbridge client (outbound WebSocket, automatic reconnection)
# ─────────────────────────────────────────────────────────────────────────────

class Rosbridge:
    """Minimal rosbridge JSON client; same protocol as the static drone's."""

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self._advertised: dict[str, str] = {}
        self._subscribed: dict[str, tuple] = {}
        self._ws = None
        self._connected = threading.Event()
        self._send_lock = threading.Lock()

    def advertise(self, name: str, msg_type: str):
        self._advertised[name] = msg_type

    def subscribe(self, name: str, msg_type: str, callback):
        self._subscribed[name] = (msg_type, callback)

    def publish(self, name: str, msg: dict) -> bool:
        ws = self._ws
        if ws is None or not self._connected.is_set():
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps({"op": "publish", "topic": name, "msg": msg}))
            return True
        except Exception as e:
            log(f"envoi impossible ({name}) : {e}")
            return False

    def _on_open(self, ws):
        with self._send_lock:
            for name, msg_type in self._advertised.items():
                ws.send(json.dumps({"op": "advertise", "topic": name, "type": msg_type}))
            for name, (msg_type, _) in self._subscribed.items():
                ws.send(json.dumps({"op": "subscribe", "topic": name, "type": msg_type}))
        self._connected.set()
        log(f"connecté à {self.url}")

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        entry = self._subscribed.get(msg.get("topic")) if msg.get("op") == "publish" else None
        if entry:
            try:
                entry[1](msg.get("msg", {}))
            except Exception as e:
                log(f"erreur en traitant {msg.get('topic')} : {e}")

    def run_forever(self):
        import websocket   # websocket-client

        header = [f"Authorization: Bearer {self.token}"] if self.token else []
        delay = 1.0
        while True:
            self._ws = websocket.WebSocketApp(
                self.url, header=header, on_open=self._on_open, on_message=self._on_message,
                on_close=lambda *a: self._connected.clear())
            started = time.monotonic()
            self._ws.run_forever(ping_interval=20, ping_timeout=10)
            self._connected.clear()
            if time.monotonic() - started > 60:
                delay = 1.0
            log(f"rosbridge déconnecté, nouvel essai dans {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


# ─────────────────────────────────────────────────────────────────────────────
# Overlay hub: SSE clients, recent audio, captions
# ─────────────────────────────────────────────────────────────────────────────

class OverlayHub:

    def __init__(self, keep: int = 20):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._audio: dict[str, bytes] = {}
        self._keep = keep
        self._ids = itertools.count(1)
        self._captions: collections.deque = collections.deque(maxlen=20)   # (t, text)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=50)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def audio(self, audio_id: str) -> bytes | None:
        with self._lock:
            return self._audio.get(audio_id)

    def add_caption(self, text: str):
        with self._lock:
            self._captions.append((time.monotonic(), text))

    def _pop_caption(self) -> str:
        # Each "say" text is synthesized into one MP3, in the same order.
        now = time.monotonic()
        while self._captions:
            t, text = self._captions.popleft()
            if now - t <= CAPTION_MAX_AGE_S:
                return text
        return ""

    def publish_audio(self, mp3: bytes) -> str:
        audio_id = str(next(self._ids))
        with self._lock:
            self._audio[audio_id] = mp3
            while len(self._audio) > self._keep:
                self._audio.pop(next(iter(self._audio)))
            event = {"id": audio_id, "text": self._pop_caption()}
            for q in self._clients:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass
        return audio_id


HUB = OverlayHub()
BRIDGE = Rosbridge(ROSBRIDGE_URL, ROSBRIDGE_TOKEN)


def on_say(msg: dict):
    text = (msg.get("data") or "").strip()
    if text:
        HUB.add_caption(text)


def on_audio_out(msg: dict):
    b64 = msg.get("data", "")
    if not b64:
        return
    if HUB.client_count() == 0:
        # Still consumed so the next caption stays paired with its audio.
        log("voix reçue mais aucun overlay OBS ouvert — personne ne l'entend")
    HUB.publish_audio(base64.b64decode(b64))


def status_loop():
    """Heartbeat only while OBS shows the overlay: static_driver then marks
    this voice connected, and body-gateway sends it speech."""
    while True:
        overlays = HUB.client_count()
        if overlays:
            BRIDGE.publish(topic("status"), {"data": json.dumps({"overlays": overlays})})
        time.sleep(STATUS_PERIOD_S)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP: /overlay, /events (SSE), /audio/<id>.mp3, /speaking, /ping
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def _authorized(self, query: dict) -> bool:
        token = query.get("token", [""])[0]
        if hmac.compare_digest(token.encode(), OBS_TOKEN.encode()):
            return True
        self._send(401, b"unauthorized", "text/plain")
        return False

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/ping":
            self._send(200, json.dumps({"ok": True, "service": "obs-agent",
                                        "overlays": HUB.client_count()}).encode(),
                       "application/json")
            return
        if not self._authorized(query):
            return
        if url.path == "/overlay":
            self._send(200, OVERLAY_HTML.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/events":
            self._events()
        elif url.path.startswith("/audio/") and url.path.endswith(".mp3"):
            mp3 = HUB.audio(url.path[len("/audio/"):-len(".mp3")])
            if mp3 is None:
                self._send(404, b"not found", "text/plain")
            else:
                self._send(200, mp3, "audio/mpeg")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/speaking":
            self._send(404, b"not found", "text/plain")
            return
        if not self._authorized(urllib.parse.parse_qs(url.query)):
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            speaking = bool(json.loads(self.rfile.read(length)).get("speaking"))
        except (ValueError, AttributeError):
            self._send(400, b"bad request", "text/plain")
            return
        BRIDGE.publish(topic("speaking"), {"data": speaking})
        self._send(200, b'{"ok": true}', "application/json")

    def _events(self):
        q = HUB.subscribe()
        log(f"overlay OBS ouvert ({HUB.client_count()})")
        # Announce the voice at once rather than at the next heartbeat.
        BRIDGE.publish(topic("status"), {"data": json.dumps({"overlays": HUB.client_count()})})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                try:
                    payload = f"data: {json.dumps(q.get(timeout=15))}\n\n"
                except queue.Empty:
                    payload = ": keepalive\n\n"
                self.wfile.write(payload.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            HUB.unsubscribe(q)
            log(f"overlay OBS fermé ({HUB.client_count()})")

    def _send(self, code: int, body: bytes, content_type: str):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass


def main():
    # PID 1 in the container: without a handler, the kernel drops SIGTERM and
    # `docker stop` waits for its timeout before sending SIGKILL.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not OBS_TOKEN or not ROSBRIDGE_TOKEN:
        log("ERREUR : OBS_TOKEN et ROSBRIDGE_TOKEN sont obligatoires")
        sys.exit(1)
    BRIDGE.advertise(topic("status"), "std_msgs/String")
    BRIDGE.advertise(topic("speaking"), "std_msgs/Bool")
    BRIDGE.subscribe(f"/bodies/{DRONE_ID}/actuators/say", "std_msgs/String", on_say)
    BRIDGE.subscribe(topic("audio_out"), "std_msgs/String", on_audio_out)
    threading.Thread(target=BRIDGE.run_forever, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    log(f"[{DRONE_ID}] overlay OBS → :{HTTP_PORT}/overlay?token=…  |  rosbridge → {ROSBRIDGE_URL}")
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
