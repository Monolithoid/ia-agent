#!/usr/bin/env python3
"""
twitch_bridge — NAO avatar on a Twitch stream (ADR 005).

    Twitch chat (IRC, anonymous read)
      │  only "!nao …" messages, filtered (twitch_chat.ChatFilter)
      ▼
    LatestQueue ── one message at a time ──► body-gateway POST /chat {speak: false}
                                               │  LLM JSON → gestures / head / LEDs → NAO
                                               ▼  "speech" text returned, not played in the room
    edge-tts (MP3) ──► OBS browser source /overlay (SSE + <audio>)
                          │  sound goes into the OBS mixer
                          └► POST /speaking → body-gateway /speaking → NAO "speaking" animation

The bridge waits for the overlay to finish playing an answer before taking the
next message, so NAO never talks over itself.  Without any overlay connected it
does not wait (the NAO still moves, nobody hears the voice).

Environment variables:
  TWITCH_CHANNEL        channel to read (required), e.g. "monolithoid"
  OBS_TOKEN             secret in the overlay URL (required): /overlay?token=…
  BODY_GATEWAY_URL      default http://chat-relay:8095
  ROBOT_ID              body named in /chat, default "nao_01"
  COMMAND_PREFIX        default "!nao"
  MAX_CHARS             longest question kept, default 200
  USER_COOLDOWN_S       seconds between two accepted messages of one viewer, default 30
  QUEUE_MAX             messages kept during a burst (oldest dropped), default 5
  MAX_AGE_S             older messages are dropped, default 90
  MIN_INTERVAL_S        pause between two answers, default 3
  BLOCKLIST             comma-separated forbidden words, default empty
  IGNORED_USERS         comma-separated logins, default "nightbot,streamelements,moobot"
  MODS_ONLY             1 = only moderators and the broadcaster, default 0
  TTS_VOICE             edge-tts voice, default "fr-FR-HenriNeural"
  HTTP_PORT             overlay port, default 8096
"""

import hmac
import itertools
import json
import os
import pathlib
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import twitch_chat

try:
    import speech_services
except ImportError:
    # Lancé depuis le dépôt : le module partagé est dans robotics/shared/.
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "shared"))
    import speech_services


def _env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


TWITCH_CHANNEL   = os.environ.get("TWITCH_CHANNEL", "").strip().lstrip("#").lower()
OBS_TOKEN        = os.environ.get("OBS_TOKEN", "")
BODY_GATEWAY_URL = os.environ.get("BODY_GATEWAY_URL", "http://chat-relay:8095").rstrip("/")
ROBOT_ID         = os.environ.get("ROBOT_ID", "nao_01")
COMMAND_PREFIX   = os.environ.get("COMMAND_PREFIX", "!nao")
MAX_CHARS        = int(os.environ.get("MAX_CHARS", "200"))
USER_COOLDOWN_S  = float(os.environ.get("USER_COOLDOWN_S", "30"))
QUEUE_MAX        = int(os.environ.get("QUEUE_MAX", "5"))
MAX_AGE_S        = float(os.environ.get("MAX_AGE_S", "90"))
MIN_INTERVAL_S   = float(os.environ.get("MIN_INTERVAL_S", "3"))
BLOCKLIST        = _env_list("BLOCKLIST")
IGNORED_USERS    = _env_list("IGNORED_USERS", "nightbot,streamelements,moobot")
MODS_ONLY        = os.environ.get("MODS_ONLY", "0") == "1"
TTS_VOICE        = os.environ.get("TTS_VOICE", "fr-FR-HenriNeural")
HTTP_PORT        = int(os.environ.get("HTTP_PORT", "8096"))

TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697
# Longest wait for the overlay to report the end of an answer.
PLAYBACK_TIMEOUT_S = 60.0
OVERLAY_HTML = pathlib.Path(__file__).with_name("overlay.html")


def log(text: str):
    print(f"[twitch-bridge] {text}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Overlay hub: SSE clients, recent audio, playback state
# ─────────────────────────────────────────────────────────────────────────────

class OverlayHub:

    def __init__(self, keep: int = 20):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._audio: dict[str, bytes] = {}
        self._keep = keep
        self._ids = itertools.count(1)
        self.playback_done = threading.Event()

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

    def publish_answer(self, user: str, question: str, answer: str, mp3: bytes | None) -> str:
        audio_id = str(next(self._ids))
        with self._lock:
            if mp3:
                self._audio[audio_id] = mp3
                while len(self._audio) > self._keep:
                    self._audio.pop(next(iter(self._audio)))
            event = {"id": audio_id, "user": user, "question": question,
                     "answer": answer, "audio": bool(mp3)}
            self.playback_done.clear()
            for q in self._clients:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass
        return audio_id


HUB = OverlayHub()


# ─────────────────────────────────────────────────────────────────────────────
# body-gateway calls
# ─────────────────────────────────────────────────────────────────────────────

def ask_brain(prompt: str) -> dict:
    """POST /chat without room speech: NAO moves, the text comes back to us."""
    resp = requests.post(
        f"{BODY_GATEWAY_URL}/chat",
        json={"text": prompt, "robot_id": ROBOT_ID, "speak": False},
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()


def report_speaking(speaking: bool):
    """Forward the overlay playback state so NAO animates while OBS plays."""
    try:
        requests.post(f"{BODY_GATEWAY_URL}/speaking", json={"speaking": speaking}, timeout=5)
    except requests.RequestException as e:
        log(f"/speaking injoignable : {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Twitch IRC reader (anonymous, read-only)
# ─────────────────────────────────────────────────────────────────────────────

def irc_loop(chat_filter: twitch_chat.ChatFilter, inbox: twitch_chat.LatestQueue):
    backoff = 2
    while True:
        try:
            raw = socket.create_connection((TWITCH_IRC_HOST, TWITCH_IRC_PORT), timeout=360)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=TWITCH_IRC_HOST)
            send = lambda s: sock.sendall((s + "\r\n").encode())  # noqa: E731
            send("CAP REQ :twitch.tv/tags")
            # "justinfan…" is Twitch's anonymous read-only login: no token needed.
            send(f"NICK justinfan{int(time.time()) % 100000}")
            send(f"JOIN #{TWITCH_CHANNEL}")
            log(f"connecté au tchat #{TWITCH_CHANNEL}")
            backoff = 2
            for line in sock.makefile("r", encoding="utf-8", errors="replace"):
                if line.startswith("PING"):
                    send("PONG" + line[4:].rstrip("\r\n"))
                    continue
                if " RECONNECT" in line[:80]:
                    log("Twitch demande une reconnexion")
                    break
                msg = twitch_chat.parse_privmsg(line)
                if msg is None:
                    continue
                question, reason = chat_filter.check(msg)
                if question is None:
                    if reason != "pas une commande":
                        log(f"ignoré ({reason}) : {msg.user}")
                    continue
                inbox.push(msg, question)
                log(f"reçu de {msg.display_name} : {question[:80]!r} (file : {len(inbox)})")
            sock.close()
        except OSError as e:
            log(f"tchat déconnecté : {e} — nouvel essai dans {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ─────────────────────────────────────────────────────────────────────────────
# Worker: one message at a time → brain → TTS → overlay
# ─────────────────────────────────────────────────────────────────────────────

def worker_loop(inbox: twitch_chat.LatestQueue):
    while True:
        item = inbox.pop()
        if item is None:
            continue
        msg, question = item
        try:
            result = ask_brain(twitch_chat.brain_prompt(msg, question))
        except (requests.RequestException, ValueError) as e:
            log(f"body-gateway en erreur : {e}")
            time.sleep(MIN_INTERVAL_S)
            continue
        if result.get("error") == "busy":
            log("cerveau occupé (parole dans la pièce) — message abandonné")
            continue
        answer = (result.get("speech") or "").strip()
        if not answer:
            log("réponse sans parole")
            continue
        try:
            mp3 = speech_services.synthesize_mp3(answer, TTS_VOICE)
        except Exception as e:
            log(f"edge-tts en erreur : {e} — réponse affichée sans voix")
            mp3 = None
        HUB.publish_answer(msg.display_name, question, answer, mp3)
        log(f"réponse à {msg.display_name} : {answer[:80]!r}")
        if mp3 and HUB.client_count() > 0:
            if not HUB.playback_done.wait(PLAYBACK_TIMEOUT_S):
                log("l'overlay n'a pas signalé la fin de lecture")
                report_speaking(False)
        time.sleep(MIN_INTERVAL_S)


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
            self._send(200, json.dumps({"ok": True, "service": "twitch-bridge",
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
        report_speaking(speaking)
        if not speaking:
            HUB.playback_done.set()
        self._send(200, b'{"ok": true}', "application/json")

    def _events(self):
        q = HUB.subscribe()
        log(f"overlay OBS connecté ({HUB.client_count()})")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                try:
                    event = q.get(timeout=15)
                    payload = f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    payload = ": keepalive\n\n"
                self.wfile.write(payload.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            HUB.unsubscribe(q)
            # A closed overlay must not keep the worker waiting.
            HUB.playback_done.set()
            log(f"overlay OBS déconnecté ({HUB.client_count()})")

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
    if not TWITCH_CHANNEL or not OBS_TOKEN:
        log("ERREUR : TWITCH_CHANNEL et OBS_TOKEN sont obligatoires")
        sys.exit(1)
    chat_filter = twitch_chat.ChatFilter(
        prefix=COMMAND_PREFIX, max_chars=MAX_CHARS, user_cooldown_s=USER_COOLDOWN_S,
        blocklist=BLOCKLIST, ignored_users=IGNORED_USERS, mods_only=MODS_ONLY,
    )
    inbox = twitch_chat.LatestQueue(maxlen=QUEUE_MAX, max_age_s=MAX_AGE_S)
    threading.Thread(target=irc_loop, args=(chat_filter, inbox), daemon=True).start()
    threading.Thread(target=worker_loop, args=(inbox,), daemon=True).start()
    log(f"overlay OBS → :{HTTP_PORT}/overlay?token=…  |  cerveau → {BODY_GATEWAY_URL}")
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
