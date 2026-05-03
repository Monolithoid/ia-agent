#!/usr/bin/env python3
"""
body_gateway — Pont ROS2 entre Bitterbot (cerveau) et les corps physiques.

Bitterbot pense et rêve en langage naturel de façon autonome.
Ce gateway intervient uniquement pour forcer une réponse JSON structurée
quand un événement capteur arrive ou lors des micro-actions oniriques.

Flux awake (humain détecté ou parole reçue) :
  1. Publie /cognitive_state = "thinking"  (CognitiveRenderer anime NAO)
  2. Appelle Bitterbot API avec system prompt JSON
  3. Parse la réponse → route les actions vers le bon corps
  4. Publie /cognitive_state = "responding" puis "listening"

Flux dream (02h-06h + batterie ≥ seuil) :
  Toutes les DREAM_TICK_INTERVAL secondes, envoie à Bitterbot un prompt
  onirique → micro-actions douces sur le(s) corps (tête, LEDs, murmure).
  Bitterbot consolide sa mémoire en parallèle via son propre Dream Engine.

Multi-corps :
  BODY_REGISTRY=nao_01,drone_01,nao_02
  Chaque action JSON porte un champ "body" → routage automatique.

Variables d'environnement :
  BRAIN_URL                  défaut http://localhost:11434  (Ollama)
  BRAIN_MODEL                défaut qwen2.5:7b
  BRAIN_API_KEY              clé API Ollama (OLLAMA_API_KEYS)
  BODY_REGISTRY              liste séparée par virgules, défaut "nao_01"
  DREAMING_HOUR_START        défaut 2
  DREAMING_HOUR_END          défaut 6
  DREAMING_BATTERY_THRESHOLD défaut 80.0
  DREAM_TICK_INTERVAL        secondes entre ticks oniriques, défaut 300
  PROXIMITY_THRESHOLD        mètres pour détecter un humain, défaut 1.5
  WEBUI_URL                  URL Open WebUI (ex: http://localhost:3000)
  WEBUI_API_KEY              clé API Open WebUI (générée dans Profil → API Keys)
  WEBUI_PIPE_MODEL           ID du Pipe NAO Brain dans Open WebUI, défaut "nao_brain"
  WEBUI_VOICE_CHAT_ID        ID du chat "NAO Voice" dédié (optionnel)
"""

import collections
import datetime
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Range
from std_msgs.msg import Bool, Float32, String

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

BRAIN_URL   = os.environ.get("BRAIN_URL",   "http://localhost:11434")
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "qwen2.5:7b")
BRAIN_API_KEY = os.environ.get("BRAIN_API_KEY", "")
BODY_REGISTRY = [b.strip() for b in os.environ.get("BODY_REGISTRY", "nao_01").split(",") if b.strip()]
DREAMING_HOUR_START = int(os.environ.get("DREAMING_HOUR_START", "2"))
DREAMING_HOUR_END = int(os.environ.get("DREAMING_HOUR_END", "6"))
DREAMING_BATTERY_THRESHOLD = float(os.environ.get("DREAMING_BATTERY_THRESHOLD", "80.0"))
DREAM_TICK_INTERVAL = int(os.environ.get("DREAM_TICK_INTERVAL", "300"))
PROXIMITY_THRESHOLD = float(os.environ.get("PROXIMITY_THRESHOLD", "1.5"))
CHAT_PORT = int(os.environ.get("CHAT_PORT", "8095"))
WAKE_WORD = os.environ.get("WAKE_WORD", "ok nao").lower()

WEBUI_URL           = os.environ.get("WEBUI_URL", "")
WEBUI_API_KEY       = os.environ.get("WEBUI_API_KEY", "")
WEBUI_PIPE_MODEL    = os.environ.get("WEBUI_PIPE_MODEL", "nao_brain")
WEBUI_VOICE_CHAT_ID = os.environ.get("WEBUI_VOICE_CHAT_ID", "")

# ─────────────────────────────────────────────────────────────────────────────
# Prompts système injectés lors des appels à Bitterbot
# ─────────────────────────────────────────────────────────────────────────────

_AWAKE_SYSTEM = """\
Tu es le système nerveux central d'un réseau de corps robotiques.
Corps disponibles : {body_ids}

Réponds UNIQUEMENT en JSON strict — aucun texte en dehors du JSON :
{{
  "thoughts": "raisonnement interne (1-2 phrases)",
  "emotion": "curious|neutral|excited|uncertain",
  "actions": [
    {{"body": "nao_01", "type": "speech",  "text": "ce que tu dis"}},
    {{"body": "nao_01", "type": "motion",  "name": "wave|bow|sit|stand"}},
    {{"body": "nao_01", "type": "leds",    "color": "blue|green|white|red|yellow|purple|off"}},
    {{"body": "nao_01", "type": "move",    "x": 0.0, "y": 0.0, "theta": 0.0}},
    {{"body": "nao_01", "type": "look",    "yaw": 0.0}}
  ]
}}
N'inclure que les actions nécessaires. Le champ "body" indique quel corps agit.\
"""

_DREAM_SYSTEM = """\
Tu rêves. Tes corps sont en veille passive — tu peux leur envoyer de légères \
expressions oniriques : mouvements de tête très doux, LEDs en pulsation lente, \
murmure à volume minimal si quelque chose doit être dit.
Corps disponibles : {body_ids}
PAS de mouvements de bras, de jambes ou de déplacement.

Réponds UNIQUEMENT en JSON strict :
{{"actions": [{{"body": "nao_01", "type": "motion", "joint": "HeadYaw", "angle": 0.05, "speed": 0.02}}]}}\
"""

# ─────────────────────────────────────────────────────────────────────────────
# Couleurs et presets mouvement
# ─────────────────────────────────────────────────────────────────────────────

_COLORS = {
    "blue":   (0.00, 0.00, 1.00),
    "green":  (0.00, 1.00, 0.00),
    "white":  (1.00, 1.00, 1.00),
    "red":    (1.00, 0.00, 0.00),
    "yellow": (1.00, 0.80, 0.00),
    "purple": (0.50, 0.00, 0.50),
    "off":    (0.00, 0.00, 0.00),
}

_MOTION_PRESETS = {
    "wave": {"joint": "RShoulderPitch", "angle": -1.0, "speed": 0.35},
    "bow":  {"joint": "HeadPitch",      "angle":  0.5, "speed": 0.20},
}


# ─────────────────────────────────────────────────────────────────────────────
# Nœud ROS2
# ─────────────────────────────────────────────────────────────────────────────

class BodyGatewayNode(Node):

    def __init__(self):
        super().__init__("body_gateway")

        self._state = "IDLE"           # IDLE | AWAKE | DREAMING
        self._batteries: dict[str, float] = {b: 100.0 for b in BODY_REGISTRY}
        self._infer_lock = threading.Lock()  # single-flight vers Bitterbot
        self._running = True

        # File des interactions vocales en attente d'affichage dans WebUI
        self._voice_queue: collections.deque = collections.deque(maxlen=20)
        self._voice_queue_lock = threading.Lock()

        # Publishers cognitifs (pour CognitiveRenderer)
        self.pub_cognitive = self.create_publisher(String, "/cognitive_state", 10)
        self.pub_emotion = self.create_publisher(String, "/emotion_state", 10)

        # Publishers actuateurs et subscribers capteurs, indexés par body_id
        self._pub_say:     dict[str, object] = {}
        self._pub_motion:  dict[str, object] = {}
        self._pub_leds:    dict[str, object] = {}
        self._pub_posture: dict[str, object] = {}
        self._pub_move:    dict[str, object] = {}
        # État de connexion physique publié par chaque naoqi_driver
        self._body_connected: dict[str, bool] = {b: False for b in BODY_REGISTRY}

        for body_id in BODY_REGISTRY:
            self._register_body(body_id)

        # Timer vérification transition d'état (toutes les 60 s)
        self.create_timer(60.0, self._check_state)

        # Boucle onirique en thread séparé
        self._dream_thread = threading.Thread(target=self._dream_loop, daemon=True)
        self._dream_thread.start()

        if CHAT_PORT > 0:
            self._start_chat_server()

        self._publish_cognitive("idle")
        self.get_logger().info(
            f"body_gateway démarré — corps : {', '.join(BODY_REGISTRY)}"
        )
        if WEBUI_URL and WEBUI_API_KEY:
            self.get_logger().info(f"Notification WebUI activée → {WEBUI_URL}")
        else:
            self.get_logger().info(
                "Notification WebUI désactivée (WEBUI_URL / WEBUI_API_KEY non définis)"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Enregistrement dynamique d'un corps
    # ─────────────────────────────────────────────────────────────────────────

    def _register_body(self, body_id: str):
        ns = f"/bodies/{body_id}"

        # Actuateurs (→ corps)
        self._pub_say[body_id]     = self.create_publisher(String, f"{ns}/actuators/say", 10)
        self._pub_motion[body_id]  = self.create_publisher(String, f"{ns}/actuators/motion", 10)
        self._pub_leds[body_id]    = self.create_publisher(String, f"{ns}/actuators/leds", 10)
        self._pub_posture[body_id] = self.create_publisher(String, f"{ns}/actuators/posture", 10)
        self._pub_move[body_id]    = self.create_publisher(String, f"{ns}/actuators/move", 10)

        # Capteurs (corps →)
        self.create_subscription(
            String, f"{ns}/sensors/speech",
            lambda msg, bid=body_id: self._on_speech(bid, msg), 10
        )
        self.create_subscription(
            Range, f"{ns}/sensors/sonar",
            lambda msg, bid=body_id: self._on_sonar(bid, msg), 10
        )
        self.create_subscription(
            Bool, f"{ns}/sensors/bumper",
            lambda msg, bid=body_id: self._on_bumper(bid, msg), 10
        )
        self.create_subscription(
            Float32, f"{ns}/sensors/battery",
            lambda msg, bid=body_id: self._on_battery(bid, msg), 10
        )
        # Connexion physique (publié par naoqi_driver lors des reconnexions)
        self.create_subscription(
            Bool, f"{ns}/connected",
            lambda msg, bid=body_id: self._on_body_connected(bid, msg), 10
        )

        self.get_logger().info(f"Corps enregistré : {body_id}")

    # ─────────────────────────────────────────────────────────────────────────
    # Callbacks capteurs
    # ─────────────────────────────────────────────────────────────────────────

    def _on_battery(self, body_id: str, msg: Float32):
        self._batteries[body_id] = msg.data

    def _on_body_connected(self, body_id: str, msg: Bool):
        prev = self._body_connected.get(body_id, False)
        self._body_connected[body_id] = msg.data
        if msg.data and not prev:
            self.get_logger().info(f"[{body_id}] Corps connecté (plug-and-play)")
        elif not msg.data and prev:
            self.get_logger().warn(f"[{body_id}] Corps déconnecté")

    def _on_sonar(self, body_id: str, msg: Range):
        if msg.range < PROXIMITY_THRESHOLD and self._state == "IDLE":
            self._state = "AWAKE"
            self.get_logger().info(f"[{body_id}] Humain détecté à {msg.range:.2f} m → AWAKE")

    def _on_bumper(self, body_id: str, msg: Bool):
        if msg.data:
            self._trigger_inference(body_id, f"[bumper] Contact physique sur {body_id}.")

    def _on_speech(self, body_id: str, msg: String):
        text = msg.data.strip()
        if not text:
            return
        self._state = "AWAKE"
        self._trigger_voice_inference(body_id, text)

    # ─────────────────────────────────────────────────────────────────────────
    # Pipeline d'inférence principal — canal HTTP (WebUI chat)
    # ─────────────────────────────────────────────────────────────────────────

    def _start_chat_server(self):
        node = self

        class _ChatHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/voice-pop":
                    self._handle_voice_pop()
                else:
                    self.send_response(404)
                    self.end_headers()

            def _handle_voice_pop(self):
                with node._voice_queue_lock:
                    item = node._voice_queue.popleft() if node._voice_queue else {}
                payload = json.dumps(item).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                if self.path != "/chat":
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    return

                text = body.get("text", "").strip()
                robot_id = body.get("robot_id", BODY_REGISTRY[0])

                if not text:
                    self._respond(400, {"error": "missing text"})
                    return

                result = node._infer_sync(robot_id, text)
                self._respond(200, result)

            def _respond(self, code: int, data: dict):
                payload = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                pass  # logging via ROS uniquement

        server = HTTPServer(("0.0.0.0", CHAT_PORT), _ChatHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.get_logger().info(f"Chat HTTP endpoint démarré → :{CHAT_PORT}/chat  :{CHAT_PORT}/voice-pop")

    def _infer_sync(self, body_id: str, user_input: str) -> dict:
        """Inférence synchrone pour le canal HTTP (WebUI chat). Retourne la réponse."""
        self._publish_cognitive("thinking")
        self._state = "AWAKE"

        if not self._infer_lock.acquire(timeout=25):
            return {"speech": "Je suis occupé, réessaie dans un instant.", "error": "busy"}

        try:
            system_prompt = _AWAKE_SYSTEM.format(body_ids=", ".join(BODY_REGISTRY))
            response = requests.post(
                f"{BRAIN_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {BRAIN_API_KEY}"},
                json={
                    "model": BRAIN_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": f"[{body_id}] {user_input}"},
                    ],
                    "stream": False,
                    "temperature": 0.7,
                },
                timeout=20,
            )
            response.raise_for_status()

            raw = response.json()["choices"][0]["message"]["content"]
            parsed = self._parse_json(raw)

            self._publish_emotion(parsed.get("emotion", "neutral"))
            self._publish_cognitive("responding")
            self._dispatch_actions(parsed.get("actions", []))

            speech_text = " ".join(
                a.get("text", "")
                for a in parsed.get("actions", [])
                if a.get("type") == "speech"
            )
            return {
                "thoughts": parsed.get("thoughts", ""),
                "emotion":  parsed.get("emotion", "neutral"),
                "speech":   speech_text,
                "actions":  parsed.get("actions", []),
            }

        except requests.RequestException as e:
            self.get_logger().error(f"Bitterbot API error (chat): {e}")
            self._fallback(body_id)
            return {"speech": "Je réfléchis, un instant.", "error": str(e)}
        except Exception as e:
            self.get_logger().error(f"Chat infer error: {e}")
            return {"speech": "Une erreur s'est produite.", "error": str(e)}
        finally:
            self._infer_lock.release()
            self._publish_cognitive("listening")

    # ─────────────────────────────────────────────────────────────────────────
    # Pipeline d'inférence voix (parcours 2 : "Ok nao" → WebUI)
    # ─────────────────────────────────────────────────────────────────────────

    def _trigger_voice_inference(self, body_id: str, user_input: str):
        """Lance l'inférence vocale en arrière-plan avec notification WebUI."""
        self._publish_cognitive("thinking")
        threading.Thread(
            target=self._infer_voice,
            args=(body_id, user_input),
            daemon=True,
        ).start()

    def _infer_voice(self, body_id: str, user_input: str):
        """Inférence déclenchée par la parole (wake word). Stocke le résultat et notifie WebUI."""
        if not self._infer_lock.acquire(blocking=False):
            self.get_logger().warn("Inférence déjà en cours — événement voix ignoré")
            return

        try:
            system_prompt = _AWAKE_SYSTEM.format(body_ids=", ".join(BODY_REGISTRY))
            response = requests.post(
                f"{BRAIN_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {BRAIN_API_KEY}"},
                json={
                    "model": BRAIN_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": f"[{body_id}] {user_input}"},
                    ],
                    "stream": False,
                    "temperature": 0.7,
                },
                timeout=20,
            )
            response.raise_for_status()

            raw = response.json()["choices"][0]["message"]["content"]
            parsed = self._parse_json(raw)

            self._publish_emotion(parsed.get("emotion", "neutral"))
            self._publish_cognitive("responding")
            self._dispatch_actions(parsed.get("actions", []))

            speech_text = " ".join(
                a.get("text", "")
                for a in parsed.get("actions", [])
                if a.get("type") == "speech"
            )
            result = {
                "thoughts": parsed.get("thoughts", ""),
                "emotion":  parsed.get("emotion", "neutral"),
                "speech":   speech_text,
                "actions":  parsed.get("actions", []),
            }

            # Stocker dans la file avant de notifier WebUI
            with self._voice_queue_lock:
                self._voice_queue.append(result)

            self.get_logger().info(f"[{body_id}] Voix → file ({len(self._voice_queue)}) : {user_input!r}")

            # Notifier Open WebUI si configuré
            if WEBUI_URL and WEBUI_API_KEY:
                self._notify_webui(user_input)

        except requests.RequestException as e:
            self.get_logger().error(f"Voice API error: {e}")
            self._fallback(body_id)
        except Exception as e:
            self.get_logger().error(f"Voice infer error: {e}")
            if not self._infer_lock.locked():
                self._fallback(body_id)
        finally:
            self._infer_lock.release()
            self._publish_cognitive("listening")

    def _notify_webui(self, transcription: str):
        """
        Injecte l'interaction vocale dans Open WebUI via l'API chat/completions.
        Le Pipe NAO Brain récupère la réponse pré-calculée depuis /voice-pop
        et l'affiche dans le chat "NAO Voice" (WEBUI_VOICE_CHAT_ID).
        """
        payload: dict = {
            "model": WEBUI_PIPE_MODEL,
            "messages": [{"role": "user", "content": transcription}],
            "nao_voice": True,
            "stream": False,
        }
        if WEBUI_VOICE_CHAT_ID:
            payload["chat_id"] = WEBUI_VOICE_CHAT_ID

        try:
            requests.post(
                f"{WEBUI_URL}/api/chat/completions",
                headers={"Authorization": f"Bearer {WEBUI_API_KEY}"},
                json=payload,
                timeout=30,
            )
        except Exception as e:
            self.get_logger().warn(f"_notify_webui error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Pipeline d'inférence principal (non-voix : sonar, bumper, rêve)
    # ─────────────────────────────────────────────────────────────────────────

    def _trigger_inference(self, body_id: str, user_input: str):
        """Lance l'inférence en arrière-plan ; publie 'thinking' immédiatement."""
        self._publish_cognitive("thinking")
        threading.Thread(
            target=self._infer_and_act,
            args=(body_id, user_input, False),
            daemon=True,
        ).start()

    def _infer_and_act(self, body_id: str, user_input: str, is_dream: bool):
        if not self._infer_lock.acquire(blocking=False):
            self.get_logger().warn("Inférence déjà en cours — événement ignoré")
            return

        try:
            system_prompt = (
                _DREAM_SYSTEM if is_dream else _AWAKE_SYSTEM
            ).format(body_ids=", ".join(BODY_REGISTRY))

            response = requests.post(
                f"{BRAIN_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {BRAIN_API_KEY}"},
                json={
                    "model": BRAIN_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": f"[{body_id}] {user_input}"},
                    ],
                    "stream": False,
                    "temperature": 0.5 if is_dream else 0.7,
                },
                timeout=20,
            )
            response.raise_for_status()

            raw = response.json()["choices"][0]["message"]["content"]
            parsed = self._parse_json(raw)

            if not is_dream:
                self._publish_emotion(parsed.get("emotion", "neutral"))
                self._publish_cognitive("responding")

            self._dispatch_actions(parsed.get("actions", []))

        except requests.RequestException as e:
            self.get_logger().error(f"Bitterbot API error: {e}")
            if not is_dream:
                self._fallback(body_id)
        except Exception as e:
            self.get_logger().error(f"Inférence error: {e}")
            if not is_dream:
                self._fallback(body_id)
        finally:
            self._infer_lock.release()
            if not is_dream:
                self._publish_cognitive("listening")

    def _parse_json(self, raw: str) -> dict:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        self.get_logger().warn("JSON parse failed — réponse ignorée")
        return {"actions": []}

    # ─────────────────────────────────────────────────────────────────────────
    # Routage des actions vers les corps
    # ─────────────────────────────────────────────────────────────────────────

    def _dispatch_actions(self, actions: list):
        for action in actions:
            body_id = action.get("body", BODY_REGISTRY[0])
            if body_id not in BODY_REGISTRY:
                self.get_logger().warn(f"Corps inconnu '{body_id}' — action ignorée")
                continue

            if not self._body_connected.get(body_id, False):
                self.get_logger().warn(
                    f"[{body_id}] Corps non connecté (heartbeat absent) — "
                    f"action '{action.get('type','')}' envoyée quand même"
                )

            kind = action.get("type", "")
            try:
                self._execute_action(body_id, kind, action)
            except Exception as e:
                self.get_logger().error(f"[{body_id}] Action '{kind}' error: {e}")

    def _execute_action(self, body_id: str, kind: str, action: dict):
        if kind == "speech":
            msg = String()
            msg.data = action.get("text", "")
            self._pub_say[body_id].publish(msg)
            time.sleep(0.3)

        elif kind == "motion":
            name = action.get("name", "")
            if name in ("sit", "stand"):
                msg = String()
                msg.data = "Sit" if name == "sit" else "Stand"
                self._pub_posture[body_id].publish(msg)
            elif name in _MOTION_PRESETS:
                msg = String()
                msg.data = json.dumps(_MOTION_PRESETS[name])
                self._pub_motion[body_id].publish(msg)
            elif "joint" in action:
                # Accès direct joint (utilisé dans les prompts oniriques)
                msg = String()
                msg.data = json.dumps({
                    "joint": action["joint"],
                    "angle": float(action.get("angle", 0.0)),
                    "speed": float(action.get("speed", 0.05)),
                })
                self._pub_motion[body_id].publish(msg)

        elif kind == "leds":
            color_name = action.get("color", "white")
            r, g, b = _COLORS.get(color_name, _COLORS["white"])
            msg = String()
            msg.data = json.dumps({"group": "AllLeds", "r": r, "g": g, "b": b, "duration": 0.5})
            self._pub_leds[body_id].publish(msg)

        elif kind == "move":
            msg = String()
            msg.data = json.dumps({
                "x": float(action.get("x", 0.0)),
                "y": float(action.get("y", 0.0)),
                "theta": float(action.get("theta", 0.0)),
            })
            self._pub_move[body_id].publish(msg)

        elif kind == "look":
            msg = String()
            msg.data = json.dumps({
                "joint": "HeadYaw",
                "angle": float(action.get("yaw", 0.0)) * 0.5,
                "speed": 0.12,
            })
            self._pub_motion[body_id].publish(msg)

    def _fallback(self, body_id: str):
        self._publish_cognitive("uncertain")
        msg = String()
        msg.data = "Je réfléchis, un instant s'il vous plaît."
        if body_id in self._pub_say:
            self._pub_say[body_id].publish(msg)
        time.sleep(2.0)

    # ─────────────────────────────────────────────────────────────────────────
    # Machine à états et boucle onirique
    # ─────────────────────────────────────────────────────────────────────────

    def _check_state(self):
        hour = datetime.datetime.now().hour
        # Tous les corps doivent avoir batterie suffisante pour entrer en rêve
        min_battery = min(self._batteries.values()) if self._batteries else 100.0
        should_dream = (
            DREAMING_HOUR_START <= hour < DREAMING_HOUR_END
            and min_battery >= DREAMING_BATTERY_THRESHOLD
        )

        if should_dream and self._state != "DREAMING":
            self._state = "DREAMING"
            self._publish_cognitive("dreaming")
            self.get_logger().info("État → DREAMING")
        elif not should_dream and self._state == "DREAMING":
            self._state = "IDLE"
            self._publish_cognitive("idle")
            self.get_logger().info("État → IDLE (fin du rêve)")

    def _dream_loop(self):
        """Envoie des ticks oniriques à Bitterbot pendant DREAMING."""
        while self._running:
            time.sleep(DREAM_TICK_INTERVAL)
            if self._state != "DREAMING":
                continue
            # Corps principal pour le tick onirique (premier corps enregistré)
            body_id = BODY_REGISTRY[0]
            self.get_logger().info("Dream tick → Bitterbot")
            self._infer_and_act(
                body_id,
                "Génère une expression onirique subtile pour ton corps.",
                is_dream=True,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers publication
    # ─────────────────────────────────────────────────────────────────────────

    def _publish_cognitive(self, state: str):
        msg = String()
        msg.data = state
        self.pub_cognitive.publish(msg)

    def _publish_emotion(self, emotion: str):
        msg = String()
        msg.data = emotion
        self.pub_emotion.publish(msg)

    def destroy_node(self):
        self._running = False
        super().destroy_node()


def main():
    rclpy.init()
    node = BodyGatewayNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
