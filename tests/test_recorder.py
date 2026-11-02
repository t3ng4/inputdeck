"""Exercises recorder.py against a fake python-evdev (real fds via pipes, so the
selector loop is the real code path)."""
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- fake evdev ------------------------------------------------------------
from inputdeck import core  # noqa: E402

ecodes = types.SimpleNamespace(
    EV_KEY=1, EV_REL=2, EV_ABS=3, KEY_A=30, KEY_ENTER=28, BTN_LEFT=272,
    keys=dict(core._FALLBACK_NAMES), ecodes={v: k for k, v in core._FALLBACK_NAMES.items()})

DEVICES = {}


class Ev:
    def __init__(self, code, value, t=0.0, type_=1):
        self.type, self.code, self.value, self._t = type_, code, value, t

    def timestamp(self):
        return self._t


class FakeDevice:
    def __init__(self, path):
        spec = DEVICES.get(path)
        if spec is None:
            raise FileNotFoundError(path)
        if spec.get("denied"):
            raise PermissionError(13, "Permission denied")
        self.path, self.name, self._caps = path, spec["name"], spec["caps"]
        self.info = types.SimpleNamespace(bustype=3, vendor=spec.get("vendor", 1), product=spec.get("product", 1))
        self.uniq = spec.get("uniq", "")
        self.phys = spec.get("phys", "usb-0000:00:14.0-%d/input0" % spec.get("vendor", 1))
        self.queue, self.grabbed = [], False
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)
        spec["instance"] = self

    # the real evdev.InputDevice defines __eq__ and is therefore unhashable: keep the fake honest
    def __eq__(self, other):
        return isinstance(other, FakeDevice) and self.path == other.path

    __hash__ = None

    def fileno(self):
        return self.r

    def capabilities(self, absinfo=False):
        return self._caps

    def push(self, *events):
        self.queue.extend(events)
        os.write(self.w, b"x")

    def read(self):
        try:
            os.read(self.r, 1024)
        except BlockingIOError:
            pass
        if not self.queue:
            raise BlockingIOError
        items, self.queue = self.queue, []
        return iter(items)

    def grab(self):
        if DEVICES[self.path].get("busy"):
            raise OSError(16, "Device or resource busy")
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False

    def close(self):
        for fd in (self.r, self.w):
            try:
                os.close(fd)
            except OSError:
                pass


fake = types.ModuleType("evdev")
fake.ecodes = ecodes
fake.InputDevice = FakeDevice
fake.list_devices = lambda: sorted(DEVICES)
sys.modules["evdev"] = fake

from inputdeck import recorder  # noqa: E402

KBD_CAPS = {1: [30, 28, 29, 42, 56, 57, 1, 2, 3, 4, 5, 272][:11]}
MOUSE_CAPS = {1: [272, 273, 274, 275], 2: [0, 1]}


def reset():
    DEVICES.clear()
    DEVICES["/dev/input/event1"] = {"name": "Test Keyboard", "caps": {1: list(range(1, 40))}, "vendor": 10}
    DEVICES["/dev/input/event7"] = {"name": "Test Keyboard Consumer Control", "caps": {1: [113, 114, 115]},
                                    "vendor": 10, "phys": "usb-0000:00:14.0-10/input1"}
    DEVICES["/dev/input/event2"] = {"name": "Test Mouse", "caps": MOUSE_CAPS, "vendor": 20}
    DEVICES["/dev/input/event3"] = {"name": "input-remapper keyboard", "caps": {1: list(range(1, 30))}, "vendor": 99}
    DEVICES["/dev/input/event4"] = {"name": "Power Button", "caps": {1: [116]}, "vendor": 98}
    DEVICES["/dev/input/event5"] = {"name": "Test Mouse", "caps": {1: [272], 2: [0, 1]}, "vendor": 20,
                                    "phys": "usb-0000:00:14.0-20/input1"}
    DEVICES["/dev/input/event9"] = {"name": "WH-1000XM3 (AVRCP)", "vendor": 30,
                                    "caps": {1: [113, 114, 115, 163, 164, 165, 200, 201, 207, 208]}}
    DEVICES["/dev/input/event6"] = {"name": "Locked", "caps": {1: [1]}, "denied": True, "vendor": 97}


def wait_for(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class DiscoveryTests(unittest.TestCase):
    def test_grouping_follows_input_remapper(self):
        reset()
        devices, denied = recorder.list_input_devices()
        self.assertEqual([d.name for d in devices], ["Test Keyboard", "Test Mouse", "WH-1000XM3 (AVRCP)"])
        kbd, mouse, headset = devices
        self.assertEqual(headset.kinds, {"media"})
        self.assertEqual(headset.kind_label, "media buttons")
        # the Consumer Control node belongs to the keyboard group, named after the shortest name
        self.assertEqual(sorted(kbd.all_paths), ["/dev/input/event1", "/dev/input/event7"])
        self.assertEqual(sorted(kbd.paths), ["/dev/input/event1", "/dev/input/event7"])
        self.assertEqual(kbd.kinds, {"keyboard"})
        self.assertEqual(mouse.kinds, {"mouse"})
        self.assertEqual(sorted(mouse.paths), ["/dev/input/event2", "/dev/input/event5"])
        self.assertEqual(denied, 1)

    def test_duplicate_names_get_numbered(self):
        reset()
        DEVICES["/dev/input/event8"] = {"name": "Test Mouse", "caps": MOUSE_CAPS, "vendor": 21}
        devices, _d = recorder.list_input_devices()
        self.assertEqual([d.name for d in devices],
                         ["Test Keyboard", "Test Mouse", "Test Mouse 2", "WH-1000XM3 (AVRCP)"])

    def test_unique_key_recipe(self):
        reset()
        dev = FakeDevice("/dev/input/event1")
        self.assertEqual(recorder.unique_key(dev), "3_10_1__usb-0000:00:14.0-10")
        dev.close()

    def test_live_nodes_cover_whole_group(self):
        reset()
        nodes = recorder.live_nodes("Test Mouse")
        self.assertEqual(len(nodes), 2)
        self.assertIn((1, 272), nodes[0]["codes"])
        self.assertEqual(len(recorder.live_nodes("Test Keyboard")), 2)
        self.assertEqual(recorder.live_nodes("Nope"), [])


class ChordTests(unittest.TestCase):
    def run_rec(self, paths, grab=True, timeout=3.0):
        out = {}
        done = threading.Event()

        def on_done(result, msg):
            out["r"], out["m"] = result, msg
            done.set()
        rec = recorder.ChordRecorder(paths, on_done, grab=grab, timeout=timeout)
        rec.start()
        return rec, out, done

    def test_chord_in_press_order_and_grab_released(self):
        reset()
        rec, out, done = self.run_rec(["/dev/input/event1"])
        self.assertTrue(wait_for(lambda: DEVICES["/dev/input/event1"].get("instance")
                                 and DEVICES["/dev/input/event1"]["instance"].grabbed))
        dev = DEVICES["/dev/input/event1"]["instance"]
        dev.push(Ev(29, 1), Ev(30, 1), Ev(30, 0))
        time.sleep(0.15)
        self.assertFalse(done.is_set())                    # Ctrl still down
        dev.push(Ev(29, 0))
        self.assertTrue(done.wait(2))
        self.assertEqual([c for c, _h in out["r"]], [29, 30])
        self.assertEqual(out["r"][0][1], core.origin_hash(dev._caps, "Test Keyboard"))
        self.assertFalse(dev.grabbed)

    def test_cancel_and_timeout_and_busy(self):
        reset()
        rec, out, done = self.run_rec(["/dev/input/event1"])
        time.sleep(0.2)
        rec.cancel()
        self.assertTrue(done.wait(2))
        self.assertIsNone(out["r"])
        self.assertEqual(out["m"], "cancelled")

        reset()
        rec, out, done = self.run_rec(["/dev/input/event1"], timeout=0.3)
        self.assertTrue(done.wait(2))
        self.assertIn("timed out", out["m"])

        reset()
        DEVICES["/dev/input/event1"]["busy"] = True
        rec, out, done = self.run_rec(["/dev/input/event1"])
        self.assertTrue(done.wait(2))
        self.assertIsNone(out["r"])
        self.assertIn("grab", out["m"])


class MacroRecorderTests(unittest.TestCase):
    def test_records_from_two_devices(self):
        reset()
        seen = []
        rec = recorder.MacroRecorder(["/dev/input/event1", "/dev/input/event2"], on_event=seen.append)
        rec.start()
        self.assertTrue(rec.started_ok.wait(2))
        DEVICES["/dev/input/event1"]["instance"].push(Ev(30, 1, 100.0), Ev(30, 0, 100.05),
                                                     Ev(0, 0, 100.06, type_=0))   # SYN ignored
        DEVICES["/dev/input/event2"]["instance"].push(Ev(272, 1, 101.0), Ev(272, 0, 101.1))
        self.assertTrue(wait_for(lambda: len(rec.events) >= 4))
        events = rec.stop()
        self.assertEqual([(n, v) for _t, n, v in events],
                         [("KEY_A", 1), ("KEY_A", 0), ("BTN_LEFT", 1), ("BTN_LEFT", 0)])
        steps = core.events_to_steps(events)
        self.assertEqual(core.steps_to_text(steps), "key(KEY_A).wait(950).key(BTN_LEFT)")
        self.assertEqual(len(seen), 4)
        self.assertFalse(DEVICES["/dev/input/event1"]["instance"].grabbed)

    def test_error_when_nothing_opens(self):
        reset()
        errors = []
        rec = recorder.MacroRecorder(["/dev/input/event6"], on_error=errors.append)
        rec.start()
        rec.join(2)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
