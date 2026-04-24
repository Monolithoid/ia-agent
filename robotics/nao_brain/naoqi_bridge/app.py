#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
naoqi_bridge — Python 2.7 HTTP bridge between the VPS and NAO via qi SDK.

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

The qi SDK requires the NAOqi Python SDK (Python 2.7) installed.
Mount the SDK directory at /opt/naoqi-sdk and set PYTHONPATH accordingly.

Environment variables:
  NAO_IP              IP address of the NAO robot (required for real mode)
  NAO_PORT            NAOqi port, default 9559
  BRIDGE_PORT         HTTP port to listen on, default 8090
  RECONNECT_INTERVAL  Seconds between reconnection attempts, default 15
"""

from __future__ import print_function

import json
import os
import Queue
import sys
import threading
import time

from flask import Flask, Response, jsonify, request

try:
    import qi
    HAS_QI = True
except ImportError:
    HAS_QI = False
    print("[naoqi_bridge] WARNING: qi module not found — running in MOCK mode.", file=sys.stderr)

NAO_IP             = os.environ.get("NAO_IP", "")
NAO_PORT           = int(os.environ.get("NAO_PORT", "9559"))
BRIDGE_PORT        = int(os.environ.get("BRIDGE_PORT", "8090"))
RECONNECT_INTERVAL = int(os.environ.get("RECONNECT_INTERVAL", "15"))

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

# Battery poll thread is started once and keeps running (checks _battery each iteration).
_battery_poll_started = False

# ── SSE event bus ─────────────────────────────────────────────────────────────
_event_queues      = []
_event_queues_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Connection management
# ──────────────────────────────────────────────────────────────────────────────

def _disconnect():
    """Clear all service proxies. The reconnect loop will retry shortly."""
    global _session, _tts, _motion, _leds, _posture, _battery, _memory, _sonar
    with _conn_lock:
        if _session is not None:
            print("[naoqi_bridge] Connection lost — will retry in {}s.".format(
                RECONNECT_INTERVAL), file=sys.stderr)
        _session = _tts = _motion = _leds = _posture = _battery = _memory = _sonar = None
    # Drop stale event subscriptions (old memory proxy is dead)
    for attr in ("_sub_bumper_r", "_sub_bumper_l", "_sub_sonar"):
        try:
            delattr(app, attr)
        except AttributeError:
            pass


def _connect_once():
    """Try one connection attempt. Returns True on success."""
    global _session, _tts, _motion, _leds, _posture, _battery, _memory, _sonar
    global _battery_poll_started

    if not HAS_QI or not NAO_IP:
        return False

    url = "tcp://{}:{}".format(NAO_IP, NAO_PORT)
    print("[naoqi_bridge] Connecting to {}...".format(url), file=sys.stderr)
    try:
        session  = qi.Session()
        session.connect(url)
        tts      = session.service("ALTextToSpeech")
        motion   = session.service("ALMotion")
        leds     = session.service("ALLeds")
        posture  = session.service("ALRobotPosture")
        battery  = session.service("ALBattery")
        memory   = session.service("ALMemory")
        sonar    = None
        try:
            sonar = session.service("ALSonar")
            sonar.subscribe("naoqi_bridge")
        except Exception as e:
            print("[naoqi_bridge] Sonar unavailable: {}".format(e), file=sys.stderr)

        with _conn_lock:
            _session = session
            _tts     = tts
            _motion  = motion
            _leds    = leds
            _posture = posture
            _battery = battery
            _memory  = memory
            _sonar   = sonar

        _subscribe_events()

        if not _battery_poll_started:
            _start_battery_poll()
            _battery_poll_started = True

        print("[naoqi_bridge] Connected to NAO at {}.".format(NAO_IP), file=sys.stderr)
        return True

    except Exception as e:
        print("[naoqi_bridge] Connection attempt failed: {}".format(e), file=sys.stderr)
        return False


def _reconnect_loop():
    """Daemon thread: keep trying to connect whenever the session is lost."""
    if not HAS_QI or not NAO_IP:
        print("[naoqi_bridge] MOCK mode active — reconnect loop idle.", file=sys.stderr)
        return

    while True:
        with _conn_lock:
            connected = _session is not None
        if not connected:
            _connect_once()
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
        connected = _session is not None
    return jsonify({"ok": True, "nao_ip": NAO_IP, "connected": connected})


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
