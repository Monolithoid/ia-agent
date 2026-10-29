"""Tests sans ROS 2 des décisions de mouvement du driver NAO (motion_policy.py).

    cd robotics/drones/nao/driver && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motion_policy import DEFAULT_LIMITS, MotionPolicy, load_limits, parse_motion  # noqa: E402


def policy(**overrides):
    limits, warnings = load_limits(overrides)
    assert not warnings, warnings
    return MotionPolicy(limits)


class TestConfig(unittest.TestCase):

    def test_defaults_and_bad_values(self):
        limits, warnings = load_limits({"min_interval": 0.2, "while_moving": "garder",
                                        "inconnu": 1, "rest_return": 1})
        self.assertEqual(limits["min_interval"], 0.2)
        self.assertEqual(limits["while_moving"], "drop")
        self.assertTrue(limits["rest_return"])
        self.assertEqual(len(warnings), 3)
        self.assertEqual(load_limits(None)[0], DEFAULT_LIMITS)

    def test_parse_motion(self):
        self.assertEqual(parse_motion({"joint": "HeadYaw", "angle": 0.2, "speed": 0.1}),
                         (["HeadYaw"], [0.2], 0.1))
        self.assertEqual(parse_motion({"joints": ["LShoulderPitch"], "angles": [1], "speed": 0.2}),
                         (["LShoulderPitch"], [1.0], 0.2))


class TestRateAndMoves(unittest.TestCase):

    def test_rate_limit_and_merge(self):
        p = policy(min_interval=0.1, min_move_time=0, velocity_scale=0)
        p.submit(["HeadYaw"], [0.1], 0.1, now=0.0)
        self.assertEqual(p.take(0.0)["joints"], ["HeadYaw"])
        p.submit(["HeadYaw"], [0.2], 0.1, now=0.01)
        p.submit(["HeadYaw"], [0.3], 0.1, now=0.02)
        p.submit(["LShoulderPitch"], [1.0], 0.05, now=0.03)
        self.assertIsNone(p.take(0.05))   # 10 Hz max
        cmd = p.take(0.1)
        self.assertEqual(cmd, {"joints": ["HeadYaw", "LShoulderPitch"],
                               "angles": [0.3, 1.0], "speed": 0.05})

    def test_orders_dropped_while_moving(self):
        p = policy(min_move_time=0.4, velocity_scale=0)
        p.submit(["HeadYaw"], [0.1], 0.1, now=0.0)
        p.take(0.0)
        self.assertEqual(p.submit(["HeadYaw", "LShoulderPitch"], [0.2, 1.0], 0.1, now=0.2), 1)
        self.assertEqual(p.take(0.2)["joints"], ["LShoulderPitch"])
        self.assertEqual(p.take_dropped(), 1)
        self.assertEqual(p.submit(["HeadYaw"], [0.3], 0.1, now=0.5), 1)

    def test_latest_waits(self):
        p = policy(min_move_time=0.4, velocity_scale=0, while_moving="latest")
        p.submit(["HeadYaw"], [0.1], 0.1, now=0.0)
        p.take(0.0)
        p.submit(["HeadYaw"], [0.2], 0.1, now=0.1)
        p.submit(["HeadYaw"], [0.3], 0.1, now=0.2)
        self.assertIsNone(p.take(0.3))
        self.assertEqual(p.take(0.4)["angles"], [0.3])

    def test_duration_from_joint_velocity(self):
        p = policy(min_move_time=0.3)
        p.submit(["HeadYaw"], [0.829], 0.1, now=0.0)   # depuis 0 inconnu : min_move_time
        p.take(0.0)
        p.submit(["HeadYaw"], [0.0], 0.1, now=0.5)     # 0.829 rad à 0.829 rad/s : 1 s
        p.take(0.5)
        self.assertEqual(p.submit(["HeadYaw"], [0.5], 0.1, now=1.4), 0)
        self.assertEqual(p.submit(["HeadYaw"], [0.5], 0.1, now=1.6), 1)


class TestPostureWalkHeat(unittest.TestCase):

    def test_posture_whitelist_cooldown_and_block(self):
        p = policy(posture_cooldown=5.0, posture_block=4.0)
        self.assertFalse(p.posture_allowed("LyingBack", 0.0)[0])
        self.assertTrue(p.posture_allowed("Stand", 0.0)[0])
        self.assertEqual(p.submit(["HeadYaw"], [0.1], 0.1, now=1.0), 0)
        self.assertFalse(p.walk_allowed(1.0))
        self.assertFalse(p.posture_allowed("Sit", 3.0)[0])
        self.assertTrue(p.posture_allowed("Sit", 6.0)[0])
        self.assertEqual(p.submit(["HeadYaw"], [0.1], 0.1, now=10.1), 1)

    def test_walk_blocks_joints_and_postures(self):
        p = policy()
        p.set_walking(True)
        self.assertEqual(p.submit(["HeadYaw"], [0.1], 0.1, now=0.0), 0)
        self.assertFalse(p.posture_allowed("Stand", 0.0)[0])
        p.set_walking(False)
        self.assertEqual(p.submit(["HeadYaw"], [0.1], 0.1, now=0.0), 1)

    def test_hot_group(self):
        p = policy()
        self.assertEqual(p.set_motor_temps({"head": 75, "larm": 40}), {"head"})
        self.assertEqual(p.submit(["HeadYaw", "LShoulderPitch"], [0.1, 1.0], 0.1, now=0.0), 1)


class TestRest(unittest.TestCase):

    def setUp(self):
        self.p = policy(min_interval=0.0, min_move_time=0.3, rest_delay=3.0)
        self.p.set_rest({"HeadYaw": 0.0, "LShoulderPitch": 1.5})

    def test_returns_after_delay_once(self):
        self.p.submit(["HeadYaw"], [0.1], 0.5, now=100.0)
        self.p.take(100.0)
        self.assertIsNone(self.p.take(103.0))
        self.assertEqual(self.p.take(103.4), {"joints": ["HeadYaw"], "angles": [0.0], "speed": 0.1})
        self.assertIsNone(self.p.take(110.0))

    def test_new_order_interrupts_rest(self):
        self.p.submit(["HeadYaw"], [0.5], 0.5, now=100.0)
        self.p.take(100.0)
        self.assertEqual(self.p.take(104.0)["angles"], [0.0])
        self.assertEqual(self.p.submit(["HeadYaw"], [0.2], 0.5, now=104.1), 1)
        self.assertEqual(self.p.take(104.1)["angles"], [0.2])

    def test_already_at_rest_or_no_pose(self):
        self.p.submit(["LShoulderPitch", "RHand"], [1.51, 1.0], 0.5, now=100.0)
        self.p.take(100.0)
        self.assertIsNone(self.p.take(200.0))

    def test_new_launch_pose_resets(self):
        self.p.submit(["HeadYaw"], [0.5], 0.5, now=100.0)
        self.p.take(100.0)
        self.p.set_rest({"HeadYaw": 0.5})
        self.assertIsNone(self.p.take(200.0))

    def test_all_to_rest(self):
        self.p.submit(["HeadYaw", "LShoulderPitch"], [0.4, 0.2], 0.5, now=100.0)
        self.p.take(100.0)
        cmd = self.p.all_to_rest()
        self.assertEqual(cmd["joints"], ["HeadYaw", "LShoulderPitch"])
        self.assertEqual(cmd["angles"], [0.0, 1.5])
        self.assertIsNone(self.p.all_to_rest())


if __name__ == "__main__":
    unittest.main()
