"""Tests sans ROS 2 des décisions de mouvement du driver NAO (motion_policy.py).

    cd robotics/drones/nao/driver && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motion_policy import (DEFAULT_LIMITS, MotionPolicy, chain_of, load_limits,  # noqa: E402
                           load_profile, parse_motion)

HEAD = {"rest":  {"HeadYaw": 0.0, "HeadPitch": 0.3},
        "start": {"HeadYaw": 0.0, "HeadPitch": 0.0},
        "range": {"HeadYaw": [-0.5, 0.5], "HeadPitch": [-0.2, 0.2]},
        "speed": 0.1}
RARM = {"rest":  {"RShoulderPitch": 1.5, "RShoulderRoll": -0.1, "RElbowYaw": 1.2, "RElbowRoll": 0.4},
        "start": {"RShoulderPitch": 1.0, "RShoulderRoll": -0.1, "RElbowYaw": 1.2, "RElbowRoll": 0.4},
        "range": {"RShoulderPitch": [-1.0, 1.2], "RShoulderRoll": [-0.5, 0.0],
                  "RElbowYaw": [1.0, 1.4], "RElbowRoll": [0.2, 1.0]},
        "speed": 0.2}


def policy(**overrides):
    # Pas d'estimation de durée ni de regroupement : chaque étape dure min_move_time.
    base = {"min_interval": 0.0, "min_move_time": 1.0, "velocity_scale": 0, "merge_window": 0.0}
    base.update(overrides)
    limits, warnings = load_limits(base)
    assert not warnings, warnings
    p = MotionPolicy(limits)
    chains, errors = load_profile({"chains": {"head": HEAD, "rarm": RARM}})
    assert not errors, errors
    p.set_profile(chains)
    return p


def head(cmd):
    return dict(zip(cmd["joints"], cmd["angles"]))


class TestConfig(unittest.TestCase):

    def test_defaults_and_bad_values(self):
        limits, warnings = load_limits({"min_interval": 0.2, "while_moving": "drop",
                                        "rest_return": 1})
        self.assertEqual(limits["min_interval"], 0.2)
        self.assertTrue(limits["rest_return"])
        self.assertEqual(len(warnings), 2)
        self.assertEqual(load_limits(None)[0], DEFAULT_LIMITS)

    def test_parse_motion(self):
        self.assertEqual(parse_motion({"joint": "HeadYaw", "angle": 0.2, "speed": 0.1}),
                         (["HeadYaw"], [0.2], 0.1))
        self.assertEqual(parse_motion({"joints": ["LShoulderPitch"], "angles": [1]}),
                         (["LShoulderPitch"], [1.0], 0.0))

    def test_chain_of(self):
        self.assertEqual(chain_of("HeadPitch"), "head")
        self.assertEqual(chain_of("LElbowRoll"), "larm")
        self.assertEqual(chain_of("RWristYaw"), "")

    def test_profile_validation(self):
        bad_start = dict(HEAD, start={"HeadYaw": 0.9, "HeadPitch": 0.0})
        missing = {k: v for k, v in RARM.items() if k != "range"}
        chains, errors = load_profile({"chains": {"head": bad_start, "rarm": missing,
                                                  "legs": HEAD}})
        self.assertEqual(chains, {})
        self.assertEqual(len(errors), 3)
        self.assertEqual(load_profile(None), ({}, []))


class TestSequence(unittest.TestCase):

    def test_rest_start_target_start_rest(self):
        p = policy(rest_delay=3.0)
        self.assertEqual(p.submit(["HeadYaw"], [0.3], 0.0, now=0.0), 1)
        self.assertEqual(head(p.take(0.0)[0]), {"HeadYaw": 0.0, "HeadPitch": 0.0})   # départ
        self.assertEqual(p.take(0.5), [])                                             # étape en cours
        cmd = p.take(1.0)[0]
        self.assertEqual(head(cmd), {"HeadYaw": 0.3, "HeadPitch": 0.0})               # cible
        self.assertEqual(cmd["speed"], 0.1)                                           # vitesse du profil
        # Inactive rest_delay après la fin de la cible (2.0) : retour à 5.0.
        self.assertEqual(p.take(4.5), [])
        self.assertEqual(head(p.take(5.0)[0]), {"HeadYaw": 0.0, "HeadPitch": 0.0})   # départ
        self.assertEqual(head(p.take(6.0)[0]), {"HeadYaw": 0.0, "HeadPitch": 0.3})   # repos
        self.assertEqual(p.take(20.0), [])

    def test_target_clamped_and_speed_capped(self):
        p = policy()
        p.submit(["HeadYaw", "HeadPitch"], [2.0, -1.0], 0.5, now=0.0)
        p.take(0.0)
        cmd = p.take(1.0)[0]
        self.assertEqual(head(cmd), {"HeadYaw": 0.5, "HeadPitch": -0.2})
        self.assertEqual(cmd["speed"], 0.1)

    def test_orders_merge_into_one_target(self):
        p = policy(merge_window=0.3)
        p.submit(["RShoulderPitch"], [-0.5], 0.0, now=0.0)
        p.submit(["RElbowRoll"], [0.9], 0.0, now=0.1)
        self.assertEqual(p.take(0.2), [])
        p.take(0.3)                                  # départ
        target = head(p.take(1.3)[0])
        self.assertEqual(target["RShoulderPitch"], -0.5)
        self.assertEqual(target["RElbowRoll"], 0.9)
        self.assertEqual(target["RShoulderRoll"], -0.1)   # complété par le départ

    def test_new_order_goes_through_start_not_rest(self):
        p = policy(rest_delay=3.0)
        p.submit(["HeadYaw"], [0.3], 0.0, now=0.0)
        p.take(0.0)
        p.take(1.0)                                  # cible 1
        p.submit(["HeadYaw"], [-0.3], 0.0, now=1.5)
        self.assertEqual(p.take(1.5), [])            # cible 1 encore en cours
        self.assertEqual(head(p.take(2.0)[0])["HeadYaw"], 0.0)    # départ
        self.assertEqual(head(p.take(3.0)[0])["HeadYaw"], -0.3)   # cible 2

    def test_order_while_going_back_to_start(self):
        p = policy(rest_delay=1.0)
        p.submit(["HeadYaw"], [0.3], 0.0, now=0.0)
        p.take(0.0)
        p.take(1.0)
        self.assertEqual(head(p.take(3.0)[0])["HeadYaw"], 0.0)    # retour : départ
        p.submit(["HeadYaw"], [0.2], 0.0, now=3.5)
        self.assertEqual(head(p.take(4.0)[0])["HeadYaw"], 0.2)    # directement la cible

    def test_chains_move_together(self):
        p = policy()
        p.submit(["HeadYaw", "RShoulderPitch"], [0.2, 0.0], 0.0, now=0.0)
        cmds = p.take(0.0)
        self.assertEqual([c["joints"][0] for c in cmds], ["HeadYaw", "RShoulderPitch"])
        self.assertEqual(cmds[1]["speed"], 0.2)

    def test_refused_without_profile_or_out_of_chain(self):
        p = policy()
        self.assertEqual(p.submit(["LShoulderPitch", "RWristYaw", "HeadYaw"], [0, 0, 0.1],
                                  0.0, now=0.0), 1)
        self.assertEqual(p.take_refused(), 2)
        empty = MotionPolicy()
        self.assertEqual(empty.submit(["HeadYaw"], [0.1], 0.0, now=0.0), 0)
        self.assertEqual(empty.take(0.0), [])

    def test_rate_limit(self):
        p = policy(min_interval=0.1)
        p.submit(["HeadYaw"], [0.1], 0.0, now=0.0)
        p.take(0.0)
        p.submit(["RShoulderPitch"], [0.0], 0.0, now=0.01)
        self.assertEqual(p.take(0.05), [])
        self.assertEqual(len(p.take(0.1)), 1)

    def test_duration_from_joint_velocity(self):
        p = policy(min_move_time=0.3, velocity_scale=1.0)
        p.submit(["HeadYaw"], [0.5], 0.0, now=0.0)
        p.take(0.0)                                  # départ : HeadPitch 0.3 rad à 0.719 rad/s
        self.assertEqual(p.take(0.4), [])
        p.take(0.5)                                  # cible : 0.5 rad à 0.829 rad/s ≈ 0.6 s
        p.submit(["HeadYaw"], [0.0], 0.0, now=0.6)
        self.assertEqual(p.take(1.0), [])
        self.assertEqual(len(p.take(1.15)), 1)

    def test_new_profile_resets(self):
        p = policy()
        p.submit(["HeadYaw"], [0.3], 0.0, now=0.0)
        p.take(0.0)
        chains, _ = load_profile({"chains": {"head": HEAD}})
        p.set_profile(chains)
        self.assertEqual(p.take(100.0), [])
        self.assertEqual(p.ranges(), {"head": {"HeadYaw": [-0.5, 0.5], "HeadPitch": [-0.2, 0.2]}})


class TestPostureWalkHeat(unittest.TestCase):

    def test_legs_off_by_default(self):
        p = policy()
        self.assertFalse(p.posture_allowed("Stand", 0.0)[0])
        self.assertFalse(p.move_decision(True, 0.0)[0])
        self.assertFalse(p.move_decision(False, 0.0)[0])

    def test_regie_crash_scenario(self):
        # « stand » puis « move 0,0,0 » : le move ne part pas pendant la posture.
        p = policy(postures=["Stand"], walk=True)
        self.assertTrue(p.posture_allowed("Stand", 0.0)[0])
        ok, why = p.move_decision(False, 0.1)
        self.assertFalse(ok)
        self.assertIn("posture", why)

    def test_zero_move_only_stops_a_walk(self):
        p = policy(walk=True)
        self.assertFalse(p.move_decision(False, 0.0)[0])
        self.assertTrue(p.move_decision(True, 0.0)[0])
        self.assertEqual(p.submit(["HeadYaw"], [0.1], 0.0, now=0.0), 0)
        self.assertTrue(p.move_decision(False, 1.0)[0])
        self.assertEqual(p.submit(["HeadYaw"], [0.1], 0.0, now=1.0), 1)

    def test_posture_whitelist_cooldown_and_block(self):
        p = policy(postures=["Stand", "Sit"], walk=True, posture_cooldown=5.0, posture_block=4.0)
        self.assertFalse(p.posture_allowed("LyingBack", 0.0)[0])
        self.assertTrue(p.posture_allowed("Stand", 0.0)[0])
        self.assertEqual(p.submit(["HeadYaw"], [0.1], 0.0, now=1.0), 0)
        self.assertFalse(p.move_decision(True, 1.0)[0])
        self.assertFalse(p.posture_allowed("Sit", 3.0)[0])
        self.assertTrue(p.posture_allowed("Sit", 6.0)[0])
        self.assertEqual(p.submit(["HeadYaw"], [0.1], 0.0, now=10.1), 1)

    def test_hot_chain_refuses_but_returns_to_rest(self):
        p = policy(rest_delay=1.0)
        p.submit(["HeadYaw"], [0.3], 0.0, now=0.0)
        p.take(0.0)
        p.take(1.0)
        self.assertEqual(p.set_motor_temps({"head": 75, "rarm": 40}), {"head"})
        self.assertEqual(p.submit(["HeadYaw", "RShoulderPitch"], [0.1, 0.0], 0.0, now=1.5), 1)
        self.assertEqual(head(p.take(3.0)[0])["HeadYaw"], 0.0)


if __name__ == "__main__":
    unittest.main()
