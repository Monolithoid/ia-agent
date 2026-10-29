"""Tests sans ROS 2 du retour à la pose de lancement (rest_pose.py).

    cd robotics/drones/nao/driver && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rest_pose import RestTracker, parse_motion  # noqa: E402


class TestParseMotion(unittest.TestCase):

    def test_single_and_multi(self):
        self.assertEqual(parse_motion({"joint": "HeadYaw", "angle": 0.2, "speed": 0.1}),
                         (["HeadYaw"], [0.2], 0.1))
        self.assertEqual(parse_motion({"joints": ["LShoulderPitch"], "angles": [1], "speed": 0.2}),
                         (["LShoulderPitch"], [1.0], 0.2))
        self.assertEqual(parse_motion({}), ([], [], 0.05))


class TestRestTracker(unittest.TestCase):

    def setUp(self):
        self.t = RestTracker(delay=3.0, speed=0.1, tolerance=0.02)
        self.t.set_rest({"HeadYaw": 0.0, "LShoulderPitch": 1.5})

    def test_no_return_before_delay(self):
        self.t.on_order(["HeadYaw"], [0.1], 0.5, now=100.0)
        self.assertIsNone(self.t.due(102.9))

    def test_return_after_delay_once(self):
        self.t.on_order(["HeadYaw"], [0.1], 0.5, now=100.0)
        cmd = self.t.due(104.0)
        self.assertEqual(cmd, {"joints": ["HeadYaw"], "angles": [0.0], "speed": 0.1})
        self.assertIsNone(self.t.due(110.0))

    def test_long_move_delays_return(self):
        # 2 rad à 0.05 x 7.19 rad/s : environ 5.6 s de geste, puis 3 s d'attente.
        self.t.on_order(["HeadYaw"], [2.0], 0.05, now=100.0)
        self.assertIsNone(self.t.due(108.0))
        self.assertIsNotNone(self.t.due(109.0))

    def test_new_order_restarts_delay(self):
        self.t.on_order(["HeadYaw"], [0.1], 0.5, now=100.0)
        self.t.on_order(["HeadYaw"], [0.2], 0.5, now=102.0)
        self.assertIsNone(self.t.due(104.0))
        self.assertIsNotNone(self.t.due(105.1))

    def test_joint_already_at_rest_or_unknown(self):
        self.t.on_order(["LShoulderPitch", "RHand"], [1.51, 1.0], 0.5, now=100.0)
        self.assertIsNone(self.t.due(200.0))

    def test_new_launch_pose_resets(self):
        self.t.on_order(["HeadYaw"], [0.5], 0.5, now=100.0)
        self.t.set_rest({"HeadYaw": 0.5})
        self.assertIsNone(self.t.due(200.0))

    def test_all_to_rest(self):
        self.t.on_order(["HeadYaw", "LShoulderPitch"], [0.4, 0.2], 0.5, now=100.0)
        cmd = self.t.all_to_rest()
        self.assertEqual(cmd["joints"], ["HeadYaw", "LShoulderPitch"])
        self.assertEqual(cmd["angles"], [0.0, 1.5])
        self.assertIsNone(self.t.all_to_rest())


if __name__ == "__main__":
    unittest.main()
