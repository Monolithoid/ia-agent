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
  GET  /events           → Server-Sent Events stream (speech, sonar, bumper, battery)
  GET  /health           → {"ok": true}

The qi SDK requires the NAOqi Python SDK (Python 2.7) installed.
Mount the SDK directory at /opt/naoqi-sdk and set PYTHONPATH accordingly,
or install via the Makefile target provided in the README.

Environment variables:
  NAO_IP      IP address of the NAO robot (required)
  NAO_PORT    NAOqi port, default 9559
  BRIDGE_PORT HTTP port to listen on, default 8090
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

NAO_IP = os.environ.get("NAO_IP", "")
NAO_PORT = int(os.environ.get("NAO_PORT", "9559"))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8090"))

app = Flask(__name__)

# Global qi session and services (initialised once on startup)
_session = None
_tts = None
_motion = None
_leds = None
_posture = None
_battery = None
_memory = None
_sonar = None

# Thread-safe queue of NAO events to stream to subscribers
_event_queues = []
_event_queues_lock = threading.Lock()

# ──────────────────────────────────────────────────────────────────────────────
# Initialisation
# ──────────────────────────────────────────────────────────────────────────────

def connect_nao():
    global _session, _tts, _motion, _leds, _posture, _battery, _memory, _sonar

    if not HAS_QI:
        print("[naoqi_bridge] MOCK mode active — no NAO connection.", file=sys.stderr)
        return

    if not NAO_IP:
        print("[naoqi_bridge] NAO_IP not set — skipping connection.", file=sys.stderr)
        return

    url = "tcp://{}:{}".format(NAO_IP, NAO_PORT)
    print("[naoqi_bridge] Connecting to {}".format(url), file=sys.stderr)

    try:
        _session = qi.Session()
        _session.connect(url)

        _tts = _session.service("ALTextToSpeech")
        _motion = _session.service("ALMotion")
        _leds = _session.service("ALLeds")
        _posture = _session.service("ALRobotPosture")
        _battery = _session.service("ALBattery")
        _memory = _session.service("ALMemory")

        try:
            _sonar = _session.service("ALSonar")
            _sonar.subscribe("naoqi_bridge")
        except Exception as e:
            print("[naoqi_bridge] Sonar unavailable: {}".format(e), file=sys.stderr)

        _subscribe_events()
        _start_battery_poll()
        print("[naoqi_bridge] Connected to NAO at {}".format(NAO_IP), file=sys.stderr)

    except Exception as e:
        print("[naoqi_bridge] Connection failed: {}".format(e), file=sys.stderr)


def _broadcast(event):
    """Push an event dict to all SSE subscriber queues."""
    data = json.dumps(event)
    with _event_queues_lock:
        for q in list(_event_queues):
            try:
                q.put_nowait(data)
            except Queue.Full:
                pass  # slow consumer — drop event


def _subscribe_events():
    """Register NAOqi callbacks that forward events to SSE subscribers."""

    def on_word_recognized(value):
        if value and len(value) >= 2 and value[1] > 0.4:
            _broadcast({"topic": "speech_recognized", "data": value[0], "confidence": value[1]})

    def on_bumper_right(value):
        _broadcast({"topic": "bumper", "side": "right", "data": bool(value > 0.5)})

    def on_bumper_left(value):
        _broadcast({"topic": "bumper", "side": "left", "data": bool(value > 0.5)})

    def on_sonar_front(value):
        _broadcast({"topic": "sonar", "sensor": "front", "data": float(value)})

    try:
        sub_speech = _memory.subscriber("WordRecognized")
        sub_speech.signal.connect(on_word_recognized)
        app._sub_speech = sub_speech  # prevent GC

        sub_bumper_r = _memory.subscriber("RightBumperPressed")
        sub_bumper_r.signal.connect(on_bumper_right)
        app._sub_bumper_r = sub_bumper_r

        sub_bumper_l = _memory.subscriber("LeftBumperPressed")
        sub_bumper_l.signal.connect(on_bumper_left)
        app._sub_bumper_l = sub_bumper_l

        sub_sonar = _memory.subscriber("Device/SubDeviceList/US/Front/Sensor/Value")
        sub_sonar.signal.connect(on_sonar_front)
        app._sub_sonar = sub_sonar

    except Exception as e:
        print("[naoqi_bridge] Event subscription error: {}".format(e), file=sys.stderr)


def _start_battery_poll():
    """Poll battery level every 30 s and broadcast it."""

    def poll():
        while True:
            try:
                if _battery:
                    level = _battery.getBatteryCharge()
                    _broadcast({"topic": "battery", "data": int(level)})
            except Exception as e:
                print("[naoqi_bridge] Battery poll error: {}".format(e), file=sys.stderr)
            time.sleep(30)

    t = threading.Thread(target=poll)
    t.daemon = True
    t.start()


# ──────────────────────────────────────────────────────────────────────────────
# Routes — commands (VPS → NAO)
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"ok": True, "nao_ip": NAO_IP, "connected": _session is not None})


@app.route("/say", methods=["POST"])
def say():
    text = request.json.get("text", "") if request.json else ""
    if not text:
        return jsonify({"error": "missing text"}), 400

    if _tts:
        try:
            _tts.say(text.encode("utf-8"))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        print("[MOCK] say: {}".format(text), file=sys.stderr)

    return jsonify({"ok": True})


@app.route("/motion", methods=["POST"])
def motion():
    data = request.json or {}
    joint = data.get("joint", "HeadYaw")
    angle = float(data.get("angle", 0.0))
    speed = float(data.get("speed", 0.1))

    if _motion:
        try:
            _motion.setStiffnesses("Head", 1.0)
            _motion.angleInterpolationWithSpeed([str(joint)], [angle], speed, True)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        print("[MOCK] motion: {} → {:.2f} rad".format(joint, angle), file=sys.stderr)

    return jsonify({"ok": True})


@app.route("/leds", methods=["POST"])
def leds():
    data = request.json or {}
    group = str(data.get("group", "AllLeds"))
    r = float(data.get("r", 0.0))
    g = float(data.get("g", 0.0))
    b = float(data.get("b", 0.0))
    duration = float(data.get("duration", 0.5))

    if _leds:
        try:
            _leds.fadeRGB(group, r, g, b, duration)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        print("[MOCK] leds: {} rgb({:.2f},{:.2f},{:.2f}) {}s".format(group, r, g, b, duration), file=sys.stderr)

    return jsonify({"ok": True})


@app.route("/posture", methods=["POST"])
def posture():
    data = request.json or {}
    posture_name = str(data.get("posture", "Stand"))
    speed = float(data.get("speed", 0.5))

    if _posture:
        try:
            _posture.goToPosture(posture_name, speed)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        print("[MOCK] posture: {} speed={}".format(posture_name, speed), file=sys.stderr)

    return jsonify({"ok": True})


@app.route("/move", methods=["POST"])
def move():
    data = request.json or {}
    x = float(data.get("x", 0.0))
    y = float(data.get("y", 0.0))
    theta = float(data.get("theta", 0.0))

    if _motion:
        try:
            _motion.moveToward(x, y, theta)
        except Exception as e:
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
            # Keepalive every 15 s to prevent proxy timeouts
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
    connect_nao()
    app.run(host="0.0.0.0", port=BRIDGE_PORT, threaded=True)
