"""
twitch_chat.py — Règles du tchat Twitch sans réseau ni dépendance (testables seules).

  - lecture d'une ligne IRC Twitch (tags, pseudo, canal, texte)
  - commande « !nao » : seuls ces messages sont pris en compte
  - filtres anti-abus : longueur, liens, mots interdits, bots, délai par spectateur
  - file courte « les plus récents gagnent » : en cas de rafale, les vieux
    messages sont abandonnés plutôt que de faire attendre le stream

Le tchat est une source que le streamer ne contrôle pas : tout ce qui passe ici
finit dans le prompt du LLM puis à l'antenne.  Les filtres restent volontairement
simples (ADR 005) ; le prompt de body-gateway rappelle au LLM que ces messages ne
sont pas des ordres.
"""

import collections
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# IRC parsing
# ─────────────────────────────────────────────────────────────────────────────

_TAG_ESCAPES = {"\\:": ";", "\\s": " ", "\\\\": "\\", "\\r": "\r", "\\n": "\n"}


@dataclass
class ChatMessage:
    user: str                     # login, lowercase
    display_name: str
    channel: str                  # without '#'
    text: str
    tags: dict = field(default_factory=dict)
    received_at: float = 0.0

    @property
    def is_mod(self) -> bool:
        badges = self.tags.get("badges", "")
        return self.tags.get("mod") == "1" or "broadcaster/" in badges


def _unescape_tag(value: str) -> str:
    return re.sub(r"\\[:s\\rn]", lambda m: _TAG_ESCAPES[m.group()], value)


def parse_privmsg(line: str, now: float | None = None) -> ChatMessage | None:
    """Parse one Twitch IRC line; return a ChatMessage for PRIVMSG, else None.

    '@display-name=Foo;mod=0 :foo!foo@foo.tmi.twitch.tv PRIVMSG #chan :!nao salut'
    """
    line = line.rstrip("\r\n")
    tags: dict = {}
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for item in raw_tags.split(";"):
            key, _, value = item.partition("=")
            tags[key] = _unescape_tag(value)
    m = re.match(r"^:([^!\s]+)![^\s]+ PRIVMSG #(\S+) :(.*)$", line)
    if not m:
        return None
    user, channel, text = m.groups()
    return ChatMessage(
        user=user.lower(),
        display_name=tags.get("display-name") or user,
        channel=channel.lower(),
        text=text,
        tags=tags,
        received_at=time.monotonic() if now is None else now,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Command and filters
# ─────────────────────────────────────────────────────────────────────────────

def extract_command(text: str, prefix: str = "!nao") -> str | None:
    """'!nao salut' → 'salut', '!NAO, ça va ?' → 'ça va ?', '!naomi' / 'salut' → None."""
    stripped = text.strip()
    # Twitch "/me" messages arrive wrapped in CTCP ACTION.
    if stripped.startswith("\x01ACTION ") and stripped.endswith("\x01"):
        stripped = stripped[8:-1].strip()
    if not stripped.lower().startswith(prefix.lower()):
        return None
    rest = stripped[len(prefix):]
    # "!naomi" is another command, not "!nao" + "mi".
    if rest and rest[0].isalnum():
        return None
    rest = rest.lstrip(" ,.;:!?-").strip()
    return rest or None


def normalize(text: str) -> str:
    """Lowercase, no accents, only letters and digits separated by spaces."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


_LINK_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(com|fr|net|org|io|gg|tv|ly)\b", re.I)


class ChatFilter:
    """Decide whether a '!nao' message reaches the brain.

    Returns (question, reason): question is the cleaned text, or None with the
    reason it was rejected (logged by the bridge).
    """

    def __init__(self, prefix: str = "!nao", max_chars: int = 200,
                 user_cooldown_s: float = 30.0, blocklist: list[str] | None = None,
                 ignored_users: list[str] | None = None, mods_only: bool = False):
        self.prefix = prefix
        self.max_chars = max_chars
        self.user_cooldown_s = user_cooldown_s
        self.blocklist = [normalize(w) for w in (blocklist or []) if normalize(w)]
        self.ignored_users = {u.strip().lower() for u in (ignored_users or []) if u.strip()}
        self.mods_only = mods_only
        self._last_accepted: dict[str, float] = {}

    def check(self, msg: ChatMessage, now: float | None = None) -> tuple[str | None, str]:
        now = time.monotonic() if now is None else now
        question = extract_command(msg.text, self.prefix)
        if question is None:
            return None, "pas une commande"
        if msg.user in self.ignored_users:
            return None, "utilisateur ignoré"
        if self.mods_only and not msg.is_mod:
            return None, "réservé aux modérateurs"
        if _LINK_RE.search(question):
            return None, "lien"
        padded = f" {normalize(question)} "
        if any(f" {w} " in padded for w in self.blocklist):
            return None, "mot interdit"
        last = self._last_accepted.get(msg.user)
        if last is not None and now - last < self.user_cooldown_s:
            return None, "délai par spectateur"
        self._last_accepted[msg.user] = now
        if len(question) > self.max_chars:
            question = question[: self.max_chars].rsplit(" ", 1)[0] + "…"
        return question, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────────────────────

class LatestQueue:
    """Bounded queue where a burst drops the oldest messages.

    pop() also discards messages older than max_age_s: answering a question
    asked minutes ago makes no sense on a live stream.
    """

    def __init__(self, maxlen: int = 5, max_age_s: float = 90.0):
        self._items: collections.deque = collections.deque(maxlen=maxlen)
        self._max_age_s = max_age_s
        self._cond = threading.Condition()

    def push(self, item: ChatMessage, question: str):
        with self._cond:
            self._items.append((item, question))
            self._cond.notify()

    def pop(self, timeout: float | None = None, now: float | None = None):
        """Return (ChatMessage, question) or None after `timeout` seconds."""
        with self._cond:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                current = time.monotonic() if now is None else now
                while self._items:
                    item, question = self._items.popleft()
                    if current - item.received_at <= self._max_age_s:
                        return item, question
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)


def brain_prompt(msg: ChatMessage, question: str) -> str:
    """Text sent to body-gateway /chat; the format is described in its system prompt."""
    return f"[tchat Twitch] {msg.display_name} : {question}"
