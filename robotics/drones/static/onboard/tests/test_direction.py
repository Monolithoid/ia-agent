"""Tests sans matériel des fonctions de direction (numpy uniquement).

    cd robotics/drones/static/onboard && python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import direction  # noqa: E402

RATE = 16000


def tone(amplitude, seconds=0.2, freq=440.0):
    t = np.arange(int(RATE * seconds)) / RATE
    return amplitude * np.sin(2 * np.pi * freq * t)


class TestLoudestMic(unittest.TestCase):

    def setUp(self):
        self.mics = direction.parse_mics("gauche:-60, droite:60")
        self.angles = [a for _, a in self.mics]

    def test_parse_mics(self):
        self.assertEqual(self.mics, [("gauche", -60.0), ("droite", 60.0)])
        self.assertEqual(direction.parse_mics(""), [])
        with self.assertRaises(ValueError):
            direction.parse_mics("gauche")

    def test_right_louder(self):
        rng = np.random.default_rng(0)
        frames = np.stack([tone(1000) + rng.normal(0, 50, int(RATE * 0.2)),
                           tone(4000) + rng.normal(0, 50, int(RATE * 0.2))], axis=1)
        levels = direction.channel_rms(frames)
        self.assertEqual(direction.loudest_mic(levels, self.angles), 60.0)

    def test_left_louder(self):
        frames = np.stack([tone(3000), tone(500)], axis=1)
        self.assertEqual(direction.loudest_mic(direction.channel_rms(frames), self.angles), -60.0)

    def test_three_mics(self):
        angles = [a for _, a in direction.parse_mics("g:-90,c:0,d:90")]
        frames = np.stack([tone(200), tone(2500), tone(800)], axis=1)
        self.assertEqual(direction.loudest_mic(direction.channel_rms(frames), angles), 0.0)

    def test_silence_gives_none(self):
        self.assertIsNone(direction.loudest_mic(np.zeros(2), self.angles))

    def test_mismatched_lengths(self):
        with self.assertRaises(ValueError):
            direction.loudest_mic([1.0, 2.0, 3.0], self.angles)


class TestImageMotion(unittest.TestCase):

    W, H, HFOV = 320, 240, 66.0

    def frames_with_block(self, x0, x1):
        prev = np.full((self.H, self.W), 100, dtype=np.uint8)
        curr = prev.copy()
        curr[80:160, x0:x1] = 220
        return prev, curr

    def test_center(self):
        prev, curr = self.frames_with_block(150, 170)
        self.assertAlmostEqual(direction.motion_angle(prev, curr, self.HFOV), 0.0, places=6)

    def test_right_edge(self):
        prev, curr = self.frames_with_block(self.W - 20, self.W)
        angle = direction.motion_angle(prev, curr, self.HFOV)
        # x moyen = 310 → (310/320 − 0.5) × 66 = 30.9375
        self.assertAlmostEqual(angle, 30.9375, places=6)

    def test_left_quarter(self):
        prev, curr = self.frames_with_block(70, 90)
        angle = direction.motion_angle(prev, curr, self.HFOV)
        self.assertAlmostEqual(angle, (80 / self.W - 0.5) * self.HFOV, places=6)
        self.assertLess(angle, 0)

    def test_no_motion(self):
        prev = np.full((self.H, self.W), 100, dtype=np.uint8)
        self.assertIsNone(direction.motion_angle(prev, prev.copy(), self.HFOV))

    def test_noise_below_threshold(self):
        rng = np.random.default_rng(1)
        prev = np.full((self.H, self.W), 100, dtype=np.uint8)
        curr = (prev + rng.integers(-10, 10, prev.shape)).astype(np.uint8)
        self.assertIsNone(direction.motion_angle(prev, curr, self.HFOV, threshold=25))

    def test_bounded_by_fov(self):
        prev, curr = self.frames_with_block(0, self.W)
        prev[:] = 0
        angle = direction.motion_angle(prev, curr, self.HFOV)
        self.assertLessEqual(abs(angle), self.HFOV / 2)


class TestHelpers(unittest.TestCase):

    def test_choose_direction_source(self):
        c = direction.choose_direction_source
        self.assertEqual(c(True, 0, True), "respeaker")
        self.assertEqual(c(False, 2, True), "loudest_mic")
        self.assertEqual(c(False, 1, True), "image")
        self.assertEqual(c(False, 0, False), "none")

    def test_respeaker_angle(self):
        self.assertEqual(direction.respeaker_angle(0), 0.0)
        self.assertEqual(direction.respeaker_angle(270), -90.0)
        self.assertEqual(direction.respeaker_angle(90, offset_deg=-90), 0.0)
        self.assertEqual(direction.respeaker_angle(90, invert=True), -90.0)
        self.assertEqual(direction.respeaker_angle(180), 180.0)

    def test_circular_mean_wraps(self):
        self.assertAlmostEqual(direction.circular_mean_deg([350, 10]), 0.0, places=6)
        self.assertIsNone(direction.circular_mean_deg([]))


if __name__ == "__main__":
    unittest.main()
