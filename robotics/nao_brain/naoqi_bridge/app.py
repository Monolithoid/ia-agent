#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
naoqi_bridge — Python 2.7 HTTP bridge between the VPS and body stack.

Exposes:
  POST /say              {"text": "..."}
  POST /motion           {"joint": "...", "angle": 0.0, "speed": 0.1}
  POST /leds             {"group": "AllLeds", "r": 0.0, "g": 0.0, "b": 1.0, "duration": 0.5}
  POST /posture          {"posture": "Stand", "speed": 0.5}
  POST /move             {"x": 0.0, "y": 0.0, "theta": 0.0}
  POST /move/stop
  GET  /battery          → {"level": 85}
  GET  /events           → Server-Sent Events stream (sonar, bumper, battery)
  GET  /health           → {"ok": true, "connected": true/false}
  POST /connected        {"connected": true/false}  — called by naoqi_driver

Environment variables:
  BRIDGE_PORT         HTTP port to listen on, default 8090
  RECONNECT_INTERVAL  Seconds between reconnection attempts, default 15
  ROSBRIDGE_URL       ws:// URL of rosbridge (e.g. ws://rosbridge-proxy:80)
                      When set and connected=true, commands are forwarded to
                      the real NAO via rosbridge instead of being mocked.
"""

from __future__ import print_function

import base64
import hashlib
import json
import os
import Queue
import struct
import sys
import threading
import time

from flask import Flask, Response, jsonify, request

BRIDGE_PORT        = int(os.environ.get("BRIDGE_PORT", "8090"))
RECONNECT_INTERVAL = int(os.environ.get("RECONNECT_INTERVAL", "15"))
ROSBRIDGE_URL      = os.environ.get("ROSBRIDGE_URL", "")


def _mock_reason():
    return "robot_initiated_ws_only"

app = Flask(__name__)

# ── Connection state ──────────────────────────────────────────────────────────
# All writes to these globals are serialised through _conn_lock.
# Reads in route handlers are lock-free (GIL + Python attribute read atomicity).
_conn_lock = threading.Lock()
_session  = None
_tts      = None
_motion   = None
_leds     = None
_posture  = None
_battery  = None
_memory   = None
_sonar    = None

# Set to True by POST /connected when naoqi_driver detects NAO via rosbridge.
_ws_connected = False

# Battery poll thread is started once and keeps running (checks _battery each iteration).
_battery_poll_started = False

# ── SSE event bus ─────────────────────────────────────────────────────────────
_event_queues      = []
_event_queues_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Rosbridge publish helper (pure Python 2.7 WebSocket client, send-only)
# ──────────────────────────────────────────────────────────────────────────────

def _rosbridge_publish(topic, msg_type, msg):
    """
    Open a WebSocket connection to rosbridge, advertise topic, publish msg,
    then close.  Fire-and-forget — caller does not wait for a reply.

    Returns True on success, False on any error (bridge falls back to mock log).
    """
    if not ROSBRIDGE_URL or not ROSBRIDGE_URL.startswith("ws://"):
        return False

    try:
        tail = ROSBRIDGE_URL[5:]
        if "/" in tail:
            host_port, path_rest = tail.split("/", 1)
            path = "/" + path_rest
        else:
            host_port, path = tail, "/"

        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        else:
            host, port = host_port, 80

        import socket as _socket
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((host, port))

        raw_key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            "GET {path} HTTP/1.1\r\n"
            "Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).format(path=path, host=host, port=port, key=raw_key)
        sock.sendall(handshake.encode("ascii"))

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise IOError("Connection closed during WS handshake")
            resp += chunk
        if b"101" not in resp.split(b"\r\n")[0]:
            sock.close()
            raise IOError("WS handshake failed: {}".format(resp.split(b"\r\n")[0]))

        def _send_frame(text):
            if isinstance(text, unicode):
                text = text.encode("utf-8")
            payload = bytearray(text)
            n = len(payload)
            mask_key = bytearray(os.urandom(4))
            hdr = bytearray([0x81])  # FIN + text opcode
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
            sock.sendall(bytes(hdr) + bytes(masked))

        _send_frame(json.dumps({"op": "advertise", "topic": topic, "type": msg_type}))
        _send_frame(json.dumps({"op": "publish",   "topic": topic, "msg":  msg}))
        sock.close()
        return True

    except Exception as e:
        print("[bridge] rosbridge_publish error ({}): {}".format(topic, e), file=sys.stderr)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Connection management
# ──────────────────────────────────────────────────────────────────────────────

def _disconnect():
    """Clear all service proxies."""
    global _session, _tts, _motion, _leds, _posture, _battery, _memory, _sonar
    with _conn_lock:
        _session = _tts = _motion = _leds = _posture = _battery = _memory = _sonar = None
    # Drop stale event subscriptions (old memory proxy is dead)
    for attr in ("_sub_bumper_r", "_sub_bumper_l", "_sub_sonar"):
        try:
            delattr(app, attr)
        except AttributeError:
            pass


def _reconnect_loop():
    """Daemon thread kept for backward compatibility (outbound robots only)."""
    has_logged_mode = False
    while True:
        if not has_logged_mode:
            print("[naoqi_bridge] Running in outbound-robot mode (no direct IP/PORT connect).",
                  file=sys.stderr)
            has_logged_mode = True
        time.sleep(RECONNECT_INTERVAL)


def start_reconnect_loop():
    t = threading.Thread(target=_reconnect_loop)
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
            except Queue.Full:
                pass


def _subscribe_events():
    """Register NAOqi memory callbacks (called after each successful connect)."""

    def on_bumper_right(value):
        _broadcast({"topic": "bumper", "side": "right", "data": bool(value > 0.5)})

    def on_bumper_left(value):
        _broadcast({"topic": "bumper", "side": "left", "data": bool(value > 0.5)})

    def on_sonar_front(value):
        _broadcast({"topic": "sonar", "sensor": "front", "data": float(value)})

    try:
        sub_r = _memory.subscriber("RightBumperPressed")
        sub_r.signal.connect(on_bumper_right)
        app._sub_bumper_r = sub_r

        sub_l = _memory.subscriber("LeftBumperPressed")
        sub_l.signal.connect(on_bumper_left)
        app._sub_bumper_l = sub_l

        sub_s = _memory.subscriber("Device/SubDeviceList/US/Front/Sensor/Value")
        sub_s.signal.connect(on_sonar_front)
        app._sub_sonar = sub_s

    except Exception as e:
        print("[naoqi_bridge] Event subscription error: {}".format(e), file=sys.stderr)


def _start_battery_poll():
    """Poll battery every 30 s. Detects disconnection and triggers reconnect."""

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
# Routes — commands (VPS → NAO)
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    with _conn_lock:
        connected = (_session is not None) or _ws_connected
    return jsonify({
        "ok": True,
        "mode": "robot_initiated_ws",
        "connected": connected,
        "mock": not connected,
        "mock_reason": _mock_reason() if not connected else "",
    })


@app.route("/connected", methods=["POST"])
def set_connected():
    """Called by naoqi_driver when NAO connection state changes via rosbridge."""
    global _ws_connected
    data = request.json or {}
    state = bool(data.get("connected", False))
    with _conn_lock:
        _ws_connected = state
    if state:
        print("[naoqi_bridge] NAO connected via rosbridge"
              + (" — forwarding to {}".format(ROSBRIDGE_URL) if ROSBRIDGE_URL else " (no ROSBRIDGE_URL, mock only)"),
              file=sys.stderr)
    else:
        print("[naoqi_bridge] NAO disconnected", file=sys.stderr)
    return jsonify({"ok": True, "connected": state})


@app.route("/say", methods=["POST"])
def say():
    text = request.json.get("text", "") if request.json else ""
    if not text:
        return jsonify({"error": "missing text"}), 400

    if _tts:
        try:
            _tts.say(text.encode("utf-8"))
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    elif _ws_connected:
        ok = _rosbridge_publish(
            "/nao/speech", "std_msgs/String",
            {"data": text.encode("utf-8") if isinstance(text, unicode) else text},
        )
        if not ok:
            return jsonify({"error": "rosbridge unreachable"}), 503
    else:
        sys.stderr.write("[MOCK] say: {}\n".format(
            text.encode("utf-8") if isinstance(text, unicode) else text))

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
            _motion.angleInterpolationWithSpeed([str(joint)], [angle], speed, True)
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    elif _ws_connected:
        ok = _rosbridge_publish(
            "/nao/motion", "std_msgs/String",
            {"data": json.dumps({"joint": str(joint), "angle": angle, "speed": speed})},
        )
        if not ok:
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
    elif _ws_connected:
        # nao_ws_bridge expects "name" (not "group") in the LED payload
        ok = _rosbridge_publish(
            "/nao/leds", "std_msgs/String",
            {"data": json.dumps({"name": group, "r": r, "g": g, "b": b, "duration": duration})},
        )
        if not ok:
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
    elif _ws_connected:
        # /nao/gesture carries the posture name as plain string data.
        # speed is not forwarded because nao_ws_bridge calls goToPosture(name, 0.5).
        ok = _rosbridge_publish(
            "/nao/gesture", "std_msgs/String",
            {"data": posture_name},
        )
        if not ok:
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
    elif _ws_connected:
        ok = _rosbridge_publish(
            "/nao/cmd_vel", "geometry_msgs/Twist",
            {
                "linear":  {"x": x,     "y": y,   "z": 0.0},
                "angular": {"x": 0.0,   "y": 0.0, "z": theta},
            },
        )
        if not ok:
            return jsonify({"error": "rosbridge unreachable"}), 503
    else:
        print("[MOCK] move: x={} y={} theta={}".format(x, y, theta), file=sys.stderr)

    return jsonify({"ok": True})


@app.route("/move/stop", methods=["POST"])
def move_stop():
    if _motion:
        try:
            _motion.stopMove()
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    elif _ws_connected:
        ok = _rosbridge_publish("/nao/stop", "std_msgs/Empty", {})
        if not ok:
            return jsonify({"error": "rosbridge unreachable"}), 503
    else:
        print("[MOCK] move: stop", file=sys.stderr)
    return jsonify({"ok": True})


@app.route("/battery")
def battery():
    if _battery:
        try:
            level = _battery.getBatteryCharge()
            return jsonify({"level": int(level)})
        except Exception as e:
            _disconnect()
            return jsonify({"error": str(e)}), 500
    return jsonify({"level": -1, "mock": True})


# ──────────────────────────────────────────────────────────────────────────────
# Routes — events (NAO → VPS, Server-Sent Events)
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/events")
def events():
    """SSE stream: each event is a JSON-encoded NAO sensor update."""
    q = Queue.Queue(maxsize=64)
    with _event_queues_lock:
        _event_queues.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=15)
                    yield "data: {}\n\n".format(data)
                except Queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _event_queues_lock:
                if q in _event_queues:
                    _event_queues.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_reconnect_loop()
    app.run(host="0.0.0.0", port=BRIDGE_PORT, threaded=True)
