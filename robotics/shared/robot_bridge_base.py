#!/usr/bin/env python3
"""
robot_bridge_base.py — Base class for plug-and-play robot bridges (Python 3).

Any bridge inherits RobotBridgeBase and implements two methods:

    _connect_impl()      → connect to hardware, raise on failure
    _disconnect_impl()   → clean up SDK resources, never raise

The base class handles the reconnection loop as a daemon thread.
It retries every RECONNECT_INTERVAL seconds until connected, and
reconnects automatically when the physical link is lost.

On each failed hardware command, call self.on_command_error(exc)
to signal the disconnection and trigger immediate reconnection.

Flask / HTTP usage example
──────────────────────────

    class MyBridge(RobotBridgeBase):

        def _connect_impl(self):
            self._sdk = MySdk.connect(ip=self._ip, port=self._port)

        def _disconnect_impl(self):
            try:
                self._sdk.close()
            except Exception:
                pass
            self._sdk = None

    bridge = MyBridge(reconnect_interval=15)

    @app.route("/health")
    def health():
        return jsonify(bridge.health_dict())

    @app.route("/do", methods=["POST"])
    def do():
        if not bridge.connected:
            return jsonify({"error": "not connected"}), 503
        try:
            bridge._sdk.do_something()
        except Exception as e:
            bridge.on_command_error(e)
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

Python 2.7 reference (naoqi_bridge)
────────────────────────────────────
naoqi_bridge/app.py implements the same pattern without this base class
because the qi SDK requires Python 2.7 which cannot import this module.
The logic is identical: _connect_once(), _disconnect(), _reconnect_loop().
"""

import threading
import time


class RobotBridgeBase:
    """
    Plug-and-play reconnection loop for robot hardware bridges.

    Subclass this and implement _connect_impl() + _disconnect_impl().
    The daemon reconnection thread starts automatically on __init__.
    """

    def __init__(self, reconnect_interval: int = 15):
        self._reconnect_interval = reconnect_interval
        self._conn_lock = threading.Lock()
        self._connected = False
        self._running = True

        t = threading.Thread(target=self._reconnect_loop, daemon=True)
        t.start()

    # ── Implement in subclass ─────────────────────────────────────────────────

    def _connect_impl(self):
        """Connect to hardware. Raise any exception on failure."""
        raise NotImplementedError

    def _disconnect_impl(self):
        """Release hardware resources. Must not raise."""
        pass

    # ── Optional hooks ────────────────────────────────────────────────────────

    def _on_connected(self):
        """Called once after each successful (re)connection."""

    def _on_disconnected(self):
        """Called once right after connection is lost."""

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        with self._conn_lock:
            return self._connected

    def on_command_error(self, exc: Exception):
        """
        Call inside a route's except block when a hardware call fails.
        Marks the bridge as disconnected so the reconnect loop retries.
        """
        self._mark_disconnected(reason=str(exc))

    def health_dict(self) -> dict:
        """Suitable for a JSON /health response."""
        return {"ok": True, "connected": self.connected}

    def stop(self):
        """Stop the reconnection loop (call on SIGTERM / app shutdown)."""
        self._running = False

    # ── Internal ─────────────────────────────────────────────────────────────

    def _mark_disconnected(self, reason: str = ""):
        """Mark the bridge as disconnected and trigger cleanup hooks.

        Thread-safe: uses _conn_lock to avoid a race between the reconnect
        loop setting _connected=True and a route's except block setting it False.
        """
        with self._conn_lock:
            was_connected = self._connected
            self._connected = False
        # Only log and call hook on an actual transition (connected → disconnected).
        # Avoids duplicate log lines when _mark_disconnected is called repeatedly.
        if was_connected:
            self._log(
                "Connection lost{} — will retry in {}s.".format(
                    f" ({reason})" if reason else "",
                    self._reconnect_interval,
                )
            )
            try:
                self._on_disconnected()
            except Exception:
                pass
        try:
            self._disconnect_impl()
        except Exception:
            pass

    def _connect_once(self) -> bool:
        self._log("Connecting...")
        try:
            self._connect_impl()
            with self._conn_lock:
                self._connected = True
            self._log("Connected.")
            try:
                self._on_connected()
            except Exception:
                pass
            return True
        except Exception as e:
            self._log(f"Connection failed: {e}")
            return False

    def _reconnect_loop(self):
        """Background daemon thread: attempt reconnection whenever disconnected.

        Sleeps RECONNECT_INTERVAL seconds between checks whether connected or not —
        this also acts as a debounce if a successful connection drops immediately.
        """
        while self._running:
            # Only attempt a connection when currently disconnected —
            # avoids calling _connect_impl while an existing session is live.
            if not self.connected:
                self._connect_once()
            time.sleep(self._reconnect_interval)

    def _log(self, msg: str):
        print(f"[{self.__class__.__name__}] {msg}", flush=True)
