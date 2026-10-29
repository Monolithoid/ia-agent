"""
events — what body_gateway publishes on the ROS topic /events (std_msgs/String,
one JSON object per message), for the régie and any ROS tool (Lichtblick, rqt,
`ros2 topic echo /events`).

Every message shares these fields:
  v      schema version (1)
  t      Unix time in seconds
  kind   input | ignored | decision | action | error | bodies
  id     interaction id: the input, the LLM decision and the actions it
         produced share it ("" for events outside an interaction)

  input     source (webui|twitch|voice|sensor|dream), body, text, author
  ignored   source, body, text, reason (wake_word|busy)
  decision  thoughts, emotion, energy, actions (as returned by the LLM), duration_s
  action    type, requested (body named by the LLM), targets (bodies it went to),
            connected {target: bool}, detail (short text)
  error     message
  bodies    connected {body: bool}, state (IDLE|AWAKE|DREAMING)

No ROS import here, so the format is tested without ROS 2.
"""

import itertools
import json
import os
import time

SCHEMA = 1
TWITCH_PREFIX = "[tchat Twitch]"

_ids = itertools.count(1)
_boot = os.urandom(2).hex()


def new_id() -> str:
    """Short interaction id, unique for this body_gateway run."""
    return f"{_boot}-{next(_ids)}"


def make(kind: str, interaction: str = "", **fields) -> dict:
    return {"v": SCHEMA, "t": round(time.time(), 3), "kind": kind, "id": interaction, **fields}


def encode(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False)


def chat_origin(text: str) -> tuple[str, str]:
    """(source, author) of a /chat text: twitch-input posts
    "[tchat Twitch] pseudo : message", everything else comes from Open WebUI."""
    if not text.startswith(TWITCH_PREFIX):
        return "webui", ""
    author, sep, _ = text[len(TWITCH_PREFIX):].partition(" : ")
    return "twitch", author.strip() if sep else ""


def action_detail(action: dict) -> str:
    """Short human-readable summary of an LLM action, for the régie."""
    kind = action.get("type", "")
    if kind == "speech":
        return action.get("text", "")
    if kind == "motion":
        if action.get("name"):
            return action["name"]
        if "joint" in action:
            return f"{action['joint']} {action.get('angle', 0)}"
        if isinstance(action.get("joints"), list):
            return " ".join(f"{j} {a}" for j, a in zip(action["joints"], action.get("angles", [])))
        return ""
    if kind == "leds":
        return action.get("color", "")
    if kind == "move":
        return f"x={action.get('x', 0)} y={action.get('y', 0)} θ={action.get('theta', 0)}"
    if kind == "look":
        return f"yaw={action.get('yaw', 0)}"
    return ""
