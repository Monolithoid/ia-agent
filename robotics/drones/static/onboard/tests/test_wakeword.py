"""Tests sans matériel ni openWakeWord du mot de réveil local (WAKE_MODE=local).

    cd robotics/drones/static/onboard && python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import static_drone  # noqa: E402
import wakeword  # noqa: E402

RATE = 16000
BLOCK = 480                 # 30 ms à 16 kHz
LOUD, QUIET = 3000, 0


class FakeModel:
    """Interface d'openwakeword.model.Model : score 0.9 sur les blocs non nuls."""

    def __init__(self):
        self.chunks = []
        self.resets = 0

    def predict(self, chunk):
        self.chunks.append(len(chunk))
        return {"ok_nao": 0.9 if chunk.any() else 0.01}

    def reset(self):
        self.resets += 1


class TestDetector(unittest.TestCase):

    def test_chunks_of_80_ms(self):
        model = FakeModel()
        det = wakeword.WakeWordDetector(model, "ok_nao", 0.5, RATE)
        for _ in range(8):                      # 8 × 30 ms = 240 ms → 3 blocs de 80 ms
            det.process(np.zeros(BLOCK, dtype=np.int16))
        self.assertEqual(model.chunks, [wakeword.CHUNK] * 3)

    def test_threshold(self):
        det = wakeword.WakeWordDetector(FakeModel(), "ok_nao", 0.5, RATE)
        self.assertIsNone(det.process(np.zeros(wakeword.CHUNK, dtype=np.int16)))
        self.assertAlmostEqual(det.process(np.ones(wakeword.CHUNK, dtype=np.int16)), 0.9, places=5)
        det.threshold = 0.95
        self.assertIsNone(det.process(np.ones(wakeword.CHUNK, dtype=np.int16)))

    def test_reset_empties_buffer(self):
        model = FakeModel()
        det = wakeword.WakeWordDetector(model, "ok_nao", 0.5, RATE)
        det.process(np.zeros(1000, dtype=np.int16))
        det.reset()
        det.process(np.zeros(1000, dtype=np.int16))
        self.assertEqual((model.chunks, model.resets), ([], 1))

    def test_48k_input(self):
        # Micro ouvert à 48 kHz : 1 s d'entrée ≈ 1 s à 16 kHz pour le modèle.
        model = FakeModel()
        det = wakeword.WakeWordDetector(model, "ok_nao", 0.5, 48000)
        for _ in range(int(1 / 0.03)):
            det.process(np.zeros(1440, dtype=np.int16))
        self.assertIn(len(model.chunks), (11, 12))


class TestResampler(unittest.TestCase):

    def test_blocks_match_one_pass(self):
        from scipy.signal import resample_poly
        t = np.arange(48000) / 48000.0
        x = (8000 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
        r = wakeword.Resampler(48000)
        y = np.concatenate([r(x[i:i + 1440]) for i in range(0, len(x), 1440)])
        padded = np.concatenate([np.zeros(r.margin), x]).astype(np.float32)
        ref = np.round(resample_poly(padded, 1, 3))[r.margin // 3:][:len(y)]
        self.assertGreater(len(y), 15900)
        self.assertEqual(np.abs(y - ref).max(), 0)

    def test_same_rate(self):
        x = np.arange(10, dtype=np.int16)
        np.testing.assert_array_equal(wakeword.Resampler(16000)(x), x)


class TestResolveModel(unittest.TestCase):

    def test_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ok_nao.onnx")
            open(path, "w").close()
            self.assertEqual(wakeword.resolve_model("ok_nao", d), path)
            self.assertEqual(wakeword.resolve_model(path, "/ailleurs"), path)
            self.assertEqual(wakeword.resolve_model("hey_jarvis", d), "hey_jarvis")


class TestListenAfterWake(unittest.TestCase):
    """Déroulé de _listen_after_wake sur des blocs synthétiques de 30 ms."""

    def setUp(self):
        env = {"RMS_THRESHOLD": "500", "SILENCE_S": "0.3", "MIN_SPEECH_S": "0.1",
               "WAKE_LISTEN_S": "1", "WAKE_PRE_ROLL_S": "0.09", "BLOCK_MS": "30"}
        with mock.patch.dict(os.environ, env):
            self.drone = static_drone.StaticDrone(static_drone.Config())
        self.sent, self.wakes = [], []
        self.drone.publish_audio = lambda pcm, found, wake_score=None: self.sent.append(
            (pcm, wake_score))
        self.drone.publish_wake = self.wakes.append

    def run_blocks(self, levels, wake_at):
        """levels : amplitude de chaque bloc ; le détecteur se déclenche au bloc wake_at."""
        drone = self.drone

        class Detector:
            name = "ok_nao"
            n = 0

            def process(self, mono):
                self.n += 1
                return 0.8 if self.n == wake_at + 1 else None

            def reset(self):
                pass

        drone.wake = Detector()
        blocks = []
        for i, level in enumerate(levels):
            frames = np.full((BLOCK, 1), level, dtype=np.int16)
            frames[0, 0] = i            # marque le bloc pour le retrouver
            blocks.append((frames, frames[:, 0], (i + 1) * 0.03))
        drone._blocks = lambda rate: iter(blocks)
        drone._listen_after_wake(RATE)

    def test_sentence_after_wake_with_pre_roll(self):
        # « ok nao » (blocs 0-9), détection au bloc 9, pause, phrase, silence.
        levels = [LOUD] * 10 + [QUIET] * 10 + [LOUD] * 10 + [QUIET] * 15
        self.run_blocks(levels, wake_at=9)
        self.assertEqual(self.wakes, [0.8])
        self.assertEqual(len(self.sent), 1)
        pcm, score = self.sent[0]
        self.assertEqual(score, 0.8)
        # Pré-roll de 90 ms : les blocs 7, 8 et 9 ouvrent l'enregistrement.
        self.assertEqual(pcm[0], 7)
        self.assertEqual(pcm[3 * BLOCK], 10)

    def test_tail_of_wake_word_is_not_the_sentence(self):
        # La fin de « nao » sonne encore 60 ms après la détection, puis rien.
        levels = [LOUD] * 10 + [LOUD] * 2 + [QUIET] * 40
        self.run_blocks(levels, wake_at=9)
        self.assertEqual(len(self.wakes), 1)
        self.assertEqual(self.sent, [])

    def test_said_in_one_breath(self):
        # « ok nao quelle heure est-il » sans pause : la phrase part quand même.
        levels = [LOUD] * 30 + [QUIET] * 15
        self.run_blocks(levels, wake_at=9)
        self.assertEqual(len(self.sent), 1)

    def test_nothing_sent_without_wake_word(self):
        self.run_blocks([LOUD] * 20 + [QUIET] * 20, wake_at=999)
        self.assertEqual((self.wakes, self.sent), ([], []))


if __name__ == "__main__":
    unittest.main()
