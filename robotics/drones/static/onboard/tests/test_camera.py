"""Tests sans matériel du choix de la caméra (CAMERA_INDEX).

    cd robotics/drones/static/onboard && python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import static_drone  # noqa: E402


class TestFindCamera(unittest.TestCase):

    def setUp(self):
        # Faux /sys/class/video4linux d'un PC portable : caméra intégrée
        # (video0, video1) + webcam USB (video2, video3).
        self.tmp = tempfile.TemporaryDirectory()
        self.sysfs = self.tmp.name
        names = {0: "Integrated Camera: Integrated C", 1: "Integrated Camera: Integrated C",
                 2: "HD Pro Webcam C920", 3: "HD Pro Webcam C920"}
        for i, name in names.items():
            os.makedirs(os.path.join(self.sysfs, "video{}".format(i)))
            with open(os.path.join(self.sysfs, "video{}".format(i), "name"), "w") as f:
                f.write(name + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def find(self, wanted):
        return static_drone.find_camera(wanted, self.sysfs)

    def test_index(self):
        self.assertEqual(self.find("2"), 2)
        self.assertEqual(self.find(""), 0)
        self.assertEqual(self.find(None), 0)

    def test_name(self):
        self.assertEqual(self.find("c920"), 2)
        self.assertEqual(self.find("Integrated"), 0)
        self.assertIsNone(self.find("absente"))

    def test_path(self):
        self.assertEqual(self.find("/dev/video2"), 2)
        link = os.path.join(self.sysfs, "usb-Logitech_C920-video-index0")
        os.symlink("/dev/video3", link)
        self.assertEqual(self.find(link), 3)

    def test_camera_name(self):
        self.assertEqual(static_drone.camera_name(2, self.sysfs), "HD Pro Webcam C920")
        self.assertIsNone(static_drone.camera_name(9, self.sysfs))


class TestSampleRate(unittest.TestCase):

    class FakeSd:
        def __init__(self, accepted):
            self.accepted = accepted

        def check_input_settings(self, device, channels, samplerate, dtype):
            if samplerate not in self.accepted:
                raise ValueError("Invalid sample rate")

    def test_wanted_accepted(self):
        info = {"name": "default", "default_samplerate": 44100.0}
        self.assertEqual(static_drone.choose_sample_rate(
            self.FakeSd({16000, 48000}), None, 1, 16000, info), 16000)

    def test_fallback(self):
        info = {"name": "USB Mic: Audio (hw:2,0)", "default_samplerate": 48000.0}
        self.assertEqual(static_drone.choose_sample_rate(
            self.FakeSd({48000}), 3, 1, 16000, info), 48000)


if __name__ == "__main__":
    unittest.main()
