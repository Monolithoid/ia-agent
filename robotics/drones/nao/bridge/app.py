#!/usr/bin/env python3
"""
naoqi_bridge — Flask HTTP bridge between the VPS and the body stack.

Exposes:
  POST /say              {"text": "..."}
  POST /motion           {"joint": "...", "angle": 0.0, "speed": 0.1}
  POST /leds             {"group": "AllLeds", "r": 0.0, "g": 0.0, "b": 1.0, "duration": 0.5}
  POST /posture          {"posture": "Stand", "speed": 0.5}
  POST /move             {"x": 0.0, "y": 0.0, "theta": 0.0}
  POST /move/stop
  POST /stiffness        {"groups": ["head","larm","rarm"], "value": 1.0}
  GET  /battery          → {"level": 85}
  GET  /events           → Server-Sent Events stream (sonar, bumper, battery)
  GET  /health           → {"ok": true, "connected": true/false}

Environment variables:
  BRIDGE_PORT            HTTP port to listen on, default 8090
  RECONNECT_INTERVAL     Seconds between reconnection attempts, default 15
  ROSBRIDGE_URL          ws:// URL of rosbridge (e.g. ws://rosbridge-proxy:80)
  ROSBRIDGE_TOKEN        Token sent as "Authorization: Bearer ..." in the WS
                         handshake; required by rosbridge-proxy
                         When set, the bridge subscribes to /nao/status and
                         forwards commands to the real NAO via ROS topics.
  NAO_STATUS_TIMEOUT     Seconds without /nao/status before marking disconnected,
                         default 90
"""

import base64
import hashlib
import json
import os
import queue
import struct
import sys
import threading
import time

from flask import Flask, Response, jsonify, request

BRIDGE_PORT        = int(os.environ.get("BRIDGE_PORT", "8090"))
RECONNECT_INTERVAL = int(os.environ.get("RECONNECT_INTERVAL", "15"))
ROSBRIDGE_URL      = os.environ.get("ROSBRIDGE_URL", "")
ROSBRIDGE_TOKEN    = os.environ.get("ROSBRIDGE_TOKEN", "")
NAO_STATUS_TIMEOUT = float(os.environ.get("NAO_STATUS_TIMEOUT", "90"))

app = Flask(__name__)

# ── Legacy direct NAOqi proxies (unused in robot-initiated WebSocket mode) ────
_conn_lock = threading.Lock()
_session  = None
_tts      = None
_motion   = None
_leds     = None
_posture  = None
_battery  = None
_memory   = None
_sonar    = None

# ── Rosbridge connection state ────────────────────────────────────────────────
# Updated by the rosbridge monitor thread watching /nao/status heartbeats.
_last_nao_status_time = 0.0   # epoch timestamp of the last received /nao/status
_status_lock = threading.Lock()

# ── SSE event bus ─────────────────────────────────────────────────────────────
_event_queues      = []
_event_queues_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Minimal pure-Python 2.7 WebSocket client (RFC 6455, ws:// only)
# Used for both publishing commands and subscribing to /nao/status.
# ──────────────────────────────────────────────────────────────────────────────

class _WS(object):
    """Lightweight WebSocket client: connect, send text frames, recv text frames."""

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url):
        # TLS not supported here — VPS-internal calls go over plain ws://.
        if not url.startswith("ws://"):
            raise ValueError("Only ws:// supported")
        tail = url[5:]
        host_port, sep, path_rest = tail.partition("/")
        # Re-add leading slash stripped by partition; default to root path.
        self._path = "/" + path_rest if sep else "/"
        # Parse optional port from host:port notation.
        if ":" in host_port:
            self._host, port_str = host_port.rsplit(":", 1)
            self._port = int(port_str)
        else:
            self._host, self._port = host_port, 80
        self._sock = None

    def connect(self, timeout=10):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((self._host, self._port))
        s.settimeout(None)
        self._sock = s

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        # rosbridge-proxy only upgrades connections carrying the shared token.
        auth = ("Authorization: Bearer {}\r\n".format(ROSBRIDGE_TOKEN)
                if ROSBRIDGE_TOKEN else "")
        req = (
            "GET {path} HTTP/1.1\r\n"
            "Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "{auth}"
            "\r\n"
        ).format(path=self._path, host=self._host, port=self._port, key=key,
                 auth=auth)
        s.sendall(req.encode("ascii"))

        resp = b""
        # Accumulate HTTP response until the header terminator arrives.
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            # Server closed the connection before completing the HTTP upgrade.
            if not chunk:
                raise IOError("Connection closed during WS handshake")
            resp += chunk
        # HTTP 101 is the only valid response to an Upgrade: websocket request.
        if b"101" not in resp.split(b"\r\n")[0]:
            raise IOError("WS handshake failed: {}".format(resp.split(b"\r\n")[0]))

    def send(self, text):
        payload = bytearray(text.encode("utf-8"))
        n = len(payload)
        mask_key = bytearray(os.urandom(4))
        hdr = bytearray([0x81])
        # RFC 6455 §5.2 payload length encoding (same as nao_ws_bridge._send_frame).
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr.extend(struct.pack(">H", n))
        else:
            hdr.append(0x80 | 127)
            hdr.extend(struct.pack(">Q", n))
        hdr.extend(mask_key)
        masked = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(hdr) + bytes(masked))

    def recv(self):
        """Block until one text frame arrives. Returns decoded str."""
        def recv_exact(n):
            buf = b""
            while len(buf) < n:
                chunk = self._sock.recv(n - len(buf))
                if not chunk:
                    raise IOError("Connection closed")
                buf += chunk
            return buf

        while True:
            b0, b1 = bytearray(recv_exact(2))
            opcode   = b0 & 0x0F
            is_mask  = (b1 & 0x80) != 0
            length   = b1 & 0x7F
            # Decode extended payload lengths per RFC 6455 §5.2.
            if length == 126:
                length = struct.unpack(">H", recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", recv_exact(8))[0]
            # Server frames are typically unmasked; read key only if present.
            mask_key = bytearray(recv_exact(4)) if is_mask else None
            payload  = bytearray(recv_exact(length))
            # Unmask payload if a masking key was sent.
            if mask_key:
                for i in range(length):
                    payload[i] ^= mask_key[i % 4]
            if opcode == 0x1:   # text frame — the only type we expect from rosbridge
                return payload.decode("utf-8", "replace")
            elif opcode == 0x9: # ping → must reply with pong per RFC 6455
                self._send_pong(bytes(payload))
            elif opcode == 0x8: # close — server terminated the connection
                raise IOError("Server sent close frame")
            # 0x0 continuation, 0x2 binary: not used by rosbridge, ignore silently.

    def _send_pong(self, data):
        hdr = bytearray([0x8A, 0x80 | len(data)])
        mask_key = bytearray(os.urandom(4))
        hdr.extend(mask_key)
        masked = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(bytearray(data)))
        self._sock.sendall(bytes(hdr) + bytes(masked))

    def close(self):
        if self._sock:
            try:
                self._sock.sendall(b"\x88\x80" + os.urandom(4))  # close frame
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


# ──────────────────────────────────────────────────────────────────────────────
# Rosbridge monitor — subscribes to /nao/status, updates _last_nao_status_time
# ──────────────────────────────────────────────────────────────────────────────

def _rosbridge_monitor():
    """
    Daemon thread.  Connects to rosbridge, subscribes to /nao/status, and
    records the timestamp of each received heartbeat.  /health reads this
    timestamp to decide if the NAO is connected (no external notification needed).
    Reconnects automatically on any error.
    """
    # When ROSBRIDGE_URL is not configured the bridge operates in full MOCK mode
    # and NAO connection state is always "disconnected".
    if not ROSBRIDGE_URL:
        print("[naoqi_bridge] ROSBRIDGE_URL not set — connection monitor disabled",
              file=sys.stderr)
        return

    print("[naoqi_bridge] Starting rosbridge monitor → {}".format(ROSBRIDGE_URL),
          file=sys.stderr)

    while True:
        ws = None
        try:
            ws = _WS(ROSBRIDGE_URL)
            ws.connect(timeout=10)
            ws.send(json.dumps({
                "op":    "subscribe",
                "topic": "/nao/status",
                "type":  "std_msgs/String",
            }))
            print("[naoqi_bridge] Subscribed to /nao/status", file=sys.stderr)

            while True:
                raw = ws.recv()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                # Only process heartbeat publishes on the expected topic.
                if msg.get("op") == "publish" and msg.get("topic") == "/nao/status":
                    with _status_lock:
                        global _last_nao_status_time
                        _last_nao_status_time = time.time()

        except Exception as e:
            print("[naoqi_bridge] rosbridge monitor error: {} — retrying in {}s".format(
                e, RECONNECT_INTERVAL), file=sys.stderr)
        finally:
            if ws:
                ws.close()

        time.sleep(RECONNECT_INTERVAL)


def _is_nao_connected():
    """True if a /nao/status heartbeat was received within NAO_STATUS_TIMEOUT seconds.

    t > 0 guards against the initial state where no heartbeat has ever arrived —
    time.time() - 0 would be a large number and incorrectly appear "recent".
    """
    with _status_lock:
        t = _last_nao_status_time
    # t == 0.0 means no heartbeat received yet — robot is not connected.
    return t > 0 and (time.time() - t) < NAO_STATUS_TIMEOUT


# ──────────────────────────────────────────────────────────────────────────────
# Rosbridge publish helper — send one command to a ROS topic
# ──────────────────────────────────────────────────────────────────────────────

def _rosbridge_publish(topic, msg_type, msg):
    """Open a WS connection to rosbridge, publish one message, close."""
    if not ROSBRIDGE_URL:
        return False
    try:
        ws = _WS(ROSBRIDGE_URL)
        ws.connect(timeout=5)
        ws.send(json.dumps({"op": "advertise", "topic": topic, "type": msg_type}))
        ws.send(json.dumps({"op": "publish",   "topic": topic, "msg":  msg}))
        ws.close()
        return True
    except Exception as e:
        print("[naoqi_bridge] rosbridge_publish error ({}): {}".format(topic, e),
              file=sys.stderr)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Legacy NAOqi connection management (direct SDK, unused in robot_initiated_ws)
# ──────────────────────────────────────────────────────────────────────────────

def _disconnect():
    global _session, _tts, _motion, _leds, _posture, _battery, _memory, _sonar
    with _conn_lock:
        _session = _tts = _motion = _leds = _posture = _battery = _memory = _sonar = None
    for attr in ("_sub_bumper_r", "_sub_bumper_l", "_sub_sonar"):
        try:
            delattr(app, attr)
        except AttributeError:
            pass


def _start_battery_poll():
    def poll():
        while True:
            time.sleep(30)
            try:
                if _battery:
                    level = _battery.getBatteryCharge()
                    _broadcast({"topic": "battery", "data": int(level)})
            except Exception as e:
                print("[naoqi_bridge] Battery poll error: {}".format(e), file=sys.stderr)
                _disconnect()
    t = threading.Thread(target=poll)
    t.daemon = True
    t.start()


# ──────────────────────────────────────────────────────────────────────────────
# SSE helpers
# ──────────────────────────────────────────────────────────────────────────────

def _broadcast(event):
    data = json.dumps(event)
    with _event_queues_lock:
        for q in list(_event_queues):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    with _conn_lock:
        direct = _session is not None
    via_ws = _is_nao_connected()
    connected = direct or via_ws
    return jsonify({
        "ok":        True,
        "mode":      "robot_initiated_ws",
        "connected": connected,
        "mock":      not connected,
        "mock_reason": "" if connected else "robot_initiated_ws_only",
    })


@app.route("/say", methods=["POST"])
def say():
    """Speak text aloud on NAO via one of three dispatch paths:
      1. Direct NAOqi SDK (legacy — _tts proxy set up on connect)
      2. rosbridge WebSocket publish (robot-initiated WS mode)
      3. MOCK log (robot absent)
    """
    text = request.json.get("text", "") if request.json else ""
    # Guard: empty string would cause NAOqi TTS to error.
    if not text:
        return jsonify({"error": "missing text"}), 400

    if _tts:
        # Path 1: direct NAOqi proxy (legacy direct connection mode).
        try:
            _tts.say(text)
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    elif _is_nao_connected():
        # Path 2: robot is connected via rosbridge — forward over WebSocket.
        if not _rosbridge_publish(
            "/nao/speech", "std_msgs/String",
            {"data": text},
        ):
            return jsonify({"error": "rosbridge unreachable"}), 503
    else:
        # Path 3: robot offline — log as MOCK so tests can verify the call.
        sys.stderr.write("[MOCK] say: {}\n".format(
            text))

    return jsonify({"ok": True})


@app.route("/motion", methods=["POST"])
def motion():
    data  = request.json or {}
    joint = data.get("joint", "HeadYaw")
    angle = float(data.get("angle", 0.0))
    speed = float(data.get("speed", 0.1))

    if _motion:
        try:
            _motion.setStiffnesses("Head", 1.0)
            _motion.angleInterpolationWithSpeed([str(joint)], [angle], speed)
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    elif _is_nao_connected():
        if not _rosbridge_publish(
            "/nao/motion", "std_msgs/String",
            {"data": json.dumps({"joint": str(joint), "angle": angle, "speed": speed})},
        ):
            return jsonify({"error": "rosbridge unreachable"}), 503
    else:
        print("[MOCK] motion: {} -> {:.2f} rad".format(joint, angle), file=sys.stderr)

    return jsonify({"ok": True})


@app.route("/leds", methods=["POST"])
def leds():
    data     = request.json or {}
    group    = str(data.get("group", "AllLeds"))
    r        = float(data.get("r", 0.0))
    g        = float(data.get("g", 0.0))
    b        = float(data.get("b", 0.0))
    duration = float(data.get("duration", 0.5))

    if _leds:
        try:
            _leds.fadeRGB(group, r, g, b, duration)
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    elif _is_nao_connected():
        # nao_ws_bridge.py expects "name" key, not "group"
        if not _rosbridge_publish(
            "/nao/leds", "std_msgs/String",
            {"data": json.dumps({"name": group, "r": r, "g": g, "b": b, "duration": duration})},
        ):
            return jsonify({"error": "rosbridge unreachable"}), 503
    else:
        print("[MOCK] leds: {} rgb({:.2f},{:.2f},{:.2f}) {}s".format(
            group, r, g, b, duration), file=sys.stderr)

    return jsonify({"ok": True})


@app.route("/posture", methods=["POST"])
def posture():
    data         = request.json or {}
    posture_name = str(data.get("posture", "Stand"))
    speed        = float(data.get("speed", 0.5))

    if _posture:
        try:
            _posture.goToPosture(posture_name, speed)
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    elif _is_nao_connected():
        if not _rosbridge_publish(
            "/nao/gesture", "std_msgs/String",
            {"data": posture_name},
        ):
            return jsonify({"error": "rosbridge unreachable"}), 503
    else:
        print("[MOCK] posture: {} speed={}".format(posture_name, speed), file=sys.stderr)

    return jsonify({"ok": True})


@app.route("/move", methods=["POST"])
def move():
    data  = request.json or {}
    x     = float(data.get("x", 0.0))
    y     = float(data.get("y", 0.0))
    theta = float(data.get("theta", 0.0))

    if _motion:
        try:
            _motion.moveToward(x, y, theta)
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    elif _is_nao_connected():
        if not _rosbridge_publish(
            "/nao/cmd_vel", "geometry_msgs/Twist",
            {"linear":  {"x": x, "y": y, "z": 0.0},
             "angular": {"x": 0.0, "y": 0.0, "z": theta}},
        ):
            return jsonify({"error": "rosbridge unreachable"}), 503
    else:
        print("[MOCK] move: x={} y={} theta={}".format(x, y, theta), file=sys.stderr)

    return jsonify({"ok": True})


_NAOQI_GROUP = {
    "head": "Head",
    "larm": "LArm",
    "rarm": "RArm",
    "legs": "Legs",
}


@app.route("/stiffness", methods=["POST"])
def stiffness():
    data   = request.json or {}
    groups = data.get("groups", [])
    value  = float(data.get("value", 0.0))

    # Guard: single-string group would iterate over characters without this check.
    if not isinstance(groups, list):
        return jsonify({"error": "groups must be a list"}), 400

    errors = []
    for grp in groups:
        naoqi_group = _NAOQI_GROUP.get(str(grp).lower())
        # Unknown group key — record warning and continue with remaining groups.
        if not naoqi_group:
            errors.append("unknown group: {}".format(grp))
            continue
        if _motion:
            # Path 1: direct NAOqi ALMotion proxy.
            try:
                _motion.setStiffnesses(naoqi_group, value)
            except Exception as e:
                _disconnect()
                return jsonify({"error": str(e)}), 500
        elif _is_nao_connected():
            # Path 2: forward via rosbridge → nao_ws_bridge._set_stiffness.
            payload = json.dumps({"groups": [str(grp).lower()], "value": value})
            if not _rosbridge_publish(
                "/nao/stiffness", "std_msgs/String", {"data": payload}
            ):
                return jsonify({"error": "rosbridge unreachable"}), 503
        else:
            # Path 3: MOCK log.
            print("[MOCK] stiffness: {} ({}) -> {}".format(grp, naoqi_group, value),
                  file=sys.stderr)

    # Partial success: some groups were unknown but the known ones were applied.
    if errors:
        return jsonify({"ok": True, "warnings": errors})
    return jsonify({"ok": True})


@app.route("/move/stop", methods=["POST"])
def move_stop():
    if _motion:
        try:
            _motion.stopMove()
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    elif _is_nao_connected():
        if not _rosbridge_publish("/nao/stop", "std_msgs/Empty", {}):
            return jsonify({"error": "rosbridge unreachable"}), 503
    else:
        print("[MOCK] move: stop", file=sys.stderr)
    return jsonify({"ok": True})


@app.route("/battery")
def battery():
    """Return battery level from the NAOqi SDK if available, or -1 in mock mode."""
    if _battery:
        # Direct NAOqi read — only available in legacy direct SDK mode.
        try:
            level = _battery.getBatteryCharge()
            return jsonify({"level": int(level)})
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    # Battery unavailable — robot-initiated WS mode sends it via /nao/status instead.
    return jsonify({"level": -1, "mock": True})


@app.route("/events")
def events():
    q = queue.Queue(maxsize=64)
    with _event_queues_lock:
        _event_queues.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=15)
                    yield "data: {}\n\n".format(data)
                except queue.Empty:
                    # No events in 15 s — send an SSE keepalive comment to prevent
                    # the browser or proxy from closing the idle connection.
                    yield ": keepalive\n\n"
        finally:
            with _event_queues_lock:
                # Guard: client may have already been removed by a concurrent cleanup.
                if q in _event_queues:
                    _event_queues.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=_rosbridge_monitor)
    t.daemon = True
    t.start()
    app.run(host="0.0.0.0", port=BRIDGE_PORT, threaded=True)
