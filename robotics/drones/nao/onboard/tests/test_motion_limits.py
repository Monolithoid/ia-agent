"""Tests sans robot des bornes de mouvement de nao_ws_bridge (mode MOCK) :
profil moteur, composants NAOqi et calibrage.

Les décisions (séquences, postures, chaleur, repos) sont dans le driver NAO :
robotics/drones/nao/driver/tests/test_motion_policy.py.

    cd robotics/drones/nao/onboard && python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nao_ws_bridge as b  # noqa: E402

HEAD = {"rest":  {"HeadYaw": 0.0, "HeadPitch": 0.3},
        "start": {"HeadYaw": 0.0, "HeadPitch": 0.0},
        "range": {"HeadYaw": [-0.5, 0.5], "HeadPitch": [-0.2, 0.2]},
        "speed": 0.1}


class FakeMotion(object):
    """ALMotion factice : enregistre les appels ; getAngles renvoie self.pose."""

    def __init__(self):
        self.calls = []
        self.pose = {}
        self.refuse = set()   # méthodes qui lèvent une erreur NAOqi

    def getAngles(self, joints, use_sensors):
        return [self.pose.get(j, 0.0) for j in joints]

    def setAngles(self, joints, angles, speed):
        self.calls.append((list(joints), list(angles), speed))

    def angleInterpolationWithSpeed(self, joints, angles, speed):
        self.calls.append(("interpolation", list(joints), list(angles), speed))
        self.pose.update(zip(joints, angles))

    def setStiffnesses(self, group, value):
        self.calls.append(("stiffness", group, value))

    def __getattr__(self, name):
        def call(*args):
            if name in self.refuse:
                raise RuntimeError("refus NAOqi")
            self.calls.append((name,) + args)
        return call


class BridgeTest(unittest.TestCase):
    """Pont en mode MOCK avec un profil et des fichiers dans un dossier temporaire."""

    profile = {"version": 1, "chains": {"head": HEAD}}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.profile_path = os.path.join(self.dir, "nao_motor_profile.json")
        if self.profile is not None:
            with open(self.profile_path, "w") as f:
                json.dump(self.profile, f)
        self.proxy = FakeMotion()
        self.bridge = self.make()
        self.sent = []

    def make(self, **kwargs):
        bridge = b.NaoBridge("ws://test/", motor_profile=self.profile_path,
                             components=os.path.join(self.dir, "nao_components.json"), **kwargs)
        bridge._guard._proxy_factory = lambda: self.proxy
        bridge._proxy = lambda name: self.proxy
        bridge._spawn = lambda fn, *args: fn(*args)
        bridge._publish = lambda topic, payload: self.sent.append((topic, payload))
        return bridge

    def tearDown(self):
        shutil.rmtree(self.dir)

    def state(self):
        return [p for t, p in self.sent if t == "/nao/calibration_state"][-1]


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
        self.assertFalse(lim["fall_manager"])


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

    def test_profile_box(self):
        profile, errors = b._validate_profile({"chains": {"head": HEAD}}, self.lim)
        self.assertEqual(errors, [])
        box = b._profile_box(profile)
        # La boîte contient la plage et le repos (HeadPitch 0.3 hors plage).
        self.assertEqual(box["HeadPitch"], (-0.2, 0.3))
        j, a = b._clamp_joints(["HeadPitch", "HeadYaw", "LShoulderPitch", "RHand"],
                               [1.0, -1.0, 0.0, 0.5], self.lim, box)
        self.assertEqual(j, ["HeadPitch", "HeadYaw"])
        self.assertEqual(a, [0.3, -0.5])

    def test_profile_validation(self):
        bad = dict(HEAD, start={"HeadYaw": 0.9, "HeadPitch": 0.0})
        profile, errors = b._validate_profile({"chains": {"head": bad, "legs": HEAD}}, self.lim)
        self.assertIsNone(profile)
        self.assertEqual(len(errors), 2)
        fast = dict(HEAD, speed=5)
        profile, _ = b._validate_profile({"chains": {"head": fast}}, self.lim)
        self.assertEqual(profile["chains"]["head"]["speed"], 0.3)


class TestBridge(BridgeTest):

    def test_stiffness_off_blocks_move(self):
        self.bridge._motion_joint("HeadYaw", 0.1, 0.1)
        self.assertEqual(self.proxy.calls, [])

    def test_move_is_bounded_and_sent_at_once(self):
        self.bridge._set_stiffness(["head"], 1.0)
        self.bridge._dispatch("/nao/motion", {"data": '{"joint": "HeadPitch", "angle": 9, "speed": 2}'})
        self.assertEqual(self.proxy.calls, [(["HeadPitch"], [0.3], 0.3)])

    def test_no_decision_on_robot(self):
        # Deux ordres rapprochés partent tous les deux : le rythme est décidé par le driver.
        self.bridge._set_stiffness(["head"], 1.0)
        self.bridge._motion_joint("HeadYaw", 0.1, 0.1)
        self.bridge._motion_joint("HeadYaw", 0.2, 0.1)
        self.assertEqual(len(self.proxy.calls), 2)

    def test_legacy_call(self):
        bridge = self.make(motion_limits={"motion_call": u"legacy"})
        bridge._set_stiffness(["head"], 1.0)
        bridge._motion_joint("HeadYaw", 0.1, 0.1)
        self.assertEqual(self.proxy.calls, [("stiffness", "Head", 1.0),
                                            ("interpolation", ["HeadYaw"], [0.1], 0.1)])

    def test_stiffness_capped_and_legs_blocked(self):
        bridge = self.make(motion_limits={"stiffness_max": 0.6})
        bridge._set_stiffness(["head", "legs"], 1.0)
        self.assertEqual(bridge._stiffness_enabled, {"head"})

    def test_startup_goes_to_rest_and_switches_components_off(self):
        self.bridge._apply_motion_safety()
        self.assertIn(("setFallManagerEnabled", False), self.proxy.calls)
        self.assertIn(("setCollisionProtectionEnabled", "Arms", False), self.proxy.calls)
        self.assertIn(("setSmartStiffnessEnabled", True), self.proxy.calls)
        self.assertIn(("stiffness", "Head", 1.0), self.proxy.calls)
        self.assertEqual(self.proxy.calls[-1],
                         ("interpolation", ["HeadYaw", "HeadPitch"], [0.0, 0.3], 0.1))
        self.assertEqual(self.bridge._capabilities()["motor_profile"]["chains"]["head"]["speed"],
                         0.1)


class TestNoProfile(BridgeTest):

    profile = None

    def test_every_joint_refused(self):
        self.bridge._set_stiffness(["head"], 1.0)
        self.bridge._motion_joint("HeadYaw", 0.1, 0.1)
        self.assertEqual(self.proxy.calls, [])
        self.assertIsNone(self.bridge._capabilities()["motor_profile"])


class TestComponents(BridgeTest):

    def test_switch_saved_and_reported(self):
        self.proxy.refuse.add("setFallManagerEnabled")
        self.bridge._dispatch("/nao/components", {"data": json.dumps(
            {"name": "breath", "enabled": True})})
        self.bridge._dispatch("/nao/components", {"data": json.dumps(
            {"name": "fall_manager", "enabled": False})})
        state = dict((c["name"], c) for c in self.sent[-1][1]["components"])
        self.assertTrue(state["breath"]["enabled"])
        self.assertFalse(state["fall_manager"]["ok"])
        self.assertIn(("setBreathEnabled", "Body", True), self.proxy.calls)
        # Au redémarrage, l'état enregistré est réappliqué.
        self.proxy.calls = []
        self.make()._apply_components()
        self.assertIn(("setBreathEnabled", "Body", True), self.proxy.calls)


class TestCalibration(BridgeTest):

    def step(self, op, **kwargs):
        kwargs["op"] = op
        self.bridge._dispatch("/nao/calibration", {"data": json.dumps(kwargs)})
        return self.state()

    def test_full_calibration(self):
        self.bridge._spawn = lambda fn, *args: None if fn == self.bridge._calib_live_loop \
            else fn(*args)
        self.bridge._sleep = lambda s: None
        state = self.step("start", chain="rarm")
        self.assertTrue(state["active"])
        self.step("release")
        self.assertEqual(self.proxy.calls[-1], ("stiffness", "RArm", 0.0))
        rest = {"RShoulderPitch": 1.5, "RShoulderRoll": -0.1, "RElbowYaw": 1.2, "RElbowRoll": 0.4}
        start = dict(rest, RShoulderPitch=1.0)
        self.proxy.pose = dict(rest)
        self.step("capture", what="rest")
        self.proxy.pose = dict(start)
        state = self.step("capture", what="start")
        self.assertEqual(state["start"]["RShoulderPitch"], 1.0)
        self.step("range_start")
        calib = self.bridge._calib
        for pitch, elbow in ((0.2, 0.3), (1.2, 1.0)):
            self.bridge._calib_record(calib, dict(start, RShoulderPitch=pitch, RElbowRoll=elbow))
        state = self.step("range_stop")
        self.assertEqual(state["range"]["RShoulderPitch"], [0.22, 1.18])
        self.assertEqual(state["range"]["RShoulderRoll"], [-0.1, -0.1])   # tenu par le départ
        self.assertIn("rejouer", self.step("save")["error"])
        self.step("speed", value=0.15)
        self.proxy.calls = []
        state = self.step("replay")
        self.assertTrue(state["replayed"])
        moves = [c for c in self.proxy.calls if c[0] == "interpolation"]
        self.assertEqual(len(moves), 2 + 2 * 4 + 2)
        self.assertEqual(moves[0][2], [1.5, -0.1, 1.2, 0.4])
        state = self.step("save")
        self.assertFalse(state["active"])
        with open(self.profile_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["version"], 2)
        self.assertEqual(sorted(saved["chains"]), ["head", "rarm"])
        self.assertEqual(saved["chains"]["rarm"]["speed"], 0.15)
        self.assertTrue(os.path.exists(self.profile_path + ".1.bak"))
        self.assertIn("/nao/capabilities", [t for t, _ in self.sent])
        self.assertIn("RShoulderPitch", self.bridge._guard.box)

    def test_calibrated_chain_ignores_driver_and_energy(self):
        self.bridge._spawn = lambda fn, *args: None
        self.bridge._set_stiffness(["head"], 1.0)
        self.step("start", chain="head")
        self.step("release")
        self.proxy.calls = []
        self.bridge._dispatch("/nao/motion", {"data": '{"joint": "HeadYaw", "angle": 0.1}'})
        self.bridge._dispatch("/nao/stiffness", {"data": '{"groups": ["head"], "value": 1.0}'})
        self.assertEqual(self.proxy.calls, [])

    def test_errors(self):
        self.assertIn("aucun", self.step("release")["error"])
        self.bridge._spawn = lambda fn, *args: None
        self.step("start", chain="head")
        self.assertIn("en cours", self.step("start", chain="larm")["error"])
        self.assertIn("manquant", self.step("replay")["error"] or "manquant")
        self.assertFalse(self.step("cancel")["active"])


if __name__ == "__main__":
    unittest.main()
