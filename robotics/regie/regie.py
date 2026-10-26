#!/usr/bin/env python3
"""
regie — control-room page that shows, live, what flows between the components:
inputs (Open WebUI, Twitch chat, "ok nao" voice, sensors, dreams) → LLM decision
in body-gateway → actions routed to the bodies (NAO, static drone, OBS voice).

It connects outbound to rosbridge with ROSBRIDGE_TOKEN, like obs-agent, and
only reads ROS topics:

  /events           (String JSON)  ← body-gateway, format in body_gateway/events.py
  /cognitive_state  (String)       ← idle | thinking | responding | listening | …
  /emotion_state    (String)       ← curious | neutral | excited | uncertain

The page is /?token=REGIE_TOKEN.  It receives the last events at once, then
the new ones over Server-Sent Events.  Any other ROS tool (Lichtblick, rqt,
`ros2 topic echo /events`) reads the same topic: the page is only one viewer.

Environment variables:
  ROSBRIDGE_URL     default ws://rosbridge-proxy:80
  ROSBRIDGE_TOKEN   bearer token checked by rosbridge-proxy (required)
  REGIE_TOKEN       secret in the page URL (required)
  HTTP_PORT         page port, default 8097
  HISTORY           events kept for a page opened later, default 300
"""

import collections
import hmac
import json
import os
import pathlib
import queue
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROSBRIDGE_URL   = os.environ.get("ROSBRIDGE_URL", "ws://rosbridge-proxy:80")
ROSBRIDGE_TOKEN = os.environ.get("ROSBRIDGE_TOKEN", "")
REGIE_TOKEN     = os.environ.get("REGIE_TOKEN", "")
HTTP_PORT       = int(os.environ.get("HTTP_PORT", "8097"))
HISTORY         = int(os.environ.get("HISTORY", "300"))

PAGE_HTML = pathlib.Path(__file__).with_name("regie.html")


def log(text: str):
    print(f"[regie] {text}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# rosbridge client (outbound WebSocket, subscriptions only, automatic reconnection)
# ─────────────────────────────────────────────────────────────────────────────

class Rosbridge:
    """Minimal rosbridge JSON client; same protocol as obs-agent's, read-only."""

    def __init__(self, url: str, token: str, on_link):
        self.url = url
        self.token = token
        self._on_link = on_link
        self._subscribed: dict[str, tuple] = {}

    def subscribe(self, name: str, msg_type: str, callback):
        self._subscribed[name] = (msg_type, callback)

    def _on_open(self, ws):
        for name, (msg_type, _) in self._subscribed.items():
            ws.send(json.dumps({"op": "subscribe", "topic": name, "type": msg_type}))
        log(f"connecté à {self.url}")
        self._on_link(True)

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
            ws = websocket.WebSocketApp(
                self.url, header=header, on_open=self._on_open, on_message=self._on_message)
            started = time.monotonic()
            ws.run_forever(ping_interval=20, ping_timeout=10)
            self._on_link(False)
            if time.monotonic() - started > 60:
                delay = 1.0
            log(f"rosbridge déconnecté, nouvel essai dans {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


# ─────────────────────────────────────────────────────────────────────────────
# Hub: recent events + SSE clients
# ─────────────────────────────────────────────────────────────────────────────

class Hub:

    def __init__(self, keep: int):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._history: collections.deque = collections.deque(maxlen=keep)

    def subscribe(self) -> tuple[queue.Queue, list]:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.append(q)
            return q, list(self._history)

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def publish(self, event: dict, keep: bool = True):
        with self._lock:
            if keep:
                self._history.append(event)
            for q in self._clients:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass


HUB = Hub(HISTORY)
LINK = {"kind": "link", "t": 0.0, "up": False}


def on_link(up: bool):
    """rosbridge link state, shown on the page (sent first to a page opened later)."""
    LINK.update(t=round(time.time(), 3), up=up)
    HUB.publish(dict(LINK), keep=False)


def on_event(msg: dict):
    try:
        event = json.loads(msg.get("data") or "")
    except ValueError:
        return
    if isinstance(event, dict) and event.get("kind"):
        HUB.publish(event)


def on_state(kind: str):
    def callback(msg: dict):
        value = (msg.get("data") or "").strip()
        if value:
            HUB.publish({"kind": kind, "t": round(time.time(), 3), "id": "", "value": value})
    return callback


BRIDGE = Rosbridge(ROSBRIDGE_URL, ROSBRIDGE_TOKEN, on_link)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP: / (page), /stream (SSE), /ping
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/ping":
            self._send(200, json.dumps({"ok": True, "service": "regie",
                                        "viewers": HUB.client_count()}).encode(),
                       "application/json")
            return
        token = urllib.parse.parse_qs(url.query).get("token", [""])[0]
        if not hmac.compare_digest(token.encode(), REGIE_TOKEN.encode()):
            self._send(401, b"unauthorized", "text/plain")
            return
        if url.path in ("/", "/regie"):
            self._send(200, PAGE_HTML.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/stream":
            self._stream()
        else:
            self._send(404, b"not found", "text/plain")

    def _stream(self):
        q, history = HUB.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            history = [dict(LINK)] + history
            self.wfile.write(f"event: history\ndata: {json.dumps(history)}\n\n".encode())
            self.wfile.flush()
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
    if not REGIE_TOKEN or not ROSBRIDGE_TOKEN:
        log("ERREUR : REGIE_TOKEN et ROSBRIDGE_TOKEN sont obligatoires")
        sys.exit(1)
    BRIDGE.subscribe("/events", "std_msgs/String", on_event)
    BRIDGE.subscribe("/cognitive_state", "std_msgs/String", on_state("cognitive"))
    BRIDGE.subscribe("/emotion_state", "std_msgs/String", on_state("emotion"))
    threading.Thread(target=BRIDGE.run_forever, daemon=True).start()
    log(f"régie → :{HTTP_PORT}/?token=…  |  rosbridge → {ROSBRIDGE_URL}")
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
