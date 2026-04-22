#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
nao_ws_bridge.py — Script embarque sur le robot NAO (Python 2.7).

Etablit une connexion WebSocket SORTANTE vers rosbridge sur le VPS,
s'abonne aux topics de commande et publie le statut du robot.

Usage :
    python /home/nao/nao_ws_bridge.py --vps wss://rosbridge.mon-domain.com

Dependances :
    websocket-client==0.59.0 + six  (voir docs/nao.md pour l'installation)
    naoqi (disponible nativement sur le robot)
"""

from __future__ import print_function

import argparse
import json
import sys
import threading
import time
import urlparse

import websocket

try:
    import ssl as _ssl
    # On some NAO Python 2.7 builds, ssl imports but SSLSocket is None
    # because the underlying _ssl C extension is incomplete.
    if not getattr(_ssl, "SSLSocket", None):
        raise ImportError("ssl.SSLSocket unavailable")
    _HAS_SSL = True
except ImportError:
    _ssl = None
    _HAS_SSL = False

try:
    from naoqi import ALProxy
    HAS_NAOQI = True
except ImportError:
    HAS_NAOQI = False
    print("[bridge] WARNING: module naoqi introuvable — mode MOCK actif", file=sys.stderr)

NAO_IP = "127.0.0.1"
NAO_PORT = 9559
RECONNECT_DELAY = 5  # secondes entre deux tentatives de reconnexion
STATUS_INTERVAL = 30  # secondes entre deux publications de statut


def is_secure_websocket(url):
    parsed = urlparse.urlparse(url)
    return parsed.scheme == "wss"


def validate_ssl_support(vps_url):
    if is_secure_websocket(vps_url) and not _HAS_SSL:
        print(
            "[bridge] ERREUR: Python n'a pas le module ssl, impossible d'utiliser {}".format(vps_url),
            file=sys.stderr
        )
        print(
            "[bridge] Utilisez une URL ws:// ou installez un runtime Python/OpenSSL avec ssl.",
            file=sys.stderr
        )
        return False
    return True


def build_ssl_options(vps_url):
    ssl_options = {}
    if not is_secure_websocket(vps_url):
        return ssl_options
    if not _HAS_SSL:
        return ssl_options

    ssl_options["cert_reqs"] = _ssl.CERT_NONE
    ssl_options["check_hostname"] = False
    return ssl_options


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
        self.ssl_options = build_ssl_options(vps_url)

    # ------------------------------------------------------------------
    # Connexion / reconnexion
    # ------------------------------------------------------------------

    def run(self):
        if self.vps_url.startswith("wss://") and not _HAS_SSL:
            print("[bridge] ERREUR : module ssl Python introuvable — connexion wss:// impossible.", file=sys.stderr)
            print("[bridge] Causes : Python 2.7 compile sans OpenSSL ou bibliotheque _ssl.so manquante.", file=sys.stderr)
            print("[bridge] Solutions : voir docs/nao.md section Depannage (rubrique SSL).", file=sys.stderr)
            return
        sslopt = {"cert_reqs": _ssl.CERT_NONE} if _HAS_SSL and self.vps_url.startswith("wss://") else {}
        while not self._stop.is_set():
            print("[bridge] Connexion a {} ...".format(self.vps_url), file=sys.stderr)
            try:
                self.ws = websocket.WebSocketApp(
                    self.vps_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(sslopt=sslopt)
            except Exception as e:
                print("[bridge] Erreur WebSocket : {}".format(e), file=sys.stderr)

            if not self._stop.is_set():
                print("[bridge] Reconnexion dans {}s ...".format(RECONNECT_DELAY), file=sys.stderr)
                time.sleep(RECONNECT_DELAY)

    def stop(self):
        self._stop.set()
        if self.ws:
            self.ws.close()

    # ------------------------------------------------------------------
    # Evenements WebSocket
    # ------------------------------------------------------------------

    def _on_open(self, ws):
        print("[bridge] Connecte. Abonnement aux topics ...", file=sys.stderr)
        topics = [
            ("/nao/speech",  "std_msgs/String"),
            ("/nao/cmd_vel", "geometry_msgs/Twist"),
            ("/nao/gesture", "std_msgs/String"),
            ("/nao/leds",    "std_msgs/String"),
            ("/nao/stop",    "std_msgs/Empty"),
        ]
        for topic, msg_type in topics:
            ws.send(json.dumps({"op": "subscribe", "topic": topic, "type": msg_type}))

        ws.send(json.dumps({"op": "advertise", "topic": "/nao/status", "type": "std_msgs/String"}))
        print("[bridge] Pret.", file=sys.stderr)
        threading.Thread(target=self._status_loop).start()

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
        msg = str(error)
        if "SSLSocket" in msg or "NoneType" in msg:
            print(
                "[bridge] ERREUR SSL : ssl.SSLSocket indisponible sur ce Python 2.7 "
                "(extension _ssl.so incomplete).",
                file=sys.stderr,
            )
            print(
                "[bridge] Utilisez une URL ws:// non chiffree ou voir docs/nao.md "
                "section Depannage (rubrique SSL).",
                file=sys.stderr,
            )
        else:
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
    parser.add_argument("--vps", required=True, help="URL rosbridge (ex: wss://rosbridge.mon-domain.com)")
    parser.add_argument("--nao-ip", default="127.0.0.1", help="IP du NAO (defaut: 127.0.0.1)")
    parser.add_argument("--nao-port", type=int, default=9559)
    args = parser.parse_args()

    NAO_IP = args.nao_ip
    NAO_PORT = args.nao_port

    if not validate_ssl_support(args.vps):
        sys.exit(1)

    bridge = NaoBridge(args.vps)
    try:
        bridge.run()
    except KeyboardInterrupt:
        bridge.stop()
