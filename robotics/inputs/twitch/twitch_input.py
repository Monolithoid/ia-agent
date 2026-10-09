#!/usr/bin/env python3
"""
twitch_input — Twitch chat as one more input of the brain (ADR 005).

Like "ok nao" heard by the static drone or a message typed in Open WebUI, a
"!nao …" chat message only becomes a text sent to body-gateway /chat.  The
brain answers with a list of actions for the bodies: gestures and LEDs on the
NAO, speech on every connected voice (room speaker, OBS stream voice).  This
service knows nothing about the outputs.

    Twitch chat (IRC, anonymous read)
      │  only "!nao …" messages, filtered (twitch_chat.ChatFilter)
      ▼
    LatestQueue ── one message at a time ──► body-gateway POST /chat
                                               "[tchat Twitch] pseudo : message"

Environment variables:
  TWITCH_CHANNEL        channel to read (required), e.g. "monolithoid"
  BODY_GATEWAY_URL      default http://chat-relay:8095
  ROBOT_ID              body named in /chat, default "nao_01"
  COMMAND_PREFIX        default "!nao"
  MAX_CHARS             longest question kept, default 200
  USER_COOLDOWN_S       seconds between two accepted messages of one viewer, default 30
  QUEUE_MAX             messages kept during a burst (oldest dropped), default 5
  MAX_AGE_S             older messages are dropped, default 90
  MIN_INTERVAL_S        pause after each answer, default 5
  BLOCKLIST             comma-separated forbidden words, default empty
  IGNORED_USERS         comma-separated logins, default "nightbot,streamelements,moobot"
  MODS_ONLY             1 = only moderators and the broadcaster, default 0
"""

import os
import socket
import ssl
import sys
import threading
import time

import requests

import twitch_chat


def _env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


TWITCH_CHANNEL   = os.environ.get("TWITCH_CHANNEL", "").strip().lstrip("#").lower()
BODY_GATEWAY_URL = os.environ.get("BODY_GATEWAY_URL", "http://chat-relay:8095").rstrip("/")
ROBOT_ID         = os.environ.get("ROBOT_ID", "nao_01")
COMMAND_PREFIX   = os.environ.get("COMMAND_PREFIX", "!nao")
MAX_CHARS        = int(os.environ.get("MAX_CHARS", "200"))
USER_COOLDOWN_S  = float(os.environ.get("USER_COOLDOWN_S", "30"))
QUEUE_MAX        = int(os.environ.get("QUEUE_MAX", "5"))
MAX_AGE_S        = float(os.environ.get("MAX_AGE_S", "90"))
MIN_INTERVAL_S   = float(os.environ.get("MIN_INTERVAL_S", "5"))
BLOCKLIST        = _env_list("BLOCKLIST")
IGNORED_USERS    = _env_list("IGNORED_USERS", "nightbot,streamelements,moobot")
MODS_ONLY        = os.environ.get("MODS_ONLY", "0") == "1"

TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697


def log(text: str):
    print(f"[twitch-input] {text}", flush=True)


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
# Worker: one message at a time → brain
# ─────────────────────────────────────────────────────────────────────────────

def worker_loop(inbox: twitch_chat.LatestQueue):
    while True:
        item = inbox.pop()
        if item is None:
            continue
        msg, question = item
        try:
            # Same entry point as Open WebUI: the brain dispatches the actions itself.
            resp = requests.post(
                f"{BODY_GATEWAY_URL}/chat",
                json={"text": twitch_chat.brain_prompt(msg, question), "robot_id": ROBOT_ID},
                timeout=180,
            )
            resp.raise_for_status()
            result = resp.json()
        except (requests.RequestException, ValueError) as e:
            log(f"body-gateway en erreur : {e}")
        else:
            if result.get("error") == "busy":
                log("cerveau occupé — message abandonné")
            else:
                log(f"réponse à {msg.display_name} : {(result.get('speech') or '')[:80]!r}")
        # Leaves time for the answer to be spoken before the next one.
        time.sleep(MIN_INTERVAL_S)


def main():
    if not TWITCH_CHANNEL:
        log("ERREUR : TWITCH_CHANNEL est obligatoire")
        sys.exit(1)
    chat_filter = twitch_chat.ChatFilter(
        prefix=COMMAND_PREFIX, max_chars=MAX_CHARS, user_cooldown_s=USER_COOLDOWN_S,
        blocklist=BLOCKLIST, ignored_users=IGNORED_USERS, mods_only=MODS_ONLY,
    )
    inbox = twitch_chat.LatestQueue(maxlen=QUEUE_MAX, max_age_s=MAX_AGE_S)
    threading.Thread(target=irc_loop, args=(chat_filter, inbox), daemon=True).start()
    log(f"tchat #{TWITCH_CHANNEL} → {BODY_GATEWAY_URL}/chat")
    worker_loop(inbox)


if __name__ == "__main__":
    main()
