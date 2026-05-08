#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
nao_ws_bridge.py — Script embedded on the NAO robot (Python 2.7).

Establishes an OUTBOUND WebSocket connection to the rosbridge server on the VPS,
subscribes to command topics and publishes robot status and captured audio.

No external Python dependencies — uses a built-in pure Python 2.7 WebSocket client.
The naoqi module is available natively on the robot (OpenNAO firmware).

Usage:
    python /home/nao/nao_ws_bridge.py --vps ws://VPS_IP:9090 [options]

Module filtering:
    --modules led,speech,gesture   Enable only the listed modules.
                                   Available: led, speech, audio_out, gesture,
                                   motion, move, stop (default: all active).
    --no-audio-capture             Disable wake word detection and VAD capture.

TTS method:
    --tts-method auto|mp3|text
        mp3  : VPS generates audio via edge-tts and sends it on /nao/audio_out.
        text : VPS sends raw text on /nao/speech (native NAOqi TTS).
        auto : mp3 if NAOqi is available, otherwise text (default).

Examples:
    # LEDs + speech only, no audio capture:
    python nao_ws_bridge.py --vps ws://IP:9090 --modules led,speech --no-audio-capture

    # All modules active (default behaviour):
    python nao_ws_bridge.py --vps ws://IP:9090

    # Apply post-flash config then start bridge:
    python nao_ws_bridge.py --vps ws://IP:9090 --provision
"""

from __future__ import print_function

import argparse
import base64
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

_PROVISION_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nao_provision.json"
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
    ("/nao/shutdown", "std_msgs/Empty"),
]
ALL_MODULES = frozenset(_MODULE_TOPICS.keys())

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

_BROKER      = None  # ALBroker instance — must stay alive for NAOqi callbacks
_CAPTURE_REF = None  # strong ref to active _AudioCapture, prevents GC during recording


# ======================================================================
# Pure Python 2.7 WebSocket client (RFC 6455, ws:// only)
# Replaces websocket-client to avoid any dependency on the ssl module,
# which may not be available on all OpenNAO firmware builds.
# ======================================================================

class _WebSocketApp(object):

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url, on_open=None, on_message=None,
                 on_error=None, on_close=None):
        self.url = url
        self.on_open = on_open
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self._sock = None
        self._closed = False

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
        if not self.url.startswith("ws://"):
            raise ValueError("Only ws:// is supported (got: {})".format(self.url))
        tail = self.url[5:]

        if "/" in tail:
            host_port, path = tail.split("/", 1)
            path = "/" + path
        else:
            host_port, path = tail, "/"

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
        req = (
            "GET {path} HTTP/1.1\r\n"
            "Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).format(path=path, host=host, port=port, key=raw_key)
        self._sock.sendall(req.encode("ascii"))

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise IOError("Connection closed during WebSocket handshake")
            resp += chunk

        status_line = resp.split(b"\r\n")[0]
        if b"101" not in status_line:
            raise IOError("WebSocket handshake failed: {}".format(
                status_line.decode("ascii", "replace")))

        # Verify the server's Sec-WebSocket-Accept header (RFC 6455 §4.1)
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
        mask_key = bytearray(os.urandom(4))
        payload = bytearray(data)
        length = len(payload)

        header = bytearray()
        header.append(0x80 | opcode)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask_key)

        masked = bytearray(length)
        for i, b in enumerate(payload):
            masked[i] = b ^ mask_key[i % 4]

        self._sock.sendall(bytes(header) + bytes(masked))

    def _recv_exact(self, n):
        """Read exactly n bytes from the socket, blocking until complete."""
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise IOError("Connection closed")
            buf += chunk
        return buf

    def _recv_loop(self):
        while not self._closed:
            b0, b1 = bytearray(self._recv_exact(2))
            opcode = b0 & 0x0F
            is_masked = (b1 & 0x80) != 0
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]

            mask_key = bytearray(self._recv_exact(4)) if is_masked else None
            payload = bytearray(self._recv_exact(length))

            if mask_key:
                for i in range(length):
                    payload[i] ^= mask_key[i % 4]

            if opcode == 0x1:   # text frame
                if self.on_message:
                    self.on_message(self, payload.decode("utf-8", "replace"))
            elif opcode == 0x2: # binary frame
                if self.on_message:
                    self.on_message(self, bytes(payload))
            elif opcode == 0x8: # close frame
                self._closed = True
                break
            elif opcode == 0x9: # ping → reply with pong (0xA)
                self._send_frame(bytes(payload), opcode=0xA)


# ======================================================================
# Bridge NAO ↔ rosbridge
# ======================================================================

def _provision(config_path):
    """Apply post-flash configuration from config_path (JSON file)."""
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
    if lang_cfg:
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

        if "tts" in lang_cfg:
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

        if "asr" in lang_cfg:
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
    try:
        return ALProxy(service, NAO_IP, NAO_PORT)
    except Exception as e:
        print("[bridge] Proxy {} indisponible : {}".format(service, e), file=sys.stderr)
        return None


def _ws_log(direction, msg):
    """Print a human-readable WebSocket exchange summary to stderr."""
    op    = msg.get("op", "?")
    topic = msg.get("topic", "")
    inner = msg.get("msg", msg.get("data", {}))
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
    if _BROKER is not None:
        return True
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
        if self._done.is_set():
            return
        try:
            samples = [buffer[i] for i in range(0, nb_ch * nb_samp, nb_ch)]
        except TypeError:
            all_s = struct.unpack_from("<{}h".format(nb_ch * nb_samp), bytes(buffer))
            samples = list(all_s[::nb_ch])
        if not samples:
            return
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        if not self._speaking:
            if rms > self._SPEECH_RMS:
                self._speaking = True
                self._pcm.extend(samples)
        else:
            self._pcm.extend(samples)
            if rms < self._SILENCE_RMS:
                self._sil_n += 1
                if self._sil_n >= self._sil_need:
                    self._done.set()
            else:
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
        vps_url       : rosbridge WebSocket URL (e.g. ws://VPS_IP:9090).
        modules       : set of module names to enable (None = all modules).
        audio_capture : enable wake word detection and VAD capture.
        tts_method    : "mp3" | "text" | "auto".
                        "auto" → mp3 if NAOqi available, otherwise text.
    """

    def __init__(self, vps_url, modules=None, audio_capture=True, tts_method="auto"):
        self.vps_url = vps_url
        self._modules = ALL_MODULES if modules is None else frozenset(modules)
        self._audio_capture_enabled = audio_capture
        self._tts_method_override = tts_method
        self.ws = None
        self._stop = threading.Event()
        self._busy_until = 0.0          # timestamp until which wake word is ignored (during TTS)
        self._wakeword_started = False   # prevents relaunching the wake-word thread on reconnect
        self._asr = None                 # ASR proxy kept alive for unsubscribe on shutdown
        self._audio_capture = None       # active _AudioCapture instance during VAD recording
        # Groups whose stiffness has been explicitly enabled (others are skipped for motion)
        self._stiffness_enabled = set()
        self._stiffness_lock = threading.Lock()

    def _get_tts_method(self):
        if self._tts_method_override != "auto":
            return self._tts_method_override
        return "mp3" if HAS_NAOQI else "text"

    # ------------------------------------------------------------------
    # Connection / auto-reconnect loop
    # ------------------------------------------------------------------

    def run(self):
        while not self._stop.is_set():
            print("[bridge] Connexion a {} ...".format(self.vps_url), file=sys.stderr)
            try:
                self.ws = _WebSocketApp(
                    self.vps_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever()
            except Exception as e:
                print("[bridge] Erreur WebSocket : {}".format(e), file=sys.stderr)

            if not self._stop.is_set():
                print("[bridge] Reconnexion dans {}s ...".format(RECONNECT_DELAY), file=sys.stderr)
                time.sleep(RECONNECT_DELAY)

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
        if self.ws and not self._stop.is_set():
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
        advertised = ["/nao/status", "/nao/capabilities"]
        if self._audio_capture_enabled:
            advertised.append("/nao/audio")
        for topic in advertised:
            adv_msg = {"op": "advertise", "topic": topic, "type": "std_msgs/String"}
            _ws_log(u"→", adv_msg)
            ws.send(json.dumps(adv_msg))

        # Immediately announce TTS capability so the driver can negotiate the method
        tts_method = self._get_tts_method()
        cap_payload = json.dumps({"tts_method": tts_method})
        cap_msg = {"op": "publish", "topic": "/nao/capabilities",
                   "msg": {"data": cap_payload}}
        _ws_log(u"→", cap_msg)
        ws.send(json.dumps(cap_msg))
        print("[bridge] Capacites TTS annoncees : {}".format(tts_method), file=sys.stderr)

        print("[bridge] Pret.", file=sys.stderr)
        threading.Thread(target=self._status_loop).start()
        if self._audio_capture_enabled and not self._wakeword_started:
            self._wakeword_started = True
            threading.Thread(target=self._wakeword_loop).start()

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        _ws_log(u"←", msg)
        if msg.get("op") != "publish":
            return
        topic = msg.get("topic", "")
        data = msg.get("msg", {})
        self._dispatch(topic, data)

    def _on_error(self, ws, error):
        print("[bridge] Erreur : {}".format(error), file=sys.stderr)

    def _on_close(self, ws):
        print("[bridge] Connexion fermee.", file=sys.stderr)

    # ------------------------------------------------------------------
    # Dispatch incoming ROS topic messages to NAOqi commands
    # ------------------------------------------------------------------

    def _dispatch(self, topic, data):
        if topic == "/nao/audio_out" and "audio_out" in self._modules:
            b64 = data.get("data", "")
            if b64:
                threading.Thread(target=self._play_audio, args=(b64,)).start()
        elif topic == "/nao/speech" and "speech" in self._modules:
            text = data.get("data", "")
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
        elif topic == "/nao/motion" and "motion" in self._modules:
            try:
                payload = json.loads(data.get("data", "{}"))
            except ValueError:
                return
            if "joints" in payload:
                self._motion_joints(
                    [str(j) for j in payload["joints"]],
                    [float(a) for a in payload["angles"]],
                    float(payload.get("speed", 0.05)),
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
            if isinstance(groups, list):
                self._set_stiffness(groups, value)

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

    def _set_stiffness(self, groups, value):
        """Enable or disable stiffness for the given group keys (head/larm/rarm/legs)."""
        with self._stiffness_lock:
            for grp in groups:
                grp = grp.lower()
                naoqi_group = _NAOQI_GROUP.get(grp)
                if not naoqi_group:
                    print("[bridge] stiffness: groupe inconnu '{}'".format(grp), file=sys.stderr)
                    continue
                if value > 0.0:
                    self._stiffness_enabled.add(grp)
                else:
                    self._stiffness_enabled.discard(grp)
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
        """Move a single joint to the given angle (radians) at the given speed fraction.

        Skipped silently when the joint's group stiffness has not been enabled.
        """
        grp = _JOINT_TO_GROUP.get(joint, "head")
        with self._stiffness_lock:
            enabled = grp in self._stiffness_enabled
        if not enabled:
            if not HAS_NAOQI:
                print("[MOCK] motion_joint skipped (stiffness off): {}".format(joint),
                      file=sys.stderr)
            return
        if not HAS_NAOQI:
            print("[MOCK] motion_joint: {} -> {:.3f} rad speed={}".format(joint, angle, speed),
                  file=sys.stderr)
            return
        proxy = get_proxy("ALMotion")
        if proxy:
            try:
                proxy.setStiffnesses(_NAOQI_GROUP[grp], 1.0)
                proxy.angleInterpolationWithSpeed([joint], [angle], speed)
            except Exception as e:
                print("[bridge] motion_joint error: {}".format(e), file=sys.stderr)

    def _motion_joints(self, joints, angles, speed):
        """Move multiple joints simultaneously; all joints must share enabled groups."""
        if not joints:
            return
        # Determine which groups need to be active and check they're all enabled
        grps_needed = set(_JOINT_TO_GROUP.get(j, "head") for j in joints)
        with self._stiffness_lock:
            disabled = grps_needed - self._stiffness_enabled
        if disabled:
            if not HAS_NAOQI:
                print("[MOCK] motion_joints skipped (stiffness off): {}".format(
                    list(disabled)), file=sys.stderr)
            return
        if not HAS_NAOQI:
            for j, a in zip(joints, angles):
                print("[MOCK] motion_joint: {} -> {:.3f} rad speed={}".format(j, a, speed),
                      file=sys.stderr)
            return
        proxy = get_proxy("ALMotion")
        if proxy:
            try:
                for grp in grps_needed:
                    proxy.setStiffnesses(_NAOQI_GROUP[grp], 1.0)
                proxy.angleInterpolationWithSpeed(joints, angles, speed)
            except Exception as e:
                print("[bridge] motion_joints error: {}".format(e), file=sys.stderr)

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
                if value and len(value) >= 2 and float(value[1]) > 0.3:
                    phrase = value[0].lower().strip()
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

            if not capture._speaking:
                print("[bridge] Aucune parole détectée.", file=sys.stderr)
                return

            audio_bytes = capture.to_wav()
            if not audio_bytes:
                return

            encoded = base64.b64encode(audio_bytes)
            if isinstance(encoded, bytes):
                encoded = encoded.decode("ascii")

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
                    if val is not None:
                        readings.append(float(val))
                except Exception:
                    pass
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
                if val is not None:
                    env[name] = round(float(val), 1)
            except Exception:
                pass
        return env

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
            motor_temp  = self._read_motor_temps()
            environment = self._read_environment()
            payload = json.dumps({"battery": level, "motor_temp": motor_temp,
                                  "environment": environment})
            try:
                self._send({
                    "op": "publish",
                    "topic": "/nao/status",
                    "msg": {"data": payload},
                })
            except Exception:
                break
            self._stop.wait(STATUS_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NAO WebSocket bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Modules disponibles : {}\n\n"
            "Exemples :\n"
            "  Tout actif (defaut) :\n"
            "    python nao_ws_bridge.py --vps ws://IP:9090\n\n"
            "  LED + parole seulement, sans capture audio :\n"
            "    python nao_ws_bridge.py --vps ws://IP:9090 --modules led,speech --no-audio-capture\n\n"
            "  Gestes + LED uniquement :\n"
            "    python nao_ws_bridge.py --vps ws://IP:9090 --modules gesture,led"
        ).format(",".join(sorted(ALL_MODULES)))
    )
    parser.add_argument("--vps", default=None,
                        help="Rosbridge URL (e.g. ws://VPS_IP:9090). Required unless using --provision only.")
    parser.add_argument(
        "--provision",
        nargs="?",
        const=_PROVISION_DEFAULT,
        default=None,
        metavar="CONFIG_FILE",
        help=(
            "Apply post-flash configuration (autonomous life, language, volume) "
            "before starting the bridge. "
            "Without argument, uses nao_provision.json next to this script. "
            "Without --vps, exits after provisioning."
        ),
    )
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
            "Default: all modules active."
        ).format(", ".join(sorted(ALL_MODULES)))
    )
    parser.add_argument(
        "--no-audio-capture",
        dest="audio_capture",
        action="store_false",
        default=True,
        help="Disable wake word detection and VAD capture (enabled by default)."
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

    if not args.provision and not args.vps:
        parser.error("--vps is required unless using --provision only.")

    NAO_IP = args.nao_ip
    NAO_PORT = args.nao_port

    # Apply post-flash provisioning if requested
    if args.provision:
        _provision(args.provision)
        if not args.vps:
            sys.exit(0)

    # Validate and parse the requested module list
    if args.modules is not None:
        requested = set(m.strip().lower() for m in args.modules.split(",") if m.strip())
        unknown = requested - ALL_MODULES
        if unknown:
            parser.error(
                "Unknown module(s): {}. Valid modules: {}".format(
                    ", ".join(sorted(unknown)),
                    ", ".join(sorted(ALL_MODULES)),
                )
            )
        active_modules = requested
    else:
        active_modules = None  # all modules

    bridge = NaoBridge(args.vps, modules=active_modules, audio_capture=args.audio_capture,
                       tts_method=args.tts_method)
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("\n[bridge] Ctrl+C received — shutting down ...", file=sys.stderr)
        bridge.stop()
        print("[bridge] Stopped.", file=sys.stderr)
