"""Tests sans ROS 2 de l'animation « speaking » du NAO.

    cd robotics/drones/nao/cognitive_renderer && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from speaking_animation import speaking_expired, speaking_frame  # noqa: E402

# Limites NAO V5 (rad), cf. commentaire _ARM_POSES du renderer
LIMITS = {
    "HeadPitch": (-0.67, 0.51),
    "LShoulderPitch": (-2.09, 2.09), "RShoulderPitch": (-2.09, 2.09),
    "LShoulderRoll": (-0.31, 1.33),  "RShoulderRoll": (-1.33, 0.31),
    "LElbowYaw": (-2.09, 2.09),      "RElbowYaw": (-2.09, 2.09),
    "LElbowRoll": (-1.54, -0.03),    "RElbowRoll": (0.03, 1.54),
}


class TestSpeakingFrame(unittest.TestCase):

    def test_eyes_blink(self):
        bright, _ = speaking_frame(0)
        dim, _ = speaking_frame(1)
        self.assertEqual(bright["group"], "FaceLeds")
        self.assertGreater(bright["r"], dim["r"])

    def test_one_arm_per_message(self):
        # nao_ws_bridge rejette un message multi-groupes si un bras est sans stiffness
        for tick in range(4):
            _, motions = speaking_frame(tick)
            for m in motions:
                joints = m.get("joints", [m.get("joint")])
                self.assertEqual(len({j[0] for j in joints if j.startswith(("L", "R"))}),
                                 1 if "joints" in m else 0)

    def test_arms_alternate(self):
        _, m0 = speaking_frame(0)
        _, m1 = speaking_frame(1)
        _, m2 = speaking_frame(2)
        self.assertEqual(len(m1), 1)            # tête seule
        self.assertNotEqual(m0[1]["angles"], m2[1]["angles"])

    def test_angles_within_limits(self):
        for tick in range(4):
            _, motions = speaking_frame(tick)
            for m in motions:
                pairs = zip(m["joints"], m["angles"]) if "joints" in m else [(m["joint"], m["angle"])]
                for joint, angle in pairs:
                    lo, hi = LIMITS[joint]
                    self.assertTrue(lo <= angle <= hi, f"{joint}={angle}")


class TestExpired(unittest.TestCase):

    def test_timeout(self):
        self.assertFalse(speaking_expired(0.0, 100.0, 120.0))
        self.assertTrue(speaking_expired(0.0, 121.0, 120.0))
        self.assertFalse(speaking_expired(0.0, 1e6, 0))


if __name__ == "__main__":
    unittest.main()
