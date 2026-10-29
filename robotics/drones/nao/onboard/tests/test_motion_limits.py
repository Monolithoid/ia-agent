"""Tests sans robot des bornes de mouvement de nao_ws_bridge (mode MOCK).

Les décisions (rythme, fin du geste, postures, chaleur, repos) sont dans le
driver NAO : robotics/drones/nao/driver/tests/test_motion_policy.py.

    cd robotics/drones/nao/onboard && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nao_ws_bridge as b  # noqa: E402


class FakeMotion(object):
    """ALMotion factice : enregistre les appels setAngles."""

    def __init__(self):
        self.calls = []

    def setAngles(self, joints, angles, speed):
        self.calls.append((list(joints), list(angles), speed))

    def setStiffnesses(self, group, value):
        self.calls.append(("stiffness", group, value))

    def angleInterpolationWithSpeed(self, joints, angles, speed):
        self.calls.append(("interpolation", list(joints), list(angles), speed))


class TestLimitsConfig(unittest.TestCase):

    def test_defaults(self):
        self.assertEqual(b._motion_limits(None), b._MOTION_LIMITS_DEFAULT)

    def test_override_and_bad_values_ignored(self):
        # min_interval est désormais une décision du driver : clé inconnue ici.
        lim = b._motion_limits({"max_speed": 0.2, "min_interval": 0.1,
                                "walk_max": "vite", "fall_manager": 1})
        self.assertEqual(lim["max_speed"], 0.2)
        self.assertNotIn("min_interval", lim)
        self.assertEqual(lim["walk_max"], b._MOTION_LIMITS_DEFAULT["walk_max"])
        self.assertTrue(lim["fall_manager"])


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


class TestBridge(unittest.TestCase):

    def setUp(self):
        self.bridge = b.NaoBridge("ws://test/")
        self.proxy = FakeMotion()
        self.bridge._guard._proxy_factory = lambda: self.proxy

    def test_stiffness_off_blocks_move(self):
        self.bridge._motion_joint("HeadYaw", 0.1, 0.1)
        self.assertEqual(self.proxy.calls, [])

    def test_move_is_bounded_and_sent_at_once(self):
        self.bridge._set_stiffness(["head"], 1.0)
        self.bridge._dispatch("/nao/motion", {"data": '{"joint": "HeadPitch", "angle": 9, "speed": 2}'})
        self.assertEqual(len(self.proxy.calls), 1)
        joints, angles, speed = self.proxy.calls[0]
        self.assertEqual(joints, ["HeadPitch"])
        self.assertAlmostEqual(angles[0], 0.5149 - 0.05)
        self.assertEqual(speed, 0.3)

    def test_no_decision_on_robot(self):
        # Deux ordres rapprochés partent tous les deux : le rythme est décidé par le driver.
        self.bridge._set_stiffness(["head"], 1.0)
        self.bridge._motion_joint("HeadYaw", 0.1, 0.1)
        self.bridge._motion_joint("HeadYaw", 0.2, 0.1)
        self.assertEqual(len(self.proxy.calls), 2)

    def test_legacy_call(self):
        bridge = b.NaoBridge("ws://test/", motion_limits={"motion_call": u"legacy"})
        bridge._guard._proxy_factory = lambda: self.proxy
        bridge._set_stiffness(["head"], 1.0)
        bridge._motion_joint("HeadYaw", 0.1, 0.1)
        self.assertEqual(self.proxy.calls, [("stiffness", "Head", 1.0),
                                            ("interpolation", ["HeadYaw"], [0.1], 0.1)])

    def test_stiffness_capped_and_legs_blocked(self):
        bridge = b.NaoBridge("ws://test/", motion_limits={"stiffness_max": 0.6})
        bridge._set_stiffness(["head", "legs"], 1.0)
        self.assertEqual(bridge._stiffness_enabled, {"head"})


if __name__ == "__main__":
    unittest.main()
