#!/usr/bin/env python3
"""
regie — control-room page that shows, live, what flows between the components:
inputs (Open WebUI, Twitch chat, "ok nao" voice, sensors, dreams) → LLM decision
in body-gateway → actions routed to the bodies (NAO, static drone, OBS voice).

It connects outbound to rosbridge with ROSBRIDGE_TOKEN, like obs-agent, and
reads ROS topics:

  /events                 (String JSON)  ← body-gateway, format in body_gateway/events.py
  /cognitive_state        (String)       ← idle | thinking | responding | listening | …
  /emotion_state          (String)       ← curious | neutral | excited | uncertain
  /nao/calibration_state  (String JSON)  ← nao_ws_bridge: calibration, motor profile
  /nao/components_state   (String JSON)  ← nao_ws_bridge: NAOqi components on/off

The page is /?token=REGIE_TOKEN.  It receives the last events at once, then
the new ones over Server-Sent Events.  Any other ROS tool (Lichtblick, rqt,
`ros2 topic echo /events`) reads the same topic: the page is only one viewer.

The robot page /robot?token=REGIE_TOKEN is the only one that writes: it
calibrates the motor profile (rest pose, start pose, ranges, speed of the
head and each arm) and switches the NAOqi components.  Its buttons POST to
/command, published on two topics that nao_ws_bridge.py listens to:

  /nao/calibration        (String JSON)  → calibration step, {"op": ...}
  /nao/components         (String JSON)  → {"name": ..., "enabled": bool}

Environment variables:
  ROSBRIDGE_URL     default ws://rosbridge-proxy:80
  ROSBRIDGE_TOKEN   bearer token checked by rosbridge-proxy (required)
  REGIE_TOKEN       secret in the page URL (required)
  OBS_URL           obs-agent domain, e.g. obs.example.com (https:// added if missing, optional)
  OBS_TOKEN         secret in the overlay URL, same as obs-agent (optional)
  HTTP_PORT         page port, default 8097
  HISTORY           events kept for a page opened later, default 300
"""

import collections
import hmac
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

ROSBRIDGE_URL   = os.environ.get("ROSBRIDGE_URL", "ws://rosbridge-proxy:80")
ROSBRIDGE_TOKEN = os.environ.get("ROSBRIDGE_TOKEN", "")
REGIE_TOKEN     = os.environ.get("REGIE_TOKEN", "")
OBS_URL         = os.environ.get("OBS_URL", "").strip().rstrip("/")
if OBS_URL and "://" not in OBS_URL:
    OBS_URL = "https://" + OBS_URL   # "obs.example.com" alone would be a relative redirect
OBS_TOKEN       = os.environ.get("OBS_TOKEN", "")
HTTP_PORT       = int(os.environ.get("HTTP_PORT", "8097"))
HISTORY         = int(os.environ.get("HISTORY", "300"))

PAGE_HTML = pathlib.Path(__file__).with_name("regie.html")
ROBOT_HTML = pathlib.Path(__file__).with_name("robot.html")
# What the robot page may publish, and where.
COMMAND_TOPICS = {"calibration": "/nao/calibration", "components": "/nao/components"}


def log(text: str):
    print(f"[regie] {text}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# rosbridge client (outbound WebSocket, automatic reconnection)
# ─────────────────────────────────────────────────────────────────────────────

class Rosbridge:
    """Minimal rosbridge JSON client; same protocol as obs-agent's.  Publishes
    only on the topics advertised (the robot page commands)."""

    def __init__(self, url: str, token: str, on_link):
        self.url = url
        self.token = token
        self._on_link = on_link
        self._subscribed: dict[str, tuple] = {}
        self._advertised: list[str] = []
        self._ws = None

    def subscribe(self, name: str, msg_type: str, callback):
        self._subscribed[name] = (msg_type, callback)

    def advertise(self, name: str):
        self._advertised.append(name)

    def publish(self, name: str, data: str) -> bool:
        """std_msgs/String on an advertised topic; False when rosbridge is down."""
        ws = self._ws
        if ws is None or name not in self._advertised:
            return False
        try:
            ws.send(json.dumps({"op": "publish", "topic": name, "msg": {"data": data}}))
        except Exception as e:
            log(f"publication {name} impossible : {e}")
            return False
        return True

    def _on_open(self, ws):
        for name, (msg_type, _) in self._subscribed.items():
            ws.send(json.dumps({"op": "subscribe", "topic": name, "type": msg_type}))
        for name in self._advertised:
            ws.send(json.dumps({"op": "advertise", "topic": name, "type": "std_msgs/String"}))
        self._ws = ws
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
            self._ws = None
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
# Robot page: only the latest state of each kind (calibration, components).
ROBOT_HUB = Hub(0)
ROBOT_STATE: dict[str, dict] = {}
LINK = {"kind": "link", "t": 0.0, "up": False}


def on_link(up: bool):
    """rosbridge link state, shown on the pages (sent first to a page opened later)."""
    LINK.update(t=round(time.time(), 3), up=up)
    HUB.publish(dict(LINK), keep=False)
    ROBOT_HUB.publish(dict(LINK), keep=False)


def on_robot_state(kind: str):
    def callback(msg: dict):
        try:
            value = json.loads(msg.get("data") or "")
        except ValueError:
            return
        if isinstance(value, dict):
            event = {"kind": kind, "t": round(time.time(), 3), "value": value}
            ROBOT_STATE[kind] = event
            ROBOT_HUB.publish(event, keep=False)
    return callback


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
# HTTP: / (page), /robot, /obs (redirect), /stream (SSE), /ping
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
        elif url.path == "/robot":
            self._send(200, ROBOT_HTML.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/obs":
            if not OBS_URL or not OBS_TOKEN:
                self._send(404, "OBS_URL et OBS_TOKEN non configurés sur la régie".encode(),
                           "text/plain; charset=utf-8")
                return
            self.send_response(302)
            self.send_header("Location", OBS_URL + "/overlay?token="
                             + urllib.parse.quote(OBS_TOKEN, safe=""))
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif url.path == "/stream":
            self._stream(HUB, [])
        elif url.path == "/robot-stream":
            self._stream(ROBOT_HUB, list(ROBOT_STATE.values()))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        """/command : {"target": "calibration"|"components", "msg": {...}} → rosbridge."""
        url = urllib.parse.urlsplit(self.path)
        token = urllib.parse.parse_qs(url.query).get("token", [""])[0]
        if not hmac.compare_digest(token.encode(), REGIE_TOKEN.encode()):
            self._send(401, b"unauthorized", "text/plain")
            return
        if url.path != "/command":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 10_000)
            body = json.loads(self.rfile.read(length) or b"{}")
            topic = COMMAND_TOPICS[body["target"]]
            msg = body["msg"]
            if not isinstance(msg, dict):
                raise TypeError("msg")
        except (KeyError, TypeError, ValueError):
            self._send(400, b'{"ok": false, "error": "commande invalide"}', "application/json")
            return
        ok = BRIDGE.publish(topic, json.dumps(msg))
        log(f"{topic} ← {json.dumps(msg)}" + ("" if ok else " (rosbridge coupé)"))
        self._send(200 if ok else 503,
                   json.dumps({"ok": ok, "error": "" if ok else "rosbridge coupé"}).encode(),
                   "application/json")

    def _stream(self, hub: Hub, state: list):
        q, history = hub.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            history = [dict(LINK)] + state + history
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
            hub.unsubscribe(q)

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
    if not REGIE_TOKEN or not ROSBRIDGE_TOKEN:
        log("ERREUR : REGIE_TOKEN et ROSBRIDGE_TOKEN sont obligatoires")
        sys.exit(1)
    BRIDGE.subscribe("/events", "std_msgs/String", on_event)
    BRIDGE.subscribe("/cognitive_state", "std_msgs/String", on_state("cognitive"))
    BRIDGE.subscribe("/emotion_state", "std_msgs/String", on_state("emotion"))
    BRIDGE.subscribe("/nao/calibration_state", "std_msgs/String", on_robot_state("calibration"))
    BRIDGE.subscribe("/nao/components_state", "std_msgs/String", on_robot_state("components"))
    for topic in COMMAND_TOPICS.values():
        BRIDGE.advertise(topic)
    threading.Thread(target=BRIDGE.run_forever, daemon=True).start()
    log(f"régie → :{HTTP_PORT}/?token=…  |  rosbridge → {ROSBRIDGE_URL}")
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
