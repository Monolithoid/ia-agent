"""Tests sans ROS 2 du format des événements /events (régie).

    cd robotics/brain/body_gateway && python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events  # noqa: E402


class TestEvents(unittest.TestCase):

    def test_common_fields(self):
        event = events.make("input", "ab-1", source="webui", text="salut")
        self.assertEqual(event["v"], events.SCHEMA)
        self.assertEqual(event["kind"], "input")
        self.assertEqual(event["id"], "ab-1")
        self.assertEqual(event["source"], "webui")
        self.assertIsInstance(event["t"], float)

    def test_encode_keeps_accents(self):
        raw = events.encode(events.make("input", text="ça va ?"))
        self.assertIn("ça va", raw)
        self.assertEqual(json.loads(raw)["text"], "ça va ?")

    def test_ids_are_unique(self):
        self.assertNotEqual(events.new_id(), events.new_id())

    def test_chat_origin(self):
        self.assertEqual(events.chat_origin("[tchat Twitch] bob : salut nao"), ("twitch", "bob"))
        self.assertEqual(events.chat_origin("Bonjour NAO"), ("webui", ""))
        self.assertEqual(events.chat_origin("[tchat Twitch] sans auteur"), ("twitch", ""))

    def test_action_detail(self):
        self.assertEqual(events.action_detail({"type": "speech", "text": "coucou"}), "coucou")
        self.assertEqual(events.action_detail({"type": "motion", "name": "wave"}), "wave")
        self.assertEqual(events.action_detail({"type": "motion", "joints": ["HeadYaw", "HeadPitch"],
                                               "angles": [0.1, -0.2]}),
                         "HeadYaw 0.1 HeadPitch -0.2")
        self.assertEqual(
            events.action_detail({"type": "motion", "joint": "HeadYaw", "angle": 0.1}),
            "HeadYaw 0.1",
        )
        self.assertEqual(
            events.action_detail({"type": "motion", "joint": "HeadPitch", "angle": 0.1, "auto": True}),
            "HeadPitch 0.1 (auto)",
        )
        self.assertEqual(events.action_detail({"type": "leds", "color": "blue"}), "blue")
        self.assertEqual(events.action_detail({"type": "look", "yaw": 30}), "yaw=30")
        self.assertEqual(events.action_detail({"type": "inconnu"}), "")


if __name__ == "__main__":
    unittest.main()
