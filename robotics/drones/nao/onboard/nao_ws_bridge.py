#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
nao_ws_bridge.py — Script embedded on the NAO robot (Python 2.7).

Establishes an OUTBOUND WebSocket connection to the rosbridge server on the VPS,
subscribes to command topics and publishes robot status and captured audio.

No external Python dependencies — uses a built-in pure Python 2.7 WebSocket client.
The naoqi module is available natively on the robot (OpenNAO firmware).

Usage:
    python /home/nao/nao_ws_bridge.py --vps ws://DRONE_IP:9090/ [options]

Authentication:
    --token TOKEN                  Secret sent as "Authorization: Bearer TOKEN" during
                                   the WebSocket handshake (rosbridge-proxy rejects
                                   connections without it). Defaults to the
                                   ROSBRIDGE_TOKEN environment variable.
                                   Not needed through the static drone relay
                                   (ADR 006), which adds the token itself.

Module filtering:
    --modules led,speech,gesture   Enable only the listed modules.
                                   Available: led, speech, audio_out, gesture,
                                   motion, move, stiffness, stop
                                   (default: all except speech and audio_out).
    --audio-capture                Enable wake word detection and VAD capture
                                   (off by default).
    --no-audio-capture             Force it off (overrides the provision file).
    Audio is off by default since ADR 004: the static drone speaks and listens,
    and the NAO saves the CPU of ALSpeechRecognition and TTS playback.
    The "bridge" section of the --provision file can set the same two options
    ({"modules": [...], "audio_capture": false}); command-line flags win.

TTS method:
    --tts-method auto|mp3|text
        mp3  : VPS generates audio via edge-tts and sends it on /nao/audio_out.
        text : VPS sends raw text on /nao/speech (native NAOqi TTS).
        auto : mp3 if NAOqi is available, otherwise text (default).

Examples:
    # Emotive-only NAO (default: the static drone speaks, listens and sees):
    python nao_ws_bridge.py --vps ws://DRONE_IP:9090/

    # LEDs + speech only, no audio capture:
    python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --modules led,speech

    # NAO's own voice and ears (no static drone):
    python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --modules led,motion,stiffness,gesture,stop,move,speech,audio_out --audio-capture

    # Apply post-flash config then start bridge:
    python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --provision
"""

from __future__ import print_function

import argparse
import base64
import binascii
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time

try:
    from naoqi import ALProxy, ALModule, ALBroker
    HAS_NAOQI = True
except ImportError:
    HAS_NAOQI = False
    ALBroker = None
    class ALModule(object):
        def __init__(self, name):
            pass
    print("[bridge] WARNING: naoqi module not found — MOCK mode active", file=sys.stderr)

NAO_IP = "127.0.0.1"
NAO_PORT = 9559
RECONNECT_DELAY = 5
STATUS_INTERVAL = 30

# High-rate topics (motion_engine publishes /nao/motion at up to 20 Hz) are not
# logged unless --verbose: one stderr line per message costs CPU on the robot.
_QUIET_TOPICS = frozenset(["/nao/motion", "/nao/status"])
_VERBOSE = False

_PROVISION_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nao_provision.json"
)
# Motor profile written by the calibration (régie), and NAOqi components
# switched on or off from the régie.  Both next to this script.
_MOTOR_PROFILE_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nao_motor_profile.json"
)
_COMPONENTS_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nao_components.json"
)

# Mapping: module name → (ROS topic, message type)
_MODULE_TOPICS = {
    "speech":    ("/nao/speech",     "std_msgs/String"),
    "gesture":   ("/nao/gesture",    "std_msgs/String"),
    "led":       ("/nao/leds",       "std_msgs/String"),
    "motion":    ("/nao/motion",     "std_msgs/String"),
    "move":      ("/nao/cmd_vel",    "geometry_msgs/Twist"),
    "stop":      ("/nao/stop",       "std_msgs/Empty"),
    "audio_out": ("/nao/audio_out",  "std_msgs/String"),
    "stiffness": ("/nao/stiffness",  "std_msgs/String"),
}
# System topics always subscribed regardless of module filtering
_SYSTEM_TOPICS = [
    ("/nao/shutdown",    "std_msgs/Empty"),
    ("/nao/calibration", "std_msgs/String"),   # calibration steps, from the régie
    ("/nao/components",  "std_msgs/String"),   # NAOqi components on/off, from the régie
]
ALL_MODULES = frozenset(_MODULE_TOPICS.keys())
# Voice modules are opt-in: since ADR 004 the static drone speaks for the NAO.
_AUDIO_MODULES = frozenset(["speech", "audio_out"])
DEFAULT_MODULES = ALL_MODULES - _AUDIO_MODULES

# ── Motor temperature sampling ────────────────────────────────────────────────
# One representative joint per group — enough to detect heat without spamming ALMemory.
# We take the hottest joint in each group (max), which is the most relevant for safety.
_TEMP_JOINTS = {
    "head": ["HeadYaw", "HeadPitch"],
    "larm": ["LShoulderPitch", "LElbowRoll"],
    "rarm": ["RShoulderPitch", "RElbowRoll"],
}
_TEMP_KEY = "Device/SubDeviceList/{}/Temperature/Sensor/Value"

# ── Environment sensor ALMemory keys ─────────────────────────────────────────
# Each entry: sensor_name → ALMemory key (read once per status tick).
# All values are raw floats; interpretation (labels + thresholds) happens on the VPS.
_ENV_SENSOR_KEYS = {
    "ambient_light": "Device/SubDeviceList/CameraTop/Illuminance/Sensor/Value",
    "sound_level":   "Device/SubDeviceList/LMicroFront/SoundLevel/Sensor/Value",
}

# ── Stiffness groups ──────────────────────────────────────────────────────────
# NAOqi group name used in ALMotion.setStiffnesses()
_NAOQI_GROUP = {
    "head": "Head",
    "larm": "LArm",
    "rarm": "RArm",
    "legs": "Legs",
}
# Reverse mapping: individual joint → group key (used to auto-detect group)
_JOINT_TO_GROUP = {}
for _g, _naoqi in _NAOQI_GROUP.items():
    if _naoqi == "Head":
        for _j in ("HeadYaw", "HeadPitch"):
            _JOINT_TO_GROUP[_j] = _g
    elif _naoqi == "LArm":
        for _j in ("LShoulderPitch", "LShoulderRoll", "LElbowYaw", "LElbowRoll",
                   "LWristYaw", "LHand"):
            _JOINT_TO_GROUP[_j] = _g
    elif _naoqi == "RArm":
        for _j in ("RShoulderPitch", "RShoulderRoll", "RElbowYaw", "RElbowRoll",
                   "RWristYaw", "RHand"):
            _JOINT_TO_GROUP[_j] = _g
    elif _naoqi == "Legs":
        for _j in ("LHipYawPitch", "LHipRoll", "LHipPitch", "LKneePitch",
                   "LAnklePitch", "LAnkleRoll",
                   "RHipYawPitch", "RHipRoll", "RHipPitch", "RKneePitch",
                   "RAnklePitch", "RAnkleRoll"):
            _JOINT_TO_GROUP[_j] = _g

# ── Motion bounds ─────────────────────────────────────────────────────────────
# The robot is a puppet: motion decisions (rate, end of move, dropped orders,
# postures, motor heat, return to rest) belong to the NAO driver on the VPS
# (robotics/drones/nao/driver).  The bridge only keeps hard bounds, a last
# safety net for an order that would bypass the driver.  Every value can be
# overridden in the "motion_limits" section of the provisioning file.
_MOTION_LIMITS_DEFAULT = {
    "max_speed":        0.3,    # fractionMaxSpeed ceiling for joint moves
    "min_speed":        0.02,   # floor: 0 or negative speeds are rejected by NAOqi
    "joint_margin":     0.05,   # rad kept away from each mechanical stop
    "joint_ranges":     {},     # per-joint [min, max] overrides (rad), e.g. {"HeadPitch": [-0.4, 0.3]}
    "stiffness_max":    1.0,    # ceiling for /nao/stiffness values
    "posture_speed":    0.3,    # goToPosture speed
    "walk_max":         0.5,    # |x|, |y|, |theta| ceiling for moveToward (normalized)
    # Startup state of the two NAOqi protections (True = on, False = off),
    # overridden by nao_components.json once switched from the régie.  Off
    # by default: both are a likely cause of NAOqi crashes on this NAO.
    "fall_manager":     False,  # ALMotion fall manager
    "arm_collision_protection": False,  # ALMotion self-collision protection for arms
    "range_margin":     0.02,   # rad taken off each end of a range shown in calibration
    # Stiffness off once a chain is back at rest; the next order turns it
    # back on without a jump (see _wake_chain).
    "rest_limp":        True,
    # "setAngles": non-blocking call (default).  "legacy": the call used
    # before the motion limits (setStiffnesses + blocking
    # angleInterpolationWithSpeed), to compare when NAOqi crashes.
    "motion_call":      "setAngles",
}

# Hardware joint ranges (rad), NAO V5 — same values as motion_engine/nao_joints.py.
# Legs are absent on purpose: leg joints are never commanded by this bridge.
_JOINT_HW_LIMITS = {
    "HeadYaw":        (-2.0857,  2.0857),
    "HeadPitch":      (-0.6720,  0.5149),
    "LShoulderPitch": (-2.0857,  2.0857),
    "LShoulderRoll":  (-0.3142,  1.3265),
    "LElbowYaw":      (-2.0857,  2.0857),
    "LElbowRoll":     (-1.5446, -0.0349),
    "LWristYaw":      (-1.8238,  1.8238),
    "LHand":          ( 0.0,     1.0),
    "RShoulderPitch": (-2.0857,  2.0857),
    "RShoulderRoll":  (-1.3265,  0.3142),
    "RElbowYaw":      (-2.0857,  2.0857),
    "RElbowRoll":     ( 0.0349,  1.5446),
    "RWristYaw":      (-1.8238,  1.8238),
    "RHand":          ( 0.0,     1.0),
}

def _motion_limits(section):
    """Merge a "motion_limits" provisioning section over the defaults.

    Unknown keys and values of the wrong type are ignored (with a warning)
    so a typo in the file never disables a guard rail.
    """
    limits = dict(_MOTION_LIMITS_DEFAULT)
    if not isinstance(section, dict):
        return limits
    for key, value in section.items():
        if key not in limits:
            print("[motion] motion_limits.{} : cle inconnue — ignoree.".format(key), file=sys.stderr)
            continue
        default = limits[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif key == "motion_call":
            # JSON strings are unicode on Python 2: compare values, not types.
            ok = value in ("setAngles", "legacy")
        elif isinstance(default, (int, float)):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            ok = isinstance(value, type(default))
        if not ok:
            print("[motion] motion_limits.{} : type invalide — ignoree.".format(key), file=sys.stderr)
            continue
        limits[key] = value
    return limits


def _joint_range(joint, limits, box=None):
    """Allowed [min, max] for a joint: hardware range shrunk by joint_margin,
    then narrowed by a joint_ranges override and by the motor profile box
    (see _profile_box).  None for an unknown joint, or for a joint outside
    the profile when there is one."""
    if joint not in _JOINT_HW_LIMITS:
        return None
    if box is not None and joint not in box:
        return None
    lo, hi = _JOINT_HW_LIMITS[joint]
    margin = float(limits["joint_margin"])
    # The hands are a 0..1 opening ratio, not an angle — no margin there.
    if not joint.endswith("Hand") and hi - lo > 2 * margin:
        lo, hi = lo + margin, hi - margin
    override = limits["joint_ranges"].get(joint)
    if isinstance(override, list) and len(override) == 2:
        try:
            # An override can only narrow the range, never widen it.
            lo = max(lo, float(override[0]))
            hi = min(hi, float(override[1]))
        except (TypeError, ValueError):
            pass
    if box is not None:
        lo, hi = max(lo, box[joint][0]), min(hi, box[joint][1])
        if lo > hi:   # box outside the hardware range: hold its nearest end
            lo = hi = _clamp(box[joint][0], *_JOINT_HW_LIMITS[joint])
    return lo, hi


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _clamp_speed(speed, limits):
    return _clamp(float(speed), float(limits["min_speed"]), float(limits["max_speed"]))


def _clamp_joints(joints, angles, limits, box=None):
    """Return (joints, angles) clamped to their allowed range.

    Unknown joints and leg joints are dropped; NaN angles too.
    """
    out_j, out_a = [], []
    for j, a in zip(joints, angles):
        rng = _joint_range(j, limits, box)
        if rng is None or a != a:  # a != a: NaN
            print("[motion] joint refuse : {}".format(j), file=sys.stderr)
            continue
        out_j.append(j)
        out_a.append(_clamp(float(a), rng[0], rng[1]))
    return out_j, out_a


# ── Motor profile ─────────────────────────────────────────────────────────────
# Constrained motor universe: the NAO stays seated and only moves three chains.
# For each one the profile gives a rest pose, a start pose, a [min, max] range
# per joint and a speed.  Every move is rest -> start -> target -> start -> rest
# (decided by the NAO driver); the bridge only refuses anything outside the
# box that holds the range, the rest pose and the start pose.  Same chains as
# motion_policy.CHAINS in the driver.  No wrist or hand in this universe.
_CHAINS = {
    "head": ("HeadYaw", "HeadPitch"),
    "larm": ("LShoulderPitch", "LShoulderRoll", "LElbowYaw", "LElbowRoll"),
    "rarm": ("RShoulderPitch", "RShoulderRoll", "RElbowYaw", "RElbowRoll"),
}


def _validate_chain(name, cfg, limits):
    """Return (clean chain, error).  Clean = floats, ranges inside the
    hardware range, start pose inside its range, speed within the limits."""
    if name not in _CHAINS or not isinstance(cfg, dict):
        return None, "chaine inconnue : {}".format(name)
    joints = _CHAINS[name]
    try:
        rest = dict((j, float(cfg["rest"][j])) for j in joints)
        start = dict((j, float(cfg["start"][j])) for j in joints)
        rng = dict((j, [float(cfg["range"][j][0]), float(cfg["range"][j][1])]) for j in joints)
        speed = float(cfg["speed"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None, "{} : repos, depart, plage ou vitesse manquant".format(name)
    for j in joints:
        hw_lo, hw_hi = _JOINT_HW_LIMITS[j]
        lo, hi = rng[j]
        if not hw_lo <= lo <= hi <= hw_hi:
            return None, "{} : plage de {} invalide".format(name, j)
        if not lo - 1e-3 <= start[j] <= hi + 1e-3:
            return None, "{} : depart de {} hors plage".format(name, j)
        if not hw_lo <= rest[j] <= hw_hi:
            return None, "{} : repos de {} hors limites".format(name, j)
    if speed != speed or speed <= 0:
        return None, "{} : vitesse invalide".format(name)
    speed = _clamp_speed(speed, limits)
    return {"rest": rest, "start": start, "range": rng, "speed": speed}, ""


def _validate_profile(profile, limits):
    """Return (clean profile or None, errors).  Invalid chains are left out:
    they do not move.  None when no chain is valid."""
    if not isinstance(profile, dict) or not isinstance(profile.get("chains"), dict):
        return None, ["profil sans chaines"]
    chains, errors = {}, []
    for name, cfg in profile["chains"].items():
        clean, error = _validate_chain(name, cfg, limits)
        if clean is None:
            errors.append(error)
        else:
            chains[name] = clean
    if not chains:
        return None, errors
    out = dict(profile)
    out["chains"] = chains
    return out, errors


def _profile_box(profile):
    """{joint: (min, max)} holding each chain's range, rest and start poses."""
    box = {}
    for cfg in profile["chains"].values():
        for j, (lo, hi) in cfg["range"].items():
            box[j] = (min(lo, cfg["rest"][j], cfg["start"][j]),
                      max(hi, cfg["rest"][j], cfg["start"][j]))
    return box


def _load_json(path):
    """Content of a JSON file, None if absent or unreadable."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except IOError:
        return None
    except ValueError as e:
        print("[bridge] {} illisible : {}".format(path, e), file=sys.stderr)
        return None


def _write_json(path, data):
    """Write through a temporary file: a crash mid-write keeps the old file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.rename(tmp, path)


# ── NAOqi components switched from the régie ──────────────────────────────────
# (name, label, default).  Everything that moves the robot on its own is off by
# default; smart stiffness and the diagnosis effect protect the motors, so
# they stay on unless switched off from the régie.  fall_manager and
# arm_collision_protection take their default from motion_limits.
_COMPONENTS = [
    ("fall_manager",             "Fall manager (protection de chute)", False),
    ("arm_collision_protection", "Anti-collision des bras", False),
    ("external_collision",       "Anti-collision externe", False),
    ("push_recovery",            "Rattrapage de poussee", False),
    ("breath",                   "Respiration", False),
    ("idle_posture",             "Posture d'attente", False),
    ("basic_awareness",          "Basic awareness (suivi visages et sons)", False),
    ("expressive_listening",     "Ecoute expressive", False),
    ("smart_stiffness",          "Smart stiffness (moins de raideur a l'arret)", True),
    ("diagnosis_effect",         "Effet de diagnostic (relache un moteur en defaut)", True),
]
_COMPONENT_NAMES = [c[0] for c in _COMPONENTS]


def _set_component(name, on, proxy_factory):
    """Switch one NAOqi component; raises on a NAOqi error (e.g. the fall
    manager cannot be disabled until allowed in the robot's web page)."""
    on = bool(on)
    if name == "basic_awareness":
        proxy = proxy_factory("ALBasicAwareness")
        if proxy:
            (proxy.startAwareness if on else proxy.stopAwareness)()
        return
    if name == "expressive_listening":
        proxy = proxy_factory("ALAutonomousMoves")
        if proxy:
            proxy.setExpressiveListeningEnabled(on)
        return
    motion = proxy_factory("ALMotion")
    if not motion:
        return
    if name == "fall_manager":
        motion.setFallManagerEnabled(on)
    elif name == "arm_collision_protection":
        motion.setCollisionProtectionEnabled("Arms", on)
    elif name == "external_collision":
        motion.setExternalCollisionProtectionEnabled("All", on)
    elif name == "push_recovery":
        motion.setPushRecoveryEnabled(on)
    elif name == "breath":
        motion.setBreathEnabled("Body", on)
    elif name == "idle_posture":
        motion.setIdlePostureEnabled("Body", on)
    elif name == "smart_stiffness":
        motion.setSmartStiffnessEnabled(on)
    elif name == "diagnosis_effect":
        motion.setDiagnosisEffectEnabled(on)


class _MotionGuard(object):
    """Hard bounds between the bridge and ALMotion for joint moves.

    Angles and speed are clamped, then sent with the non-blocking
    ALMotion.setAngles so the WebSocket receive thread never waits on a
    motion.  No decision is taken here: rate, end of move and dropped
    orders are handled by the NAO driver.
    """

    def __init__(self, limits, proxy_factory=None):
        self.limits = limits
        self.box = None   # motor profile box, None without a profile
        self._proxy_factory = proxy_factory or (
            lambda: get_proxy("ALMotion") if HAS_NAOQI else None)

    def submit(self, joints, angles, speed):
        """Clamp then send a joint move; returns the number of joints sent."""
        joints, angles = _clamp_joints(joints, angles, self.limits, self.box)
        if not joints:
            return 0
        speed = _clamp_speed(speed, self.limits)
        proxy = self._proxy_factory()
        if proxy is None:
            if not HAS_NAOQI:
                for j, a in zip(joints, angles):
                    print("[MOCK] motion_joint: {} -> {:.3f} rad speed={}".format(j, a, speed),
                          file=sys.stderr)
            return len(joints)
        try:
            if self.limits["motion_call"] == "legacy":
                for grp in set(_JOINT_TO_GROUP.get(j, "head") for j in joints):
                    proxy.setStiffnesses(_NAOQI_GROUP[grp], 1.0)
                proxy.angleInterpolationWithSpeed(joints, angles, speed)
            else:
                proxy.setAngles(joints, angles, speed)
        except Exception as e:
            print("[motion] {} error: {}".format(self.limits["motion_call"], e), file=sys.stderr)
        return len(joints)


_BROKER      = None  # ALBroker instance — must stay alive for NAOqi callbacks
_CAPTURE_REF = None  # strong ref to active _AudioCapture, prevents GC during recording

_PROXIES      = {}   # service name → ALProxy, see get_proxy()
_PROXIES_LOCK = threading.Lock()


def _xor_mask(data, mask_key):
    """XOR data with a 4-byte WebSocket mask key (RFC 6455 §5.3).

    Done on one big integer instead of byte by byte: a Python loop over a
    base64 audio payload keeps NAO's CPU busy for a noticeable time.
    """
    n = len(data)
    if not n:
        return b""
    key = (bytes(mask_key) * (n // 4 + 1))[:n]
    x = int(binascii.hexlify(data), 16) ^ int(binascii.hexlify(key), 16)
    return binascii.unhexlify("%0*x" % (2 * n, x))


# ======================================================================
# Pure Python 2.7 WebSocket client (RFC 6455, ws:// only)
# Replaces websocket-client to avoid any dependency on the ssl module,
# which may not be available on all OpenNAO firmware builds.
# ======================================================================

class _WebSocketApp(object):

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url, on_open=None, on_message=None,
                 on_error=None, on_close=None, token=None):
        self.url = url
        self.token = token
        self.on_open = on_open
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self._sock = None
        self._closed = False
        # Status, audio and receive threads all send: frames must not interleave.
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(self, data):
        if isinstance(data, unicode):
            data = data.encode("utf-8")
        self._send_frame(data, opcode=0x1)

    def close(self):
        self._closed = True
        if self._sock:
            try:
                self._send_frame(b"", opcode=0x8)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass

    def run_forever(self, **kwargs):
        try:
            self._connect()
        except Exception as exc:
            if self.on_error:
                self.on_error(self, exc)
            if self.on_close:
                self.on_close(self)
            return

        if self.on_open:
            self.on_open(self)

        try:
            self._recv_loop()
        except Exception as exc:
            if not self._closed and self.on_error:
                self.on_error(self, exc)

        if self.on_close:
            self.on_close(self)

    # ------------------------------------------------------------------
    # TCP connection + HTTP/WebSocket handshake (RFC 6455 §4)
    # ------------------------------------------------------------------

    def _connect(self):
        """Open a TCP socket and complete the RFC 6455 WebSocket handshake."""
        # TLS is not supported here — ssl module may be absent on OpenNAO firmware.
        # The caller (NaoBridge.run) is responsible for providing a ws:// URL.
        if not self.url.startswith("ws://"):
            raise ValueError("Only ws:// is supported (got: {})".format(self.url))
        tail = self.url[5:]

        # Split optional path from host:port — default to "/" if no path given.
        if "/" in tail:
            host_port, path = tail.split("/", 1)
            path = "/" + path
        else:
            host_port, path = tail, "/"

        # Split optional port from host — default to 80 for plain HTTP/WS.
        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        else:
            host, port = host_port, 80

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(10)
        self._sock.connect((host, port))
        self._sock.settimeout(None)

        raw_key = base64.b64encode(os.urandom(16)).decode("ascii")
        # rosbridge-proxy only upgrades connections carrying the shared token.
        auth = "Authorization: Bearer {}\r\n".format(self.token) if self.token else ""
        req = (
            "GET {path} HTTP/1.1\r\n"
            "Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "{auth}"
            "\r\n"
        ).format(path=path, host=host, port=port, key=raw_key, auth=auth)
        self._sock.sendall(req.encode("ascii"))

        resp = b""
        # Read the HTTP upgrade response until the header terminator is found.
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            # Server closed the connection before completing the HTTP handshake.
            if not chunk:
                raise IOError("Connection closed during WebSocket handshake")
            resp += chunk

        status_line = resp.split(b"\r\n")[0]
        # HTTP 101 Switching Protocols is the only acceptable response to an
        # Upgrade: websocket request; anything else is a server-side rejection.
        if b"101" not in status_line:
            raise IOError("WebSocket handshake failed: {}".format(
                status_line.decode("ascii", "replace")))

        # Verify the server's Sec-WebSocket-Accept header (RFC 6455 §4.1).
        # The server must echo back SHA-1(key + GUID) in base64 to prove it
        # understood the WebSocket protocol — guards against plain HTTP servers.
        expected = base64.b64encode(
            hashlib.sha1(
                (raw_key + self._GUID).encode("ascii")
            ).digest()
        ).decode("ascii")
        if expected not in resp.decode("ascii", "replace"):
            raise IOError("Invalid Sec-WebSocket-Accept")

    # ------------------------------------------------------------------
    # WebSocket frame encoding/decoding (RFC 6455 §5)
    # ------------------------------------------------------------------

    def _send_frame(self, data, opcode=0x1):
        if isinstance(data, unicode):
            data = data.encode("utf-8")
        mask_key = os.urandom(4)
        length = len(data)

        header = bytearray()
        header.append(0x80 | opcode)
        # RFC 6455 §5.2 payload length encoding:
        # < 126 bytes  → inline 7-bit value
        # < 65536 bytes → marker 126 + 16-bit big-endian length
        # else          → marker 127 + 64-bit big-endian length
        if length < 126:
            header.append(0x80 | length)        # mask bit set (clients must mask)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask_key)

        frame = bytes(header) + _xor_mask(data, mask_key)
        with self._send_lock:
            self._sock.sendall(frame)

    def _recv_exact(self, n):
        """Read exactly n bytes from the socket, blocking until complete."""
        chunks = []
        remaining = n
        # Collect chunks and join once: "buf += chunk" copies the whole buffer
        # on every recv, which is quadratic on large audio frames.
        while remaining:
            chunk = self._sock.recv(min(remaining, 65536))
            if not chunk:
                raise IOError("Connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _recv_loop(self):
        """Receive and decode WebSocket frames until the connection closes."""
        while not self._closed:
            b0, b1 = bytearray(self._recv_exact(2))
            opcode = b0 & 0x0F
            is_masked = (b1 & 0x80) != 0
            length = b1 & 0x7F
            # Decode extended payload length fields per RFC 6455 §5.2.
            if length == 126:
                # 16-bit extended payload length follows.
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                # 64-bit extended payload length follows.
                length = struct.unpack(">Q", self._recv_exact(8))[0]

            # Server-to-client frames are usually unmasked; read key only if masked.
            mask_key = self._recv_exact(4) if is_masked else None
            payload = self._recv_exact(length)

            # Unmask payload when a masking key was provided.
            if mask_key:
                payload = _xor_mask(payload, mask_key)

            if opcode == 0x1:   # text frame — dispatch as decoded string
                if self.on_message:
                    self.on_message(self, payload.decode("utf-8", "replace"))
            elif opcode == 0x2: # binary frame — dispatch as raw bytes
                if self.on_message:
                    self.on_message(self, payload)
            elif opcode == 0x8: # close frame — server initiated close, exit loop
                self._closed = True
                break
            elif opcode == 0x9: # ping → must reply with pong (opcode 0xA) per RFC 6455
                self._send_frame(payload, opcode=0xA)


# ======================================================================
# Bridge NAO ↔ rosbridge
# ======================================================================

def _bridge_config(config_path):
    """Return the "bridge" section of the provisioning file ({} if absent).

    Keys: "modules" (list of module names) and "audio_capture" (bool).
    """
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception:
        return {}  # already reported by _provision()
    section = config.get("bridge", {})
    if not isinstance(section, dict):
        return {}
    if "modules" in section and not isinstance(section["modules"], list):
        print("[provision] bridge.modules must be a list — ignored.", file=sys.stderr)
        section = dict(section)
        del section["modules"]
    return section


def _provision(config_path, tts=True, asr=True):
    """Apply post-flash configuration from config_path (JSON file).

    tts / asr: configure the NAOqi TTS / speech recognition language.  Off when
    the bridge does not use them: setLanguage loads the language engine into
    NAOqi's memory for nothing (the NAO is silent since ADR 004).
    """
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception as e:
        print("[provision] Error reading {}: {}".format(config_path, e), file=sys.stderr)
        return False

    print("[provision] Applying {} ...".format(config_path), file=sys.stderr)
    ok = True

    # -- Autonomous life -----------------------------------------------
    al_cfg = config.get("autonomous_life", {})
    if "state" in al_cfg:
        state = str(al_cfg["state"])
        valid_states = ("disabled", "solitary", "interactive", "safeguard")
        if state not in valid_states:
            print("[provision] autonomous_life.state '{}' invalid (valid: {}).".format(
                state, ", ".join(valid_states)), file=sys.stderr)
        else:
            proxy = get_proxy("ALAutonomousLife")
            if proxy:
                try:
                    proxy.setState(state)
                    print("[provision] ALAutonomousLife -> '{}'.".format(state), file=sys.stderr)
                except Exception as e:
                    print("[provision] ALAutonomousLife error: {}".format(e), file=sys.stderr)
                    ok = False

    # -- Language packages --------------------------------------------
    # .pkg files are copied to /tmp (only writable space available on NAO).
    # Installation uses qicli (PackageManager, always present on NAO OS).
    lang_cfg = config.get("language", {})
    if lang_cfg and not (tts or asr):
        print("[provision] Audio off — TTS/ASR language skipped.", file=sys.stderr)
    elif lang_cfg:
        import glob as _glob
        import subprocess as _subprocess
        pkgs = sorted(_glob.glob("/tmp/*.pkg"))
        if pkgs:
            for pkg_path in pkgs:
                try:
                    ret = _subprocess.call(
                        ["qicli", "call", "PackageManager.install", pkg_path]
                    )
                    if ret == 0:
                        print("[provision] Package installed: {}.".format(
                            os.path.basename(pkg_path)), file=sys.stderr)
                    else:
                        print("[provision] Install error {} (code {}).".format(
                            os.path.basename(pkg_path), ret), file=sys.stderr)
                        ok = False
                except OSError:
                    print("[provision] qicli not found — packages skipped.", file=sys.stderr)
                    ok = False
                    break
        else:
            print("[provision] No .pkg files found in /tmp.", file=sys.stderr)

        if "tts" in lang_cfg and tts:
            lang = str(lang_cfg["tts"])
            proxy = get_proxy("ALTextToSpeech")
            if proxy:
                try:
                    available = proxy.getAvailableLanguages()
                    if lang in available:
                        proxy.setLanguage(lang)
                        print("[provision] TTS language -> '{}'.".format(lang), file=sys.stderr)
                    else:
                        print("[provision] TTS: '{}' not available (available: {}).".format(
                            lang, ", ".join(available)), file=sys.stderr)
                        ok = False
                except Exception as e:
                    print("[provision] TTS error: {}".format(e), file=sys.stderr)
                    ok = False

        if "asr" in lang_cfg and asr:
            lang = str(lang_cfg["asr"])
            proxy = get_proxy("ALSpeechRecognition")
            if proxy:
                try:
                    proxy.setLanguage(lang)
                    print("[provision] ASR language -> '{}'.".format(lang), file=sys.stderr)
                except Exception as e:
                    print("[provision] ASR error: {}".format(e), file=sys.stderr)
                    ok = False

    # -- System settings ----------------------------------------------
    sys_cfg = config.get("system", {})
    if "volume" in sys_cfg:
        vol = int(sys_cfg["volume"])
        if not (0 <= vol <= 100):
            print("[provision] Volume {} out of range [0-100].".format(vol), file=sys.stderr)
        else:
            proxy = get_proxy("ALAudioDevice")
            if proxy:
                try:
                    proxy.setOutputVolume(vol)
                    print("[provision] Volume -> {}.".format(vol), file=sys.stderr)
                except Exception as e:
                    print("[provision] Volume error: {}".format(e), file=sys.stderr)
                    ok = False

    status = "OK" if ok else "finished with errors"
    print("[provision] Provisioning {}.".format(status), file=sys.stderr)
    return ok


def get_proxy(service):
    """Return a cached ALProxy for service, or None if it is unavailable.

    Creating an ALProxy is a round trip to NAOqi that fetches the module's
    method list: doing it for every command (20 Hz with motion_engine) and
    every sensor read loads naoqi for nothing.
    """
    with _PROXIES_LOCK:
        proxy = _PROXIES.get(service)
        if proxy is not None:
            return proxy
        try:
            proxy = ALProxy(service, NAO_IP, NAO_PORT)
        except Exception as e:
            print("[bridge] Proxy {} indisponible : {}".format(service, e), file=sys.stderr)
            return None
        _PROXIES[service] = proxy
        return proxy


def _ws_log(direction, msg):
    """Print a human-readable WebSocket exchange summary to stderr."""
    op    = msg.get("op", "?")
    topic = msg.get("topic", "")
    if op == "publish" and topic in _QUIET_TOPICS and not _VERBOSE:
        return
    inner = msg.get("msg", msg.get("data", {}))
    data = inner.get("data") if isinstance(inner, dict) else None
    # Large string payloads (base64 audio): cut before serialising rather
    # than dumping hundreds of kB just to keep the first 200 characters.
    if isinstance(data, (str, type(u""))) and len(data) > 200:
        inner = dict(inner, data=data[:200])
    summary = json.dumps(inner, ensure_ascii=False)
    # Truncate large payloads (e.g. base64 audio) to keep logs readable
    if len(summary) > 200:
        summary = u"{}… [truncated, {} bytes]".format(summary[:200], len(summary))
    ts = time.strftime("%H:%M:%S")
    print(u"[WS {dir}] {ts}  op={op}  topic={topic}  {summary}".format(
        dir=direction, ts=ts, op=op, topic=topic, summary=summary,
    ).encode("utf-8", "replace"), file=sys.stderr)


def _ensure_broker():
    """Create the ALBroker singleton if not yet initialised.

    The broker is required before registering any ALModule subclass
    (_AudioCapture). It must stay alive for the lifetime of the capture session.
    """
    global _BROKER
    # Already initialised — reuse the singleton rather than creating a second broker.
    if _BROKER is not None:
        return True
    # Mock mode or missing NAOqi SDK — broker cannot be created.
    if not HAS_NAOQI or ALBroker is None:
        return False
    try:
        _BROKER = ALBroker("naoBridgeBroker", "0.0.0.0", 0, NAO_IP, NAO_PORT)
        return True
    except Exception as e:
        print("[bridge] ALBroker init error: {}".format(e), file=sys.stderr)
        return False


class _AudioCapture(ALModule):
    """NAOqi module that receives audio frames and detects end-of-speech via VAD.

    Registered with ALAudioDevice to receive raw 16-bit PCM at 16 kHz.
    Accumulates samples while the RMS level exceeds _SPEECH_RMS, then stops
    after _SILENCE_SEC of consecutive silence below _SILENCE_RMS.
    """

    _RATE         = 16000  # sample rate expected by Whisper (and NAO's minimum)
    _SPEECH_RMS   = 600    # RMS threshold that triggers speech capture
    _SILENCE_RMS  = 200    # RMS below which a frame is considered silence
    _SILENCE_SEC  = 1.5    # consecutive silent seconds needed to stop recording
    _SAMPLES_CALL = 170    # samples per NAO audio callback at 16 kHz (firmware default)
    _MAX_SEC      = 10.0   # hard safety timeout to prevent infinite recording

    def __init__(self, name):
        ALModule.__init__(self, name)
        self._name     = name
        self._pcm      = []
        self._speaking = False
        self._sil_n    = 0
        self._sil_need = int(self._SILENCE_SEC * self._RATE / self._SAMPLES_CALL)
        self._done     = threading.Event()

    def processRemote(self, nb_ch, nb_samp, _ts, buffer):
        """NAOqi audio callback: accumulate PCM frames and detect end-of-speech.

        Called by ALAudioDevice at _RATE Hz.  We keep only the first (front) channel
        by striding through the interleaved multichannel buffer.
        """
        # Stop collecting once the VAD has already signalled completion.
        if self._done.is_set():
            return
        try:
            # Fast path: buffer is already a sequence of ints (most NAO firmware versions).
            samples = [buffer[i] for i in range(0, nb_ch * nb_samp, nb_ch)]
        except TypeError:
            # Slow path: buffer is a raw bytes object — unpack as signed 16-bit LE.
            all_s = struct.unpack_from("<{}h".format(nb_ch * nb_samp), bytes(buffer))
            samples = list(all_s[::nb_ch])
        # Guard: empty frame (should not happen, but avoids div-by-zero below).
        if not samples:
            return
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        if not self._speaking:
            # Wait for the RMS to exceed the speech threshold before recording.
            if rms > self._SPEECH_RMS:
                self._speaking = True
                self._pcm.extend(samples)
        else:
            # Already in speech: accumulate samples and watch for trailing silence.
            self._pcm.extend(samples)
            if rms < self._SILENCE_RMS:
                # Frame is silent — count consecutive silent frames.
                self._sil_n += 1
                # Enough consecutive silence frames → speech segment is complete.
                if self._sil_n >= self._sil_need:
                    self._done.set()
            else:
                # Frame is loud again — reset silence counter.
                self._sil_n = 0

    def to_wav(self):
        import io as _io
        if not self._pcm:
            return b""
        raw = struct.pack("<{}h".format(len(self._pcm)), *self._pcm)
        buf = _io.BytesIO()
        buf.write(b"RIFF")
        buf.write(struct.pack("<I", 36 + len(raw)))
        buf.write(b"WAVE")
        buf.write(b"fmt ")
        buf.write(struct.pack("<IHHIIHH", 16, 1, 1,
                              self._RATE, self._RATE * 2, 2, 16))
        buf.write(b"data")
        buf.write(struct.pack("<I", len(raw)))
        buf.write(raw)
        return buf.getvalue()


class NaoBridge(object):
    """Main bridge class: manages the WebSocket connection and dispatches commands.

    Args:
        vps_url       : rosbridge WebSocket URL (e.g. ws://DRONE_IP:9090/, static drone relay).
        token         : shared secret expected by rosbridge-proxy (None = no header).
        modules       : set of module names to enable (None = DEFAULT_MODULES).
        audio_capture : enable wake word detection and VAD capture.
        tts_method    : "mp3" | "text" | "auto".
                        "auto" → mp3 if NAOqi available, otherwise text.
        motion_limits : "motion_limits" section of the provisioning file.
        motor_profile : path of the motor profile (written by the calibration).
        components    : path of the NAOqi components state (switched from the régie).
    """

    def __init__(self, vps_url, modules=None, audio_capture=False, tts_method="auto",
                 token=None, motion_limits=None, motor_profile=_MOTOR_PROFILE_DEFAULT,
                 components=_COMPONENTS_DEFAULT):
        self.vps_url = vps_url
        self._token = token
        self._modules = DEFAULT_MODULES if modules is None else frozenset(modules)
        self._audio_capture_enabled = audio_capture
        self._tts_method_override = tts_method
        self.ws = None
        self._stop = threading.Event()
        self._busy_until = 0.0          # timestamp until which wake word is ignored (during TTS)
        self._connected = threading.Event()  # set while a WebSocket session is open
        self._status_started = False     # one status thread for the bridge's lifetime
        self._wakeword_started = False   # prevents relaunching the wake-word thread on reconnect
        self._asr = None                 # ASR proxy kept alive for unsubscribe on shutdown
        self._audio_capture = None       # active _AudioCapture instance during VAD recording
        # Groups whose stiffness has been explicitly enabled (others are skipped for motion)
        self._stiffness_enabled = set()
        self._stiffness_lock = threading.Lock()
        # Chains left limp at rest (rest_limp): an order wakes them up.
        self._limp = set()
        self._motion_seq = {}   # chain -> number of the last order, see _release_at_rest
        # Guard rails between incoming motion commands and ALMotion.
        self._limits = _motion_limits(motion_limits)
        self._guard = _MotionGuard(self._limits)
        # Head and arm angles read at launch, reported for information.
        self._launch_pose = {}
        # NAOqi proxies, replaceable in tests.
        self._proxy = lambda name: get_proxy(name) if HAS_NAOQI else None
        # Run a long step (calibration capture, replay) off the WebSocket thread.
        self._spawn = lambda fn, *args: threading.Thread(target=fn, args=args).start()
        self._sleep = time.sleep
        self._profile_path = motor_profile
        self._profile = None
        self._load_profile()
        self._components_path = components
        self._components = {}   # name -> {"enabled", "ok", "error"}
        self._calib = None      # calibration in progress, see _calibration()
        self._calib_lock = threading.Lock()

    def _get_tts_method(self):
        """Return the effective TTS method, or None when the NAO does not speak.

        "auto" selects mp3 when NAOqi is available (real robot) and audio_out
        is on, otherwise falls back to text — useful for development/mock mode
        on a non-NAO host.
        """
        # Neither speech module on (default since ADR 004): nothing to negotiate.
        if not self._modules & _AUDIO_MODULES:
            return None
        # Explicit override takes precedence over auto-detection.
        if self._tts_method_override != "auto":
            return self._tts_method_override
        # Auto: mp3 requires NAOqi ALAudioPlayer and the audio_out module.
        return "mp3" if HAS_NAOQI and "audio_out" in self._modules else "text"

    # ------------------------------------------------------------------
    # Connection / auto-reconnect loop
    # ------------------------------------------------------------------

    def run(self):
        """Connect and run the WebSocket loop; reconnect on disconnect.

        Blocks until stop() is called.  On each connection loss, waits
        RECONNECT_DELAY seconds before retrying (exponential backoff is not
        needed here — the NAO network is typically on a stable LAN).
        """
        self._apply_motion_safety()
        while not self._stop.is_set():
            print("[bridge] Connexion a {} ...".format(self.vps_url), file=sys.stderr)
            try:
                self.ws = _WebSocketApp(
                    self.vps_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    token=self._token,
                )
                self.ws.run_forever()
            except Exception as e:
                print("[bridge] Erreur WebSocket : {}".format(e), file=sys.stderr)

            # Only sleep if stop() has not been requested — avoids a spurious
            # delay when the user presses Ctrl+C.
            if not self._stop.is_set():
                print("[bridge] Reconnexion dans {}s ...".format(RECONNECT_DELAY), file=sys.stderr)
                time.sleep(RECONNECT_DELAY)

    def _apply_motion_safety(self):
        """Switch the NAOqi components, read the launch pose, go to rest."""
        print("[motion] Bornes : vitesse <= {}, marge {} rad, marche <= {}".format(
            self._limits["max_speed"], self._limits["joint_margin"],
            self._limits["walk_max"]), file=sys.stderr)
        self._apply_components()
        proxy = self._proxy("ALMotion")
        if not proxy:
            return
        names = sorted(_JOINT_HW_LIMITS)
        try:
            # True: sensor values, i.e. where the joints really are.
            angles = proxy.getAngles(names, True)
            self._launch_pose = dict((n, round(float(a), 4)) for n, a in zip(names, angles))
        except Exception as e:
            print("[motion] pose de lancement non lue : {}".format(e), file=sys.stderr)
        self._go_to_rest(sorted(self._profile["chains"]) if self._profile else [])

    # ------------------------------------------------------------------
    # Motor profile
    # ------------------------------------------------------------------

    def _load_profile(self):
        """Read the motor profile; without one, no joint moves (driver and bridge)."""
        raw = _load_json(self._profile_path)
        profile, errors = _validate_profile(raw, self._limits) if raw is not None else (None, [])
        for e in errors:
            print("[motion] profil moteur : {}".format(e), file=sys.stderr)
        self._set_profile(profile)
        if profile is None:
            print("[motion] pas de profil moteur ({}) : aucun geste avant un calibrage "
                  "depuis la regie.".format(self._profile_path), file=sys.stderr)
        else:
            print("[motion] profil moteur : {}".format(", ".join(sorted(profile["chains"]))),
                  file=sys.stderr)

    def _set_profile(self, profile):
        self._profile = profile
        # Without a profile the box is empty: the guard refuses every joint.
        self._guard.box = _profile_box(profile) if profile else {}

    def _hold_here(self, motion, joints):
        """Stiffness on without a jump: first make the current position the target."""
        group = _NAOQI_GROUP[_JOINT_TO_GROUP[joints[0]]]
        angles = motion.getAngles(list(joints), True)
        motion.setAngles(list(joints), [float(a) for a in angles], float(self._limits["min_speed"]))
        motion.setStiffnesses(group, float(self._limits["stiffness_max"]))

    def _go_to_rest(self, chains):
        """Stiffness on and back to the rest pose of each chain (blocking).

        At startup (after a crash too) and at the end of a calibration: the
        NAO driver resets its sequences on the next /nao/capabilities and
        assumes the robot is at rest.
        """
        motion = self._proxy("ALMotion")
        for name in chains:
            cfg = self._profile["chains"][name] if self._profile else None
            if not cfg:
                continue
            joints = list(_CHAINS[name])
            with self._stiffness_lock:
                self._stiffness_enabled.add(name)
            if not motion:
                print("[MOCK] repos {}".format(name), file=sys.stderr)
                continue
            try:
                self._hold_here(motion, joints)
                motion.angleInterpolationWithSpeed(
                    joints, [cfg["rest"][j] for j in joints], cfg["speed"])
            except Exception as e:
                print("[motion] retour au repos {} : {}".format(name, e), file=sys.stderr)
                continue
            if self._limits["rest_limp"]:
                self._go_limp(name)

    def _go_limp(self, chain):
        """Stiffness off on a chain at rest; it stays usable (see _wake_chain)."""
        motion = self._proxy("ALMotion")
        try:
            if motion:
                motion.setStiffnesses(_NAOQI_GROUP[chain], 0.0)
            else:
                print("[MOCK] {} sans raideur au repos".format(chain), file=sys.stderr)
        except Exception as e:
            print("[motion] raideur {} : {}".format(chain, e), file=sys.stderr)
            return
        with self._stiffness_lock:
            self._stiffness_enabled.discard(chain)
            self._limp.add(chain)

    def _wake_chain(self, chain):
        """Stiffness back on a chain left limp at rest, without a jump."""
        motion = self._proxy("ALMotion")
        try:
            if motion:
                self._hold_here(motion, _CHAINS[chain])
        except Exception as e:
            print("[motion] raideur {} : {}".format(chain, e), file=sys.stderr)
            return
        with self._stiffness_lock:
            self._limp.discard(chain)
            self._stiffness_enabled.add(chain)

    def _release_at_rest(self, chain, seq, delay):
        """Order sent by the driver toward the rest pose: once it is reached
        and no newer order came for the chain, cut its stiffness."""
        self._sleep(max(0.0, delay) + 0.3)
        if self._motion_seq.get(chain) != seq or self._calib_chain() == chain:
            return
        with self._stiffness_lock:
            if chain not in self._stiffness_enabled:
                return
        self._go_limp(chain)

    def _capabilities(self):
        tts_method = self._get_tts_method()
        caps = {"tts_method": tts_method} if tts_method else {}
        if self._launch_pose:
            caps["launch_pose"] = self._launch_pose
        caps["motor_profile"] = self._profile
        return caps

    def _publish(self, topic, payload):
        self._send({"op": "publish", "topic": topic, "msg": {"data": json.dumps(payload)}})

    # ------------------------------------------------------------------
    # NAOqi components (régie)
    # ------------------------------------------------------------------

    def _apply_components(self):
        """Put every component in its saved state (or its default)."""
        saved = _load_json(self._components_path)
        saved = saved if isinstance(saved, dict) else {}
        for name, _label, default in _COMPONENTS:
            if name in ("fall_manager", "arm_collision_protection"):
                default = bool(self._limits[name])
            self._switch_component(name, bool(saved.get(name, default)))

    def _switch_component(self, name, on):
        error = ""
        try:
            _set_component(name, on, self._proxy)
            if not HAS_NAOQI:
                print("[MOCK] composant {} -> {}".format(name, on), file=sys.stderr)
        except Exception as e:
            error = str(e)
            print("[motion] composant {} -> {} : {}".format(name, on, e), file=sys.stderr)
        self._components[name] = {"enabled": bool(on), "ok": not error, "error": error}

    def _components_state(self):
        return {"components": [dict(self._components.get(n, {}), name=n, label=label)
                               for n, label, _ in _COMPONENTS]}

    def _components_command(self, payload):
        """/nao/components : {"name": "...", "enabled": bool} or {"op": "status"}."""
        name = payload.get("name")
        if name in _COMPONENT_NAMES and isinstance(payload.get("enabled"), bool):
            self._switch_component(name, payload["enabled"])
            try:
                _write_json(self._components_path, dict(
                    (n, c["enabled"]) for n, c in self._components.items()))
            except (IOError, OSError) as e:
                print("[motion] {} non ecrit : {}".format(self._components_path, e),
                      file=sys.stderr)
        self._publish("/nao/components_state", self._components_state())

    def stop(self):
        self._stop.set()
        self._cleanup_resources()
        if self.ws:
            self.ws.close()

    def _cleanup_resources(self):
        """Release NAOqi resources: unsubscribe ASR and stop any active VAD capture."""
        if self._audio_capture:
            self._audio_capture._done.set()
            audio_dev = get_proxy("ALAudioDevice")
            if audio_dev:
                try:
                    audio_dev.unsubscribe(self._audio_capture._name)
                    print("[bridge] Capture VAD arrêtée.", file=sys.stderr)
                except Exception as e:
                    print("[bridge] Erreur arrêt VAD: {}".format(e), file=sys.stderr)
            self._audio_capture = None

        if self._asr:
            try:
                self._asr.unsubscribe("nao_ws_bridge_ww")
                print("[bridge] ASR désabonné.", file=sys.stderr)
            except Exception as e:
                print("[bridge] Erreur désabonnement ASR: {}".format(e), file=sys.stderr)
            self._asr = None

    # ------------------------------------------------------------------
    # WebSocket send with structured logging
    # ------------------------------------------------------------------

    def _send(self, payload_dict):
        _ws_log(u"→", payload_dict)
        if self.ws and self._connected.is_set() and not self._stop.is_set():
            self.ws.send(json.dumps(payload_dict))

    def _on_open(self, ws):
        active = sorted(self._modules)
        print("[bridge] Connecte. Modules actifs : {}. Capture audio : {}.".format(
            ", ".join(active) if active else "aucun",
            "oui" if self._audio_capture_enabled else "non",
        ), file=sys.stderr)

        # Subscribe to command topics for each enabled module
        for mod, (topic, msg_type) in _MODULE_TOPICS.items():
            if mod in self._modules:
                sub_msg = {"op": "subscribe", "topic": topic, "type": msg_type}
                _ws_log(u"→", sub_msg)
                ws.send(json.dumps(sub_msg))

        # Subscribe to system topics unconditionally (e.g. /nao/shutdown)
        for topic, msg_type in _SYSTEM_TOPICS:
            sub_msg = {"op": "subscribe", "topic": topic, "type": msg_type}
            _ws_log(u"→", sub_msg)
            ws.send(json.dumps(sub_msg))

        # Advertise topics published by this bridge
        advertised = ["/nao/status", "/nao/capabilities",
                      "/nao/calibration_state", "/nao/components_state"]
        if self._audio_capture_enabled:
            advertised.append("/nao/audio")
        for topic in advertised:
            adv_msg = {"op": "advertise", "topic": topic, "type": "std_msgs/String"}
            _ws_log(u"→", adv_msg)
            ws.send(json.dumps(adv_msg))

        # Immediately announce TTS capability so the driver can negotiate the
        # method, and the motor profile the driver builds its sequences on.
        tts_method = self._get_tts_method()
        caps = self._capabilities()
        cap_payload = json.dumps(caps)
        cap_msg = {"op": "publish", "topic": "/nao/capabilities",
                   "msg": {"data": cap_payload}}
        _ws_log(u"→", cap_msg)
        ws.send(json.dumps(cap_msg))
        if tts_method:
            print("[bridge] Capacites TTS annoncees : {}".format(tts_method), file=sys.stderr)
        else:
            print("[bridge] Pas de TTS annonce (speech et audio_out coupes).", file=sys.stderr)

        print("[bridge] Pret.", file=sys.stderr)
        self._connected.set()
        self._publish("/nao/components_state", self._components_state())
        self._publish("/nao/calibration_state", self._calibration_state())
        # Started once: a thread per connection used to pile up on every reconnect.
        if not self._status_started:
            self._status_started = True
            threading.Thread(target=self._status_loop).start()
        if self._audio_capture_enabled and not self._wakeword_started:
            self._wakeword_started = True
            threading.Thread(target=self._wakeword_loop).start()

    def _on_message(self, ws, raw):
        """Handle an incoming WebSocket frame from rosbridge.

        rosbridge uses the "publish" op to forward topic messages.
        All other ops (service_response, set_level, etc.) are ignored here.
        """
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        _ws_log(u"←", msg)
        # Only "publish" ops carry robot commands — skip status and control messages.
        if msg.get("op") != "publish":
            return
        topic = msg.get("topic", "")
        data = msg.get("msg", {})
        self._dispatch(topic, data)

    def _on_error(self, ws, error):
        print("[bridge] Erreur : {}".format(error), file=sys.stderr)

    def _on_close(self, ws):
        self._connected.clear()
        print("[bridge] Connexion fermee.", file=sys.stderr)

    # ------------------------------------------------------------------
    # Dispatch incoming ROS topic messages to NAOqi commands
    # ------------------------------------------------------------------

    def _dispatch(self, topic, data):
        """Route an incoming topic message to the appropriate NAOqi command.

        Each branch checks both the topic name AND that the corresponding module
        is enabled — this allows the user to run the bridge with a subset of
        capabilities (e.g. leds + speech only).
        """
        if topic == "/nao/audio_out" and "audio_out" in self._modules:
            b64 = data.get("data", "")
            # Skip empty audio payloads that could arrive during a TTS race.
            if b64:
                threading.Thread(target=self._play_audio, args=(b64,)).start()
        elif topic == "/nao/speech" and "speech" in self._modules:
            text = data.get("data", "")
            # Skip empty text — NAOqi TTS would error on an empty string.
            if text:
                self._say(text)
        elif topic == "/nao/cmd_vel" and "move" in self._modules:
            linear = data.get("linear", {})
            angular = data.get("angular", {})
            self._move(
                float(linear.get("x", 0.0)),
                float(linear.get("y", 0.0)),
                float(angular.get("z", 0.0)),
            )
        elif topic == "/nao/gesture" and "gesture" in self._modules:
            posture = data.get("data", "Stand")
            self._posture(posture)
        elif topic == "/nao/leds" and "led" in self._modules:
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
        elif topic == "/nao/stop" and "stop" in self._modules:
            self._stop_move()
        elif topic == "/nao/shutdown":
            # Always handled regardless of module filtering
            print("[bridge] /nao/shutdown received — clean shutdown.", file=sys.stderr)
            threading.Thread(target=self.stop).start()
        elif topic in ("/nao/calibration", "/nao/components"):
            try:
                payload = json.loads(data.get("data", "{}"))
            except ValueError:
                return
            if not isinstance(payload, dict):
                return
            if topic == "/nao/calibration":
                self._calibration(payload)
            else:
                self._components_command(payload)
        elif topic == "/nao/motion" and "motion" in self._modules:
            try:
                payload = json.loads(data.get("data", "{}"))
            except ValueError:
                return
            # Multi-joint payload (from motion_engine): "joints" key is present.
            if "joints" in payload:
                # rest_in: the driver sends the chain back to rest, reached in that many s.
                rest_in = payload.get("rest_in")
                self._motion_joints(
                    [str(j) for j in payload["joints"]],
                    [float(a) for a in payload["angles"]],
                    float(payload.get("speed", 0.05)),
                    float(rest_in) if isinstance(rest_in, (int, float)) else None,
                )
            else:
                self._motion_joint(
                    str(payload.get("joint", "HeadYaw")),
                    float(payload.get("angle", 0.0)),
                    float(payload.get("speed", 0.05)),
                )
        elif topic == "/nao/stiffness" and "stiffness" in self._modules:
            try:
                payload = json.loads(data.get("data", "{}"))
            except ValueError:
                return
            groups = payload.get("groups", [])
            value  = float(payload.get("value", 0.0))
            # Guard: groups must be a list (single-group string would iterate chars).
            # The chain being calibrated keeps the stiffness the calibration sets.
            if isinstance(groups, list):
                calib = self._calib_chain()
                self._set_stiffness([g for g in groups if g != calib], value)

    # ------------------------------------------------------------------
    # NAOqi command implementations
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

    def _play_audio(self, b64_data):
        """Play a base64-encoded MP3 received from the VPS via ALAudioPlayer.

        Bypasses NAOqi TTS entirely — used when tts_method is "mp3"
        and the VPS generates audio with edge-tts.
        """
        import os
        import tempfile
        if not HAS_NAOQI:
            print("[MOCK] play_audio: {} octets base64".format(len(b64_data)), file=sys.stderr)
            return
        try:
            audio_bytes = base64.b64decode(b64_data)
            fd, path = tempfile.mkstemp(suffix=".mp3", prefix="nao_tts_")
            try:
                os.write(fd, audio_bytes)
                os.close(fd)
                proxy = get_proxy("ALAudioPlayer")
                if proxy:
                    proxy.playFile(str(path))
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        except Exception as e:
            print("[bridge] play_audio error: {}".format(e), file=sys.stderr)

    def _move(self, x, y, theta):
        # moveToward takes normalized velocities in [-1, 1]; walk_max caps them.
        cap = float(self._limits["walk_max"])
        x, y, theta = (_clamp(v, -cap, cap) for v in (x, y, theta))
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
        """Start a posture change in its own thread.

        goToPosture blocks for several seconds: running it on the WebSocket
        receive thread would stall every other command meanwhile.  Which
        postures are allowed, and when, is decided by the NAO driver.
        """
        threading.Thread(target=self._run_posture, args=(str(name),)).start()

    def _run_posture(self, name):
        if not HAS_NAOQI:
            print("[MOCK] posture: {}".format(name), file=sys.stderr)
            return
        proxy = get_proxy("ALRobotPosture")
        if proxy:
            try:
                proxy.goToPosture(name, float(self._limits["posture_speed"]))
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

    def _set_stiffness(self, groups, value):
        """Enable or disable stiffness for the given group keys (head/larm/rarm).

        Leg group stiffness is permanently blocked — lower-body motors must
        never be commanded to avoid unintended movement.
        """
        value = _clamp(value, 0.0, float(self._limits["stiffness_max"]))
        with self._stiffness_lock:
            for grp in groups:
                grp = grp.lower()
                if grp == "legs":
                    print("[bridge] stiffness: groupe 'legs' bloque — moteurs inferieurs desactives",
                          file=sys.stderr)
                    continue
                naoqi_group = _NAOQI_GROUP.get(grp)
                # Unknown group key — skip rather than sending a bad NAOqi group name.
                if not naoqi_group:
                    print("[bridge] stiffness: groupe inconnu '{}'".format(grp), file=sys.stderr)
                    continue
                # An explicit order (régie, renderer) replaces the limp at rest.
                self._limp.discard(grp)
                # Track which groups have stiffness ON so _motion_joint can gate on it.
                if value > 0.0:
                    self._stiffness_enabled.add(grp)
                else:
                    # value == 0 means "release stiffness" — remove from enabled set.
                    self._stiffness_enabled.discard(grp)
                # Mock mode: log instead of calling NAOqi.
                if not HAS_NAOQI:
                    print("[MOCK] stiffness: {} ({}) -> {}".format(grp, naoqi_group, value),
                          file=sys.stderr)
                    continue
                proxy = get_proxy("ALMotion")
                if proxy:
                    try:
                        proxy.setStiffnesses(naoqi_group, value)
                    except Exception as e:
                        print("[bridge] stiffness error {}: {}".format(grp, e), file=sys.stderr)

    def _motion_joint(self, joint, angle, speed):
        """Move a single joint to the given angle (radians) at the given speed fraction."""
        self._motion_joints([joint], [angle], speed)

    def _motion_joints(self, joints, angles, speed, rest_in=None):
        """Move joints through the motion guard (clamped, rate-limited, non-blocking).

        Leg joints are filtered out unconditionally.  The whole command is
        rejected if any group it needs has stiffness off: a partial move
        with some limp joints would produce unexpected poses.
        """
        filtered = [(j, a) for j, a in zip(joints, angles)
                    if _JOINT_TO_GROUP.get(j, "head") != "legs"]
        if not filtered:
            if joints:
                print("[bridge] motion bloque: joints de jambe {}".format(list(joints)),
                      file=sys.stderr)
            return
        # The chain being calibrated is moved by hand or by the calibration only.
        calib = self._calib_chain()
        filtered = [(j, a) for j, a in filtered if _JOINT_TO_GROUP.get(j) != calib]
        if not filtered:
            return
        joints = [j for j, _ in filtered]
        angles = [a for _, a in filtered]
        grps_needed = set(_JOINT_TO_GROUP.get(j, "head") for j in joints)
        with self._stiffness_lock:
            limp = grps_needed & self._limp
        for grp in sorted(limp):
            self._wake_chain(grp)
        with self._stiffness_lock:
            disabled = grps_needed - self._stiffness_enabled
        if disabled:
            if not HAS_NAOQI:
                print("[MOCK] motion skipped (stiffness off): {}".format(sorted(disabled)),
                      file=sys.stderr)
            return
        # Stiffness is already on (set by _set_stiffness): no setStiffnesses
        # call per move, which only added load on NAOqi.
        self._guard.submit(joints, angles, speed)
        for grp in grps_needed:
            seq = self._motion_seq.get(grp, 0) + 1
            self._motion_seq[grp] = seq
            if rest_in is not None and self._limits["rest_limp"]:
                self._spawn(self._release_at_rest, grp, seq, rest_in)

    # ------------------------------------------------------------------
    # Calibration of the motor profile (driven from the régie)
    # ------------------------------------------------------------------
    #
    # /nao/calibration carries one step at a time, {"op": ...}:
    #   status                 publish the state (current profile included)
    #   start {chain}          calibrate head, larm or rarm; eyes turn orange
    #   release / hold         stiffness off (to place the chain by hand) / on
    #   capture {what}         what = rest | start: average of the sensors
    #   range_start/range_stop stiffness off, record min and max of each joint
    #   speed {value}          speed of the chain
    #   replay                 rest -> start -> min and max of each joint -> start -> rest
    #   save                   after a replay: write the profile, announce it
    #   cancel                 leave without writing; the chain goes back to rest
    # The state is published on /nao/calibration_state after each step, and
    # every 0.5 s with the live angles while a calibration is open.

    def _calib_chain(self):
        calib = self._calib
        return calib["chain"] if calib else None

    def _calibration_state(self):
        calib = self._calib
        state = {"active": calib is not None, "profile": self._profile,
                 "chains": dict((n, list(j)) for n, j in _CHAINS.items())}
        if calib:
            state.update(dict((k, v) for k, v in calib.items() if not k.startswith("_")))
        return state

    def _calib_publish(self, message=None, error=None):
        calib = self._calib
        if calib is not None:
            if message is not None:
                calib["message"] = message
            calib["error"] = error or ""
        state = self._calibration_state()
        state["error"] = error or ""
        self._publish("/nao/calibration_state", state)

    def _calib_angles(self, joints):
        motion = self._proxy("ALMotion")
        if not motion:   # MOCK: middle of the hardware range
            return dict((j, round(sum(_JOINT_HW_LIMITS[j]) / 2.0, 4)) for j in joints)
        angles = motion.getAngles(list(joints), True)
        return dict((j, round(float(a), 4)) for j, a in zip(joints, angles))

    def _calib_stiffness(self, chain, on):
        motion = self._proxy("ALMotion")
        if motion:
            if on:
                self._hold_here(motion, _CHAINS[chain])
            else:
                motion.setStiffnesses(_NAOQI_GROUP[chain], 0.0)
        with self._stiffness_lock:
            self._limp.discard(chain)
            (self._stiffness_enabled.add if on else self._stiffness_enabled.discard)(chain)

    def _calibration(self, payload):
        op = payload.get("op", "")
        with self._calib_lock:
            try:
                error = self._calibration_step(op, payload)
            except Exception as e:
                error = "{} : {}".format(op, e)
        if error:
            print("[calibration] {}".format(error), file=sys.stderr)
        self._calib_publish(error=error)

    def _calibration_step(self, op, payload):
        """Run one step; returns an error message or ''."""
        calib = self._calib
        if op == "status":
            return ""
        if op == "start":
            chain = payload.get("chain")
            if chain not in _CHAINS:
                return "chaine inconnue : {}".format(chain)
            if calib:
                return "" if calib["chain"] == chain else \
                    "calibrage de {} en cours".format(calib["chain"])
            old = (self._profile or {}).get("chains", {}).get(chain, {})
            self._calib = {"chain": chain, "step": "hold", "message": "", "error": "",
                           "rest": dict(old.get("rest", {})), "start": dict(old.get("start", {})),
                           "range": dict(old.get("range", {})),
                           "speed": old.get("speed", 0.1), "replayed": False,
                           "angles": {}, "_recording": False}
            self._calib_stiffness(chain, True)
            self._leds("FaceLeds", 1.0, 0.4, 0.0)
            self._spawn(self._calib_live_loop)
            self._calib["message"] = "Calibrage de {} ouvert".format(chain)
            return ""
        if calib is None:
            return "aucun calibrage ouvert"
        chain = calib["chain"]
        if calib["step"] in ("capturing", "replaying") and op != "cancel":
            return "etape en cours : {}".format(calib["step"])
        if op == "release":
            self._calib_stiffness(chain, False)
            calib.update(step="released", message="Raideur coupee : place la chaine a la main")
        elif op == "hold":
            self._calib_stiffness(chain, True)
            calib.update(step="hold", message="Raideur remise")
        elif op == "capture":
            what = payload.get("what")
            if what not in ("rest", "start"):
                return "capture : rest ou start"
            calib["step"] = "capturing"
            self._spawn(self._calib_capture, what)
        elif op == "range_start":
            self._calib_stiffness(chain, False)
            calib.update(step="recording", _recording=True, range={},
                         message="Enregistrement : parcours la zone permise")
        elif op == "range_stop":
            if calib["step"] != "recording":
                return "pas d'enregistrement en cours"
            calib["_recording"] = False
            self._calib_finish_range()
            self._calib_stiffness(chain, True)
            calib.update(step="hold", replayed=False, message="Plage enregistree")
        elif op == "speed":
            try:
                calib["speed"] = _clamp_speed(float(payload.get("value")), self._limits)
            except (TypeError, ValueError):
                return "vitesse invalide"
            calib.update(replayed=False, message="Vitesse : {}".format(calib["speed"]))
        elif op == "replay":
            clean, error = _validate_chain(chain, calib, self._limits)
            if clean is None:
                return error
            calib["step"] = "replaying"
            self._spawn(self._calib_replay, clean)
        elif op == "save":
            return self._calib_save()
        elif op == "cancel":
            calib["_recording"] = False
            self._calib = None
            self._calib_end(chain)
            print("[calibration] {} annule".format(chain), file=sys.stderr)
        else:
            return "etape inconnue : {}".format(op)
        return ""

    def _calib_live_loop(self):
        """While a calibration is open: live angles, min/max when recording."""
        while not self._stop.is_set():
            calib = self._calib
            if calib is None:
                return
            try:
                angles = self._calib_angles(_CHAINS[calib["chain"]])
            except Exception as e:
                print("[calibration] angles non lus : {}".format(e), file=sys.stderr)
                angles = {}
            calib["angles"] = angles
            if calib["_recording"]:
                self._calib_record(calib, angles)
            if self._connected.is_set():
                self._calib_publish()
            self._stop.wait(0.05 if calib["_recording"] else 0.5)

    def _calib_record(self, calib, angles):
        rng = calib["range"]
        for j, a in angles.items():
            lo, hi = rng.get(j, [a, a])
            rng[j] = [min(lo, a), max(hi, a)]

    def _calib_finish_range(self):
        """Recorded min/max -> range: margin off, inside the hardware range,
        and holding the start pose."""
        calib = self._calib
        m = float(self._limits["range_margin"])
        for j in _CHAINS[calib["chain"]]:
            if j not in calib["range"]:
                continue
            lo, hi = calib["range"][j]
            if hi - lo > 2 * m:
                lo, hi = lo + m, hi - m
            hw_lo, hw_hi = _joint_range(j, self._limits)
            lo, hi = _clamp(lo, hw_lo, hw_hi), _clamp(hi, hw_lo, hw_hi)
            if j in calib["start"]:
                lo, hi = min(lo, calib["start"][j]), max(hi, calib["start"][j])
            calib["range"][j] = [round(lo, 4), round(hi, 4)]

    def _calib_capture(self, what):
        """Average of 5 sensor reads, 0.1 s apart (off the WebSocket thread)."""
        calib = self._calib
        error = ""
        try:
            joints = _CHAINS[calib["chain"]]
            samples = []
            for _ in range(5):
                samples.append(self._calib_angles(joints))
                self._sleep(0.1)
            calib[what] = dict((j, round(sum(s[j] for s in samples) / len(samples), 4))
                               for j in joints)
            calib.update(replayed=False, message="Position {} capturee".format(
                "de repos" if what == "rest" else "de depart"))
        except Exception as e:
            error = "capture : {}".format(e)
        calib["step"] = "released" if calib["chain"] not in self._stiffness_enabled else "hold"
        self._calib_publish(error=error)

    def _calib_replay(self, cfg):
        """Play the chain through its whole range at its speed (blocking calls)."""
        calib = self._calib
        joints = list(_CHAINS[calib["chain"]])
        poses = [cfg["rest"], cfg["start"]]
        for j in joints:
            for end in cfg["range"][j]:
                pose = dict(cfg["start"])
                pose[j] = end
                poses.append(pose)
        poses += [cfg["start"], cfg["rest"]]
        error = ""
        try:
            self._calib_stiffness(calib["chain"], True)
            motion = self._proxy("ALMotion")
            for i, pose in enumerate(poses):
                if self._calib is not calib:
                    return   # cancelled meanwhile
                calib["message"] = "Rejeu {}/{}".format(i + 1, len(poses))
                self._calib_publish()
                if motion:
                    motion.angleInterpolationWithSpeed(
                        joints, [pose[j] for j in joints], cfg["speed"])
                else:
                    print("[MOCK] rejeu {} -> {}".format(joints, [pose[j] for j in joints]),
                          file=sys.stderr)
            calib.update(replayed=True, message="Rejeu termine : enregistrer ou annuler")
        except Exception as e:
            error = "rejeu : {}".format(e)
        calib["step"] = "hold"
        self._calib_publish(error=error)

    def _calib_save(self):
        calib = self._calib
        if not calib["replayed"]:
            return "rejouer avant d'enregistrer"
        clean, error = _validate_chain(calib["chain"], calib, self._limits)
        if clean is None:
            return error
        profile = dict(self._profile or {})
        chains = dict(profile.get("chains", {}))
        chains[calib["chain"]] = clean
        profile.update(chains=chains, posture="Sit",
                       version=int(profile.get("version", 0)) + 1,
                       calibrated_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        try:
            if self._profile is not None:
                _write_json("{}.{}.bak".format(self._profile_path, self._profile.get("version", 0)),
                            self._profile)
            _write_json(self._profile_path, profile)
        except (IOError, OSError) as e:
            return "profil non ecrit : {}".format(e)
        self._set_profile(profile)
        self._calib = None
        self._calib_end(calib["chain"])
        print("[calibration] {} enregistre (version {})".format(
            calib["chain"], profile["version"]), file=sys.stderr)
        return ""

    def _calib_end(self, chain):
        """Back to rest (when the chain has a profile), eyes back, profile
        announced: the NAO driver resets its sequences."""
        self._calib_stiffness(chain, True)
        self._spawn(self._calib_back_to_rest, chain)

    def _calib_back_to_rest(self, chain):
        self._go_to_rest([chain])
        self._leds("FaceLeds", 1.0, 1.0, 1.0)
        self._publish("/nao/capabilities", self._capabilities())

    # ------------------------------------------------------------------
    # Wake word detection → audio recording → publish /nao/audio
    # ------------------------------------------------------------------

    def _wakeword_loop(self):
        memory = get_proxy("ALMemory")
        asr    = get_proxy("ALSpeechRecognition")
        if not memory or not asr:
            print("[bridge] ASR non disponible — wake word désactivé.", file=sys.stderr)
            return
        try:
            asr.setVocabulary(["ok nao", "okay nao"], True)
            asr.subscribe("nao_ws_bridge_ww")
            self._asr = asr
            print("[bridge] Wake word 'ok nao' actif.", file=sys.stderr)
        except Exception as e:
            print("[bridge] ASR init error: {}".format(e), file=sys.stderr)
            return

        last_word = ""
        _exiting_busy = False
        while not self._stop.is_set():
            try:
                # During TTS/recording, suppress additional wake-word detections
                # so the robot does not start a new capture while still speaking.
                if time.time() < self._busy_until:
                    _exiting_busy = True
                    time.sleep(0.2)
                    continue

                if _exiting_busy:
                    # First iteration after the busy window: flush the ALMemory
                    # cache to avoid a false trigger on the residual "ok nao" value.
                    _exiting_busy = False
                    last_word = ""
                    try:
                        memory.insertData("WordRecognized", ["", 0.0])
                    except Exception:
                        pass
                    time.sleep(0.2)
                    continue

                value = memory.getData("WordRecognized")
                # Accept a wake-word match only when confidence > 0.3 and
                # the ALMemory result contains at least [phrase, confidence].
                if value and len(value) >= 2 and float(value[1]) > 0.3:
                    phrase = value[0].lower().strip()
                    # Deduplicate: ignore the same phrase seen in consecutive polls
                    # (ALMemory keeps the last result until explicitly cleared).
                    if phrase != last_word and ("ok nao" in phrase or "okay nao" in phrase):
                        last_word = phrase
                        self._busy_until = time.time() + 20.0  # lock out wake word during listen + TTS
                        # Immediately clear ALMemory to prevent re-detection if body_gateway
                        # is slow and the 20 s window expires before TTS completes.
                        try:
                            memory.insertData("WordRecognized", ["", 0.0])
                        except Exception:
                            pass
                        threading.Thread(target=self._record_and_publish).start()
            except Exception as e:
                print("[bridge] wakeword_loop error: {}".format(e), file=sys.stderr)
            time.sleep(0.2)

    def _record_and_publish(self):
        global _CAPTURE_REF

        if not _ensure_broker():
            print("[bridge] Broker NAOqi non disponible — enregistrement annulé.", file=sys.stderr)
            return

        module_name = "NaoCap{:05d}".format(int(time.time() * 10) % 100000)
        capture = _AudioCapture(module_name)
        _CAPTURE_REF = capture
        self._audio_capture = capture

        audio_dev = get_proxy("ALAudioDevice")
        if not audio_dev:
            print("[bridge] ALAudioDevice non disponible.", file=sys.stderr)
            _CAPTURE_REF = None
            self._audio_capture = None
            return

        try:
            print("[bridge] Ecoute VAD (max {}s) ...".format(_AudioCapture._MAX_SEC),
                  file=sys.stderr)
            audio_dev.setClientPreferences(module_name, _AudioCapture._RATE, 0, 0)
            audio_dev.subscribe(module_name)

            capture._done.wait(_AudioCapture._MAX_SEC + 1.0)

            audio_dev.unsubscribe(module_name)
            self._audio_capture = None
            _CAPTURE_REF = None

            # VAD timed out without detecting speech above threshold — nothing to send.
            if not capture._speaking:
                print("[bridge] Aucune parole détectée.", file=sys.stderr)
                return

            audio_bytes = capture.to_wav()
            # Guard: empty WAV would cause Whisper to error; skip silently.
            if not audio_bytes:
                return

            encoded = base64.b64encode(audio_bytes)
            if isinstance(encoded, bytes):
                encoded = encoded.decode("ascii")

            # Only publish if bridge is still running — avoids sending on shutdown.
            if not self._stop.is_set():
                duration = len(capture._pcm) / float(_AudioCapture._RATE)
                print("[bridge] Audio VAD prêt ({} octets, {:.1f}s) — envoi ...".format(
                    len(audio_bytes), duration), file=sys.stderr)
                self._send({
                    "op":    "publish",
                    "topic": "/nao/audio",
                    "msg":   {"data": encoded},
                })
        except Exception as e:
            print("[bridge] _record_and_publish error: {}".format(e), file=sys.stderr)
            try:
                audio_dev.unsubscribe(module_name)
            except Exception:
                pass
            self._audio_capture = None
            _CAPTURE_REF = None

    # ------------------------------------------------------------------
    # Status publisher — battery level heartbeat every STATUS_INTERVAL s
    # ------------------------------------------------------------------

    def _read_motor_temps(self):
        """Return max temperature (°C, int) per group via ALMemory, or {} in mock mode."""
        if not HAS_NAOQI:
            return {}
        memory = get_proxy("ALMemory")
        if not memory:
            return {}
        temps = {}
        for grp, joints in _TEMP_JOINTS.items():
            readings = []
            for j in joints:
                try:
                    val = memory.getData(_TEMP_KEY.format(j))
                    # ALMemory returns None for joints not available on this firmware.
                    if val is not None:
                        readings.append(float(val))
                except Exception:
                    pass
            # Report the hottest joint per group — the worst case is the safety-relevant value.
            if readings:
                temps[grp] = int(max(readings))
        return temps

    def _read_environment(self):
        """Return scalar environment sensor values via ALMemory, or {} in mock mode."""
        if not HAS_NAOQI:
            return {}
        memory = get_proxy("ALMemory")
        if not memory:
            return {}
        env = {}
        for name, key in _ENV_SENSOR_KEYS.items():
            try:
                val = memory.getData(key)
                # Sensor not available on this firmware version — skip gracefully.
                if val is not None:
                    env[name] = round(float(val), 1)
            except Exception:
                pass
        return env

    def _status_loop(self):
        """Publish battery + sensor readings every STATUS_INTERVAL seconds.

        Runs in a single thread started by the first _on_open and kept across
        reconnects.  Skips sensor reads while disconnected; exits when the
        bridge is stopped.
        """
        while not self._stop.is_set():
            # No connection: nothing to publish, so do not poll NAOqi either.
            if not self._connected.is_set():
                self._stop.wait(1.0)
                continue
            level = -1
            # Battery level is only available with NAOqi — keep -1 in mock mode.
            if HAS_NAOQI:
                proxy = get_proxy("ALBattery")
                if proxy:
                    try:
                        level = proxy.getBatteryCharge()
                    except Exception:
                        pass
            motor_temp  = self._read_motor_temps()
            environment = self._read_environment()
            # The motor profile is repeated so that a NAO driver started after
            # the robot (it missed /nao/capabilities) still gets it.
            payload = json.dumps({"battery": level, "motor_temp": motor_temp,
                                  "environment": environment,
                                  "motor_profile": self._profile})
            try:
                self._send({
                    "op": "publish",
                    "topic": "/nao/status",
                    "msg": {"data": payload},
                })
            except Exception as e:
                # Connection dropped mid-send: the next session resumes publishing.
                print("[bridge] status send error: {}".format(e), file=sys.stderr)
            self._stop.wait(STATUS_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NAO WebSocket bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Modules disponibles : {}\n\n"
            "Exemples :\n"
            "  NAO emotif, sans audio (defaut) :\n"
            "    python nao_ws_bridge.py --vps ws://DRONE_IP:9090/\n\n"
            "  LED + parole seulement, sans capture audio :\n"
            "    python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --modules led,speech\n\n"
            "  Gestes + LED uniquement :\n"
            "    python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --modules gesture,led"
        ).format(",".join(sorted(ALL_MODULES)))
    )
    parser.add_argument("--vps", default=None,
                        help="Rosbridge URL, usually the static drone relay (e.g. ws://DRONE_IP:9090/). Required unless using --provision only.")
    parser.add_argument("--token", default=os.environ.get("ROSBRIDGE_TOKEN"),
                        help="Token expected by rosbridge-proxy (default: $ROSBRIDGE_TOKEN).")
    parser.add_argument(
        "--provision",
        nargs="?",
        const=_PROVISION_DEFAULT,
        default=None,
        metavar="CONFIG_FILE",
        help=(
            "Apply post-flash configuration (autonomous life, language, volume) "
            "before starting the bridge; its \"bridge\" section sets the default "
            "--modules and audio capture. "
            "Without argument, uses nao_provision.json next to this script. "
            "Without --vps, exits after provisioning."
        ),
    )
    parser.add_argument("--motor-profile", default=_MOTOR_PROFILE_DEFAULT, metavar="FILE",
                        help="Motor profile written by the calibration (default: "
                             "nao_motor_profile.json next to this script). Without it, "
                             "no joint moves until a calibration from the regie.")
    parser.add_argument("--verbose", action="store_true",
                        help="Also log every /nao/motion and /nao/status message (costs CPU).")
    parser.add_argument("--nao-ip", default="127.0.0.1",
                        help="NAO IP address (default: 127.0.0.1)")
    parser.add_argument("--nao-port", type=int, default=9559,
                        help="NAOqi port (default: 9559)")
    parser.add_argument(
        "--modules",
        default=None,
        metavar="MOD1,MOD2,...",
        help=(
            "Comma-separated list of modules to enable. "
            "Available: {}. "
            "Default: {}."
        ).format(", ".join(sorted(ALL_MODULES)), ", ".join(sorted(DEFAULT_MODULES)))
    )
    parser.add_argument(
        "--audio-capture",
        dest="audio_capture",
        action="store_true",
        default=None,
        help="Enable wake word detection and VAD capture (disabled by default)."
    )
    parser.add_argument(
        "--no-audio-capture",
        dest="audio_capture",
        action="store_false",
        help="Disable wake word detection and VAD capture, even if the provision file enables it."
    )
    parser.add_argument(
        "--tts-method",
        dest="tts_method",
        choices=["auto", "mp3", "text"],
        default="auto",
        help=(
            "TTS method announced to the VPS. "
            "'mp3': VPS generates audio (edge-tts) and sends it on /nao/audio_out. "
            "'text': VPS sends raw text on /nao/speech (native NAOqi TTS). "
            "'auto' (default): mp3 if NAOqi available, otherwise text."
        )
    )
    args = parser.parse_args()

    # Both --provision-only and --vps-only are valid; only the combination of
    # neither is invalid.
    if not args.provision and not args.vps:
        parser.error("--vps is required unless using --provision only.")

    # The token is written verbatim into an HTTP header line.
    if args.token and ("\r" in args.token or "\n" in args.token):
        parser.error("--token must not contain line breaks.")
    if args.vps and not args.token:
        print("[bridge] WARNING: no --token / ROSBRIDGE_TOKEN, rosbridge-proxy will reject the connection",
              file=sys.stderr)

    NAO_IP = args.nao_ip
    NAO_PORT = args.nao_port
    _VERBOSE = args.verbose

    # "bridge" section of the provisioning file, overridden by the CLI flags.
    bridge_cfg = _bridge_config(args.provision) if args.provision else {}
    modules_arg = args.modules
    if modules_arg is None and "modules" in bridge_cfg:
        modules_arg = ",".join(str(m) for m in bridge_cfg["modules"])
    audio_capture = args.audio_capture
    if audio_capture is None:
        audio_capture = bool(bridge_cfg.get("audio_capture", False))

    # Validate and parse the requested module list.
    if modules_arg is not None:
        requested = set(m.strip().lower() for m in modules_arg.split(",") if m.strip())
        unknown = requested - ALL_MODULES
        # Reject unknown module names early rather than silently ignoring them.
        if unknown:
            parser.error(
                "Unknown module(s): {}. Valid modules: {}".format(
                    ", ".join(sorted(unknown)),
                    ", ".join(sorted(ALL_MODULES)),
                )
            )
        active_modules = requested
    else:
        active_modules = None  # None means DEFAULT_MODULES

    # Apply post-flash provisioning if requested; the TTS and ASR languages
    # only when the modules that use them are on.
    if args.provision:
        enabled = DEFAULT_MODULES if active_modules is None else active_modules
        _provision(args.provision, tts="speech" in enabled, asr=audio_capture)
        # When --vps was not given, the user only wanted to provision — exit now.
        if not args.vps:
            sys.exit(0)

    bridge = NaoBridge(args.vps, modules=active_modules, audio_capture=audio_capture,
                       tts_method=args.tts_method, token=args.token,
                       motion_limits=bridge_cfg.get("motion_limits"),
                       motor_profile=args.motor_profile)
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\n[bridge] Ctrl+C received — shutting down ...", file=sys.stderr)
        bridge.stop()
        print("[bridge] Stopped.", file=sys.stderr)
