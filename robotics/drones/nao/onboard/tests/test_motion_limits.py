"""Tests sans robot des limites de mouvement de nao_ws_bridge (mode MOCK).

    cd robotics/drones/nao/onboard && python3 -m unittest discover -s tests -v
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nao_ws_bridge as b  # noqa: E402


class FakeMotion(object):
    """ALMotion factice : enregistre les appels setAngles."""

    def __init__(self):
        self.calls = []
        self.event = threading.Event()

    def setAngles(self, joints, angles, speed):
        self.calls.append((list(joints), list(angles), speed))
        self.event.set()


def guard(**overrides):
    proxy = FakeMotion()
    g = b._MotionGuard(b._motion_limits(overrides), proxy_factory=lambda: proxy)
    return g, proxy


def wait_calls(proxy, n, timeout=2.0):
    end = time.time() + timeout
    while len(proxy.calls) < n and time.time() < end:
        time.sleep(0.01)
    return proxy.calls


class TestLimitsConfig(unittest.TestCase):

    def test_defaults(self):
        self.assertEqual(b._motion_limits(None), b._MOTION_LIMITS_DEFAULT)

    def test_override_and_bad_values_ignored(self):
        lim = b._motion_limits({"max_speed": 0.2, "min_interval": "vite",
                                "inconnu": 1, "fall_manager": 1})
        self.assertEqual(lim["max_speed"], 0.2)
        self.assertEqual(lim["min_interval"], b._MOTION_LIMITS_DEFAULT["min_interval"])
        self.assertTrue(lim["fall_manager"])
        self.assertNotIn("inconnu", lim)


class TestClamp(unittest.TestCase):

    def setUp(self):
        self.lim = b._motion_limits({})

    def test_speed(self):
        self.assertEqual(b._clamp_speed(1.0, self.lim), 0.3)
        self.assertEqual(b._clamp_speed(0.0, self.lim), 0.02)
        self.assertEqual(b._clamp_speed(0.1, self.lim), 0.1)

    def test_angle_margin(self):
        j, a = b._clamp_joints(["HeadPitch"], [3.0], self.lim)
        self.assertAlmostEqual(a[0], 0.5149 - 0.05)

    def test_override_only_narrows(self):
        lim = b._motion_limits({"joint_ranges": {"HeadPitch": [-5.0, 0.2]}})
        lo, hi = b._joint_range("HeadPitch", lim)
        self.assertAlmostEqual(lo, -0.6720 + 0.05)
        self.assertAlmostEqual(hi, 0.2)

    def test_hand_without_margin(self):
        self.assertEqual(b._joint_range("LHand", self.lim), (0.0, 1.0))

    def test_unknown_leg_and_nan_dropped(self):
        j, a = b._clamp_joints(["LKneePitch", "Foo", "HeadYaw", "HeadPitch"],
                               [0.1, 0.1, float("nan"), 0.1], self.lim)
        self.assertEqual(j, ["HeadPitch"])


class TestGuard(unittest.TestCase):

    def test_burst_is_coalesced_per_joint(self):
        g, proxy = guard(min_interval=0.2)
        # Premier envoi immédiat, puis une rafale fusionnée en un seul appel.
        g.submit(["HeadYaw"], [0.1], 0.1)
        wait_calls(proxy, 1)
        for i in range(20):
            g.submit(["HeadYaw"], [0.01 * i], 0.1)
        g.submit(["LShoulderPitch"], [1.0], 0.05)
        calls = wait_calls(proxy, 2)
        time.sleep(0.3)
        g.stop()
        self.assertEqual(len(proxy.calls), 2)
        joints, angles, speed = calls[1]
        self.assertEqual(joints, ["HeadYaw", "LShoulderPitch"])
        self.assertAlmostEqual(angles[0], 0.19)
        self.assertEqual(speed, 0.05)  # la vitesse la plus lente gagne

    def test_rate_limit(self):
        g, proxy = guard(min_interval=0.15)
        t0 = time.time()
        for i in range(3):
            g.submit(["HeadYaw"], [0.1 * i], 0.1)
            wait_calls(proxy, i + 1)
        g.stop()
        self.assertGreaterEqual(time.time() - t0, 0.29)

    def test_busy_drops_moves(self):
        g, proxy = guard()
        g.set_busy("posture", True)
        self.assertEqual(g.submit(["HeadYaw"], [0.1], 0.1), 0)
        g.set_busy("posture", False)
        self.assertEqual(g.submit(["HeadYaw"], [0.1], 0.1), 1)
        g.stop()

    def test_hot_group_refused(self):
        g, proxy = guard()
        g.set_motor_temps({"head": 75, "larm": 40})
        self.assertEqual(g.submit(["HeadYaw", "LShoulderPitch"], [0.1, 1.0], 0.1), 1)
        g.stop()

    def test_posture_whitelist_and_cooldown(self):
        g, _ = guard(posture_cooldown=5.0)
        self.assertFalse(g.posture_allowed("LyingBack", now=100.0))
        self.assertTrue(g.posture_allowed("Stand", now=100.0))
        self.assertFalse(g.posture_allowed("Sit", now=102.0))
        self.assertTrue(g.posture_allowed("Sit", now=106.0))
        g.stop()


class TestBridge(unittest.TestCase):

    def setUp(self):
        self.bridge = b.NaoBridge("ws://test/", motion_limits={"posture_cooldown": 0})
        self.proxy = FakeMotion()
        self.bridge._guard._proxy_factory = lambda: self.proxy

    def tearDown(self):
        self.bridge._guard.stop()

    def test_stiffness_off_blocks_move(self):
        self.bridge._motion_joint("HeadYaw", 0.1, 0.1)
        time.sleep(0.2)
        self.assertEqual(self.proxy.calls, [])

    def test_move_goes_through_guard(self):
        self.bridge._set_stiffness(["head"], 1.0)
        self.bridge._dispatch("/nao/motion", {"data": '{"joint": "HeadPitch", "angle": 9, "speed": 2}'})
        calls = wait_calls(self.proxy, 1)
        self.assertEqual(calls[0][0], ["HeadPitch"])
        self.assertAlmostEqual(calls[0][1][0], 0.5149 - 0.05)
        self.assertEqual(calls[0][2], 0.3)

    def test_walk_is_capped_and_blocks_arms(self):
        self.bridge._set_stiffness(["larm"], 1.0)
        self.bridge._move(1.0, 0.0, 0.0)
        self.assertTrue(self.bridge._guard.busy_with("walk"))
        self.bridge._motion_joint("LShoulderPitch", 1.0, 0.1)
        self.bridge._stop_move()
        self.assertFalse(self.bridge._guard.busy_with("walk"))
        time.sleep(0.2)
        self.assertEqual(self.proxy.calls, [])

    def test_posture_runs_off_the_receive_thread(self):
        self.bridge._posture("Stand")
        end = time.time() + 2
        while self.bridge._guard.busy_with("posture") and time.time() < end:
            time.sleep(0.01)
        self.assertFalse(self.bridge._guard.busy_with("posture"))

    def test_stiffness_capped(self):
        bridge = b.NaoBridge("ws://test/", motion_limits={"stiffness_max": 0.6})
        bridge._set_stiffness(["head", "legs"], 1.0)
        self.assertEqual(bridge._stiffness_enabled, {"head"})
        bridge._guard.stop()


if __name__ == "__main__":
    unittest.main()
