"""Tests sans ROS 2 des règles de body_gateway (routage, mot de réveil, direction).

    cd robotics/brain/body_gateway && python3 -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import body_rules  # noqa: E402

REGISTRY = ["nao_01", "static_01"]


class TestRouting(unittest.TestCase):

    def test_body_type(self):
        self.assertEqual(body_rules.body_type("static_01"), "static")
        self.assertEqual(body_rules.body_type("nao_01"), "nao")
        self.assertEqual(body_rules.body_type("robot_x"), "nao")

    def test_speech_goes_to_connected_static(self):
        connected = {"nao_01": True, "static_01": True}
        action = {"body": "nao_01", "type": "speech", "text": "bonjour"}
        self.assertEqual(body_rules.route_action(action, REGISTRY, connected), "static_01")

    def test_speech_stays_on_nao_without_static(self):
        connected = {"nao_01": True, "static_01": False}
        for body in ("nao_01", "static_01"):
            action = {"body": body, "type": "speech", "text": "bonjour"}
            self.assertEqual(body_rules.route_action(action, REGISTRY, connected), "nao_01")

    def test_nao_only_registry_unchanged(self):
        action = {"body": "nao_01", "type": "speech", "text": "bonjour"}
        self.assertEqual(body_rules.route_action(action, ["nao_01"], {}), "nao_01")
        self.assertEqual(body_rules.route_action({"type": "leds"}, ["nao_01"], {}), "nao_01")

    def test_gestures_go_to_nao(self):
        connected = {"nao_01": True, "static_01": True}
        for kind in ("motion", "leds", "move", "look"):
            action = {"body": "static_01", "type": kind}
            self.assertEqual(body_rules.route_action(action, REGISTRY, connected), "nao_01")

    def test_speech_goes_to_every_connected_voice(self):
        registry = ["nao_01", "static_01", "obs_01"]
        action = {"body": "nao_01", "type": "speech", "text": "bonjour"}
        both = {"nao_01": True, "static_01": True, "obs_01": True}
        self.assertEqual(body_rules.route_targets(action, registry, both), ["static_01", "obs_01"])
        only_obs = {"nao_01": True, "static_01": False, "obs_01": True}
        self.assertEqual(body_rules.route_targets(action, registry, only_obs), ["obs_01"])
        self.assertEqual(body_rules.speech_bodies(registry, {}, "nao_01"), ["nao_01"])
        self.assertEqual(body_rules.route_targets({"body": "obs_01", "type": "leds"},
                                                  registry, both), ["nao_01"])
        self.assertEqual(body_rules.body_type("obs_01"), "obs")

    def test_unknown_body(self):
        self.assertIsNone(body_rules.route_action({"body": "x", "type": "speech"}, REGISTRY, {}))

    def test_describe_bodies(self):
        text = body_rules.describe_bodies(REGISTRY, "static_01")
        self.assertIn("- static_01 : parler, entendre, voir", text)
        self.assertIn("- nao_01 : gestes, tête, LEDs", text)
        self.assertIn("- nao_01 : parler, gestes", body_rules.describe_bodies(["nao_01"], "nao_01"))


class TestWakeWord(unittest.TestCase):

    def setUp(self):
        self.gate = body_rules.WakeWordGate("ok nao", window_s=10)

    def test_wake_word_and_question(self):
        self.assertEqual(self.gate.filter("s", "OK Nao, quelle heure est-il ?", now=0),
                         "quelle heure est-il ?")
        self.assertEqual(self.gate.filter("s", "Okay, Nao. Tu m'entends ?", now=0),
                         "Tu m'entends ?")

    def test_without_wake_word_ignored(self):
        self.assertIsNone(self.gate.filter("s", "Il fait beau aujourd'hui.", now=0))

    def test_lone_wake_word_opens_window(self):
        self.assertIsNone(self.gate.filter("s", "Ok Nao.", now=0))
        self.assertTrue(self.gate.armed("s", now=5))
        self.assertEqual(self.gate.filter("s", "Raconte une blague", now=5), "Raconte une blague")
        # Window consumed by that sentence.
        self.assertIsNone(self.gate.filter("s", "Et une autre", now=6))

    def test_window_expires(self):
        self.gate.filter("s", "ok nao", now=0)
        self.assertIsNone(self.gate.filter("s", "Raconte une blague", now=11))

    def test_aliases(self):
        gate = body_rules.WakeWordGate("ok nao", window_s=10, aliases=["ok now", "OK no"])
        self.assertEqual(gate.filter("s", "Ok, now. Quelle heure est-il ?", now=0),
                         "Quelle heure est-il ?")
        self.assertIsNone(gate.filter("s", "Okay no.", now=0))
        self.assertTrue(gate.armed("s", now=1))
        # Without aliases, a mishearing is ignored.
        self.assertIsNone(self.gate.filter("s", "Ok, now. Quelle heure est-il ?", now=0))


class TestDirection(unittest.TestCase):

    def test_label(self):
        self.assertEqual(body_rules.direction_label(-30), "[voix : 30° à gauche]")
        self.assertEqual(body_rules.direction_label(42.4), "[voix : 42° à droite]")
        self.assertEqual(body_rules.direction_label(2), "[voix : en face]")
        self.assertEqual(body_rules.direction_label(None), "")

    def test_head_yaw(self):
        # Voice 30° to the right → NAO head turns right (negative HeadYaw).
        self.assertAlmostEqual(body_rules.head_yaw_rad(30), -math.radians(30))
        self.assertAlmostEqual(body_rules.head_yaw_rad(30, offset_deg=10), -math.radians(20))
        # Clamped to the HeadYaw joint limits.
        self.assertEqual(body_rules.head_yaw_rad(-170), body_rules.HEAD_YAW_LIMIT_RAD)
        self.assertEqual(body_rules.head_yaw_rad(170), -body_rules.HEAD_YAW_LIMIT_RAD)


class TestMotorRanges(unittest.TestCase):

    def test_label(self):
        text = body_rules.motor_ranges_label(
            {"rarm": {"RShoulderPitch": [-0.5, 1.2]}, "head": {"HeadYaw": [-0.8, 0.8]}})
        self.assertEqual(text, "- tête :\n"
                               "  HeadYaw de -0.80 à +0.80 (regarde à droite → regarde à gauche)\n"
                               "- bras droit :\n"
                               "  RShoulderPitch de -0.50 à +1.20 (bras levé → bras baissé)")

    def test_not_calibrated(self):
        self.assertIn("non calibré", body_rules.motor_ranges_label({}))
        self.assertIn("non calibré", body_rules.motor_ranges_label({"head": {}}))

    RANGES = {
        "head": {"HeadYaw": [-0.8, 0.8], "HeadPitch": [-0.4, 0.3]},
        "larm": {"LShoulderPitch": [-1.2, 1.0], "LShoulderRoll": [0.0, 1.0],
                 "LElbowRoll": [-1.5, -0.1]},
        "rarm": {"RShoulderPitch": [-1.1, 1.0], "RShoulderRoll": [-1.0, 0.0],
                 "RElbowRoll": [0.1, 1.5]},
    }

    def test_examples_use_range_ends(self):
        text = body_rules.motion_examples(self.RANGES, "nao_01")
        self.assertIn('"joints": ["RShoulderPitch", "RElbowRoll"], "angles": [-1.10, 0.10]', text)
        self.assertIn('"joints": ["LShoulderRoll"], "angles": [1.00]', text)
        self.assertIn('"joints": ["RShoulderRoll"], "angles": [-1.00]', text)
        self.assertIn('"joints": ["HeadYaw", "HeadPitch"], "angles": [0.80, -0.40]', text)
        self.assertIn('"joints": ["LShoulderPitch"], "angles": [-1.20]', text)
        self.assertEqual(text.count("\n- "), 4)

    def test_examples_skip_missing_joints(self):
        text = body_rules.motion_examples({"head": self.RANGES["head"]}, "nao_01")
        self.assertIn("regarder tout en haut à gauche", text)
        self.assertNotIn("bras", text)
        self.assertEqual(body_rules.motion_examples({}, "nao_01"), "")


if __name__ == "__main__":
    unittest.main()
