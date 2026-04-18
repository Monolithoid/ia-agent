#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
nao_ws_bridge.py — Bridge NAOqi <-> ROS 2 via rosbridge WebSocket
Runs on the NAO robot (Python 2.7 + NAOqi 2.x SDK).

Usage:
    python nao_ws_bridge.py --vps ws://VPS_IP:9090

Install websocket-client on NAO first:
    pip install websocket-client==0.59.0

The script:
  1. Connects to local NAOqi daemon (localhost:9559)
  2. Opens an outbound WebSocket to the VPS rosbridge server
  3. Subscribes to /nao/* ROS topics published from the VPS
  4. Publishes /nao/status (battery, temperature) back to the VPS every 10 s
"""

from __future__ import print_function

import argparse
import json
import sys
import threading
import time

try:
    import qi
except ImportError:
    print("ERROR: qi module not found. Run this script on the NAO robot.")
    sys.exit(1)

try:
    import websocket
except ImportError:
    print("ERROR: websocket-client not installed.")
    print("       Run on NAO: pip install websocket-client==0.59.0")
    sys.exit(1)


SUBSCRIBED_TOPICS = [
    "/nao/cmd_vel",   # geometry_msgs/Twist  — velocity control
    "/nao/speech",    # std_msgs/String       — text-to-speech
    "/nao/gesture",   # std_msgs/String       — posture name
    "/nao/leds",      # std_msgs/String       — JSON {name,r,g,b}
    "/nao/stop",      # std_msgs/Empty        — emergency stop
]

STATUS_TOPIC   = "/nao/status"     # std_msgs/String (JSON)
STATUS_PERIOD  = 10                # seconds between status publishes


class NAOBridge(object):

    def __init__(self, vps_url, naoqi_ip="localhost", naoqi_port=9559):
        self.vps_url       = vps_url
        self.ws            = None
        self.connected     = False
        self._reconnect_delay = 2
        self._stop_event   = threading.Event()

        # Connect to local NAOqi daemon
        self.session = qi.Session()
        self.session.connect("tcp://{}:{}".format(naoqi_ip, naoqi_port))

        self.motion  = self.session.service("ALMotion")
        self.tts     = self.session.service("ALTextToSpeech")
        self.posture = self.session.service("ALRobotPosture")
        self.leds    = self.session.service("ALLeds")
        self.memory  = self.session.service("ALMemory")
        self.battery = self.session.service("ALBattery")

        # Wake NAO up (stiffness on) if not already
        self.motion.wakeUp()

        print("[Bridge] NAOqi connected at {}:{}".format(naoqi_ip, naoqi_port))

    # ------------------------------------------------------------------
    # WebSocket callbacks
    # ------------------------------------------------------------------

    def _on_open(self, ws):
        self.connected        = True
        self._reconnect_delay = 2
        print("[Bridge] Connected to VPS rosbridge: {}".format(self.vps_url))

        for topic in SUBSCRIBED_TOPICS:
            self._subscribe(topic)

        self._advertise(STATUS_TOPIC, "std_msgs/String")
        self._start_status_thread()

    def _on_message(self, ws, raw):
        try:
            data = json.loads(raw)
            op   = data.get("op")
            if op == "publish":
                self._dispatch(data.get("topic", ""), data.get("msg", {}))
        except Exception as exc:
            print("[Bridge] Message error: {}".format(exc))

    def _on_error(self, ws, error):
        print("[Bridge] WebSocket error: {}".format(error))
        self.connected = False

    def _on_close(self, ws, code, msg):
        self.connected = False
        if not self._stop_event.is_set():
            print("[Bridge] Disconnected — retry in {}s".format(self._reconnect_delay))
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)
            self._run_once()

    # ------------------------------------------------------------------
    # rosbridge protocol helpers
    # ------------------------------------------------------------------

    def _send(self, payload):
        if self.connected and self.ws:
            self.ws.send(json.dumps(payload))

    def _subscribe(self, topic):
        self._send({"op": "subscribe", "topic": topic})

    def _advertise(self, topic, msg_type):
        self._send({"op": "advertise", "topic": topic, "type": msg_type})

    # ------------------------------------------------------------------
    # ROS -> NAOqi dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, topic, msg):
        try:
            if topic == "/nao/cmd_vel":
                lx = float(msg.get("linear",  {}).get("x", 0.0))
                ly = float(msg.get("linear",  {}).get("y", 0.0))
                az = float(msg.get("angular", {}).get("z", 0.0))
                # moveToward is non-blocking; clamp to [-1, 1]
                lx = max(-1.0, min(1.0, lx))
                ly = max(-1.0, min(1.0, ly))
                az = max(-1.0, min(1.0, az))
                self.motion.moveToward(lx, ly, az)

            elif topic == "/nao/speech":
                text = msg.get("data", "").strip()
                if text:
                    self.tts.say(str(text))

            elif topic == "/nao/gesture":
                posture = msg.get("data", "Stand").strip()
                self.posture.goToPosture(str(posture), 0.5)

            elif topic == "/nao/leds":
                led = json.loads(msg.get("data", "{}"))
                self.leds.fadeRGB(
                    str(led.get("name", "FaceLeds")),
                    float(led.get("r", 1.0)),
                    float(led.get("g", 1.0)),
                    float(led.get("b", 0.0)),
                    float(led.get("duration", 0.5)),
                )

            elif topic == "/nao/stop":
                self.motion.stopMove()

        except Exception as exc:
            print("[Bridge] Dispatch error on {}: {}".format(topic, exc))

    # ------------------------------------------------------------------
    # NAO -> ROS status publisher
    # ------------------------------------------------------------------

    def _start_status_thread(self):
        t = threading.Thread(target=self._status_loop)
        t.daemon = True
        t.start()

    def _status_loop(self):
        while self.connected and not self._stop_event.is_set():
            try:
                battery_pct = self.battery.getBatteryCharge()
                temps       = self.memory.getData(
                    "Device/SubDeviceList/CPU/Temperature/Sensor/Value"
                )
                status = {
                    "battery_pct": int(battery_pct),
                    "cpu_temp_c":  round(float(temps), 1),
                    "ts":          time.time(),
                }
                self._send({
                    "op":    "publish",
                    "topic": STATUS_TOPIC,
                    "msg":   {"data": json.dumps(status)},
                })
            except Exception as exc:
                print("[Bridge] Status error: {}".format(exc))
            time.sleep(STATUS_PERIOD)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _run_once(self):
        self.ws = websocket.WebSocketApp(
            self.vps_url,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        self.ws.run_forever(ping_interval=30, ping_timeout=10)

    def run(self):
        print("[Bridge] Connecting to {}".format(self.vps_url))
        try:
            self._run_once()
        except KeyboardInterrupt:
            print("[Bridge] Stopped.")
            self._stop_event.set()
            self.motion.stopMove()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="NAO <-> VPS rosbridge WebSocket bridge"
    )
    parser.add_argument(
        "--vps", default="ws://localhost:9090",
        help="VPS rosbridge URL (default: ws://localhost:9090)",
    )
    parser.add_argument(
        "--naoqi-ip", default="localhost",
        help="NAOqi host on the robot (default: localhost)",
    )
    parser.add_argument(
        "--naoqi-port", type=int, default=9559,
        help="NAOqi port (default: 9559)",
    )
    args = parser.parse_args()

    bridge = NAOBridge(args.vps, args.naoqi_ip, args.naoqi_port)
    bridge.run()


if __name__ == "__main__":
    main()
