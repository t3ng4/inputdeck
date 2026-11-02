"""Built-in engine tests: fake evdev nodes in, fake uinput devices out."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_recorder as tr  # noqa: E402  (installs the fake evdev first)
from inputdeck import core, engine  # noqa: E402

Ev, DEVICES, wait_for = tr.Ev, tr.DEVICES, tr.wait_for
KEY_A, KEY_B, KEY_C, KEY_F5, KEY_Q = 30, 48, 46, 63, 16
CTRL, BTN_LEFT, BTN_SIDE, BTN_EXTRA = 29, 272, 275, 276


class FakeUInput:
    def __init__(self, log, name):
        self.log, self.name, self.closed = log, name, False

    def write(self, t, c, v):
        self.log.append((self.name, t, c, v))

    def write_event(self, e):
        self.write(e.type, e.code, e.value)

    def syn(self):
        pass

    def close(self):
        self.closed = True


class Rig:
    """Collects everything written to virtual devices."""

    def __init__(self):
        self.log = []
        self.messages = []
        self.created = []
        self.fail_clone = False

    def make_uinput(self, caps, name):
        self.created.append(name.split(" (")[0].replace("InputDeck ", ""))
        return FakeUInput(self.log, "out:" + name.split(" (")[0].replace("InputDeck ", ""))

    def make_clone(self, dev):
        if self.fail_clone:
            raise TypeError("simulated failure while cloning")
        return FakeUInput(self.log, "clone:" + dev.path)

    def events(self, prefix):
        return [(c, v) for n, t, c, v in self.log if n.startswith(prefix) and t == 1]


def mapping(combo, output, sleep_ms=1.0):
    plan = engine.plan_output(output)
    return engine.Mapping(list(combo), plan, sleep_ms, "test")


class PlanTests(unittest.TestCase):
    def test_outputs(self):
        self.assertEqual(engine.plan_output("KEY_F5").codes, [KEY_F5])
        self.assertEqual(engine.plan_output("hold_keys(KEY_LEFTCTRL, KEY_C)").codes, [CTRL, KEY_C])
        notes = []
        self.assertEqual(engine.plan_output("Control_L + a", notes).codes, [CTRL, KEY_A])
        self.assertEqual(len(notes), 1)                                   # "a" was a QWERTY guess
        self.assertEqual(engine.plan_output("disable").kind, "disable")
        p = engine.plan_output("hold(key_down(BTN_LEFT).wait(50).key_up(BTN_LEFT).wait(50))")
        self.assertEqual((p.kind, p.mode, [c.name for c in p.chain]),
                         ("macro", "hold", ["key_down", "wait", "key_up", "wait"]))
        self.assertEqual(engine.plan_output("toggle(key(KEY_A))").mode, "toggle")
        self.assertEqual(engine.plan_output("repeat(3, key(KEY_A)).wait(10)").mode, "once")

    def test_errors_are_reported_not_raised_later(self):
        for bad in ("modify(KEY_A, key(KEY_B))", "key(", "key(NOPE_KEY)", "repeat(2)", "wait(abc)",
                    "key(KEY_A) key(KEY_B)", "KEY_A + nonsense+"):
            with self.assertRaises(engine.MacroError, msg=bad):
                engine.plan_output(bad)

    def test_compile_preset_skips_unsupported(self):
        data = [
            {"input_combination": [{"type": 1, "code": BTN_SIDE}], "output_symbol": "KEY_F5"},
            {"input_combination": [{"type": 3, "code": 1}], "output_type": 2, "output_code": 1},
            {"input_combination": [{"type": 1, "code": 31}], "output_symbol": "if_single(key(a), key(b))"},
            {"input_combination": [{"type": 1, "code": 32}], "output_symbol": "p", "macro_key_sleep_ms": 25},
        ]
        mappings, warnings = engine.compile_preset(data)
        self.assertEqual([m.combo for m in mappings], [[BTN_SIDE], [32]])
        self.assertEqual(mappings[1].sleep_ms, 25.0)
        self.assertEqual(len(warnings), 3)          # axis input, if_single, and the "p" guess note


class MacroRunTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.out = engine.Output(self.rig.make_uinput, "dev")

    def run_plan(self, text, sleep=1.0):
        run = engine.MacroRun(engine.plan_output(text), self.out, sleep, print)
        run.start()
        return run

    def test_sequence_order(self):
        run = self.run_plan("key(KEY_A).wait(5).key(KEY_B)")
        run.join(2)
        self.assertEqual(self.rig.events("out:keyboard"), [(KEY_A, 1), (KEY_A, 0), (KEY_B, 1), (KEY_B, 0)])

    def test_mouse_buttons_go_to_the_mouse_device(self):
        self.run_plan("key(BTN_LEFT).key(KEY_A)").join(2)
        self.assertEqual(self.rig.events("out:mouse"), [(BTN_LEFT, 1), (BTN_LEFT, 0)])
        self.assertEqual(self.rig.events("out:keyboard"), [(KEY_A, 1), (KEY_A, 0)])

    def test_hold_repeats_until_released_then_nothing_is_stuck(self):
        run = self.run_plan("hold(key_down(KEY_A).wait(2).key_up(KEY_A))")
        self.assertTrue(wait_for(lambda: len([e for e in self.rig.events("out:keyboard") if e == (KEY_A, 1)]) >= 3))
        run.released.set()
        run.join(2)
        self.assertFalse(run.is_alive())
        self.assertEqual(self.out.down, set())

    def test_stop_mid_macro_releases_held_keys(self):
        run = self.run_plan("key_down(KEY_A).wait(60000).key_up(KEY_A)")
        self.assertTrue(wait_for(lambda: KEY_A in self.out.down))
        run.stop_ev.set()
        run.join(2)
        self.assertFalse(run.is_alive())
        self.assertEqual(self.out.down, set())

    def test_hold_keys_waits_for_release(self):
        run = self.run_plan("key(KEY_Q).hold_keys(KEY_LEFTCTRL, KEY_A)")
        self.assertTrue(wait_for(lambda: {CTRL, KEY_A} <= self.out.down))
        run.released.set()
        run.join(2)
        self.assertEqual(self.out.down, set())
        self.assertEqual(self.rig.events("out:keyboard")[-2:], [(KEY_A, 0), (CTRL, 0)])   # reverse order

    def test_repeat(self):
        self.run_plan("repeat(3, key(KEY_A))").join(2)
        self.assertEqual(len([e for e in self.rig.events("out:keyboard") if e == (KEY_A, 1)]), 3)


class InjectorTests(unittest.TestCase):
    def setUp(self):
        tr.reset()
        self.rig = Rig()
        self.node = tr.FakeDevice("/dev/input/event2")

    def tearDown(self):
        pass

    def inj(self, *mappings):
        inj = engine.Injector("Dev", [self.node], list(mappings), self.rig.make_uinput,
                              self.rig.make_clone, print)
        inj.start()
        self.addCleanup(inj.stop)
        return inj

    def clone_events(self):
        return self.rig.events("clone:")

    def test_swallows_trigger_and_forwards_everything_else(self):
        inj = self.inj(mapping([BTN_SIDE], "KEY_F5"))
        self.assertTrue(self.node.grabbed)
        inj.handle(self.node, Ev(BTN_SIDE, 1))
        inj.handle(self.node, Ev(KEY_Q, 1))
        inj.handle(self.node, Ev(KEY_Q, 0))
        inj.handle(self.node, Ev(1, 5, type_=2))                       # REL event passes through
        inj.handle(self.node, Ev(BTN_SIDE, 0))
        self.assertEqual(self.clone_events(), [(KEY_Q, 1), (KEY_Q, 0)])
        self.assertEqual([(t, c) for n, t, c, v in self.rig.log if n.startswith("clone:") and t == 2], [(2, 1)])
        self.assertEqual(self.rig.events("out:keyboard"), [(KEY_F5, 1), (KEY_F5, 0)])

    def test_combination_releases_modifier_for_the_system(self):
        inj = self.inj(mapping([CTRL, KEY_C], "KEY_F5"))
        inj.handle(self.node, Ev(KEY_C, 1))                            # C alone: not a trigger
        inj.handle(self.node, Ev(KEY_C, 0))
        self.assertEqual(self.clone_events(), [(KEY_C, 1), (KEY_C, 0)])
        self.rig.log.clear()
        inj.handle(self.node, Ev(CTRL, 1))
        inj.handle(self.node, Ev(KEY_C, 1))                            # trigger
        inj.handle(self.node, Ev(KEY_C, 0))
        inj.handle(self.node, Ev(CTRL, 0))                             # physical release is swallowed
        self.assertEqual(self.clone_events(), [(CTRL, 1), (CTRL, 0)])  # system saw Ctrl pressed+released, no C
        self.assertEqual(self.rig.events("out:keyboard"), [(KEY_F5, 1), (KEY_F5, 0)])

    def test_longest_combination_wins(self):
        inj = self.inj(mapping([KEY_C], "KEY_A"), mapping([CTRL, KEY_C], "KEY_B"))
        inj.handle(self.node, Ev(CTRL, 1))
        inj.handle(self.node, Ev(KEY_C, 1))
        self.assertEqual(self.rig.events("out:keyboard"), [(KEY_B, 1)])

    def test_disable_swallows_the_key(self):
        inj = self.inj(mapping([BTN_EXTRA], "disable"))
        inj.handle(self.node, Ev(BTN_EXTRA, 1))
        inj.handle(self.node, Ev(BTN_EXTRA, 0))
        self.assertEqual(self.rig.log, [])

    def test_kernel_autorepeat_is_dropped(self):
        inj = self.inj(mapping([BTN_SIDE], "KEY_F5"))
        inj.handle(self.node, Ev(KEY_Q, 1))
        inj.handle(self.node, Ev(KEY_Q, 2))
        inj.handle(self.node, Ev(BTN_SIDE, 1))
        inj.handle(self.node, Ev(BTN_SIDE, 2))
        self.assertEqual(self.clone_events(), [(KEY_Q, 1)])

    def test_output_devices_exist_before_the_first_key(self):
        self.inj(mapping([BTN_SIDE], "KEY_F5"))
        self.assertEqual(self.rig.created, ["keyboard"])                  # no mouse needed
        rig2, node2 = Rig(), tr.FakeDevice("/dev/input/event5")
        inj2 = engine.Injector("D2", [node2], [mapping([BTN_LEFT], "hold(key(BTN_RIGHT).key(KEY_A))")],
                               rig2.make_uinput, rig2.make_clone, print)
        inj2.start()
        self.addCleanup(inj2.stop)
        self.assertEqual(sorted(rig2.created), ["keyboard", "mouse"])

    def test_any_startup_failure_releases_the_device(self):
        self.rig.fail_clone = True
        inj = engine.Injector("Dev", [self.node], [mapping([BTN_SIDE], "KEY_F5")], self.rig.make_uinput,
                              self.rig.make_clone, print)
        with self.assertRaises(TypeError):
            inj.start()
        self.assertFalse(self.node.grabbed)                               # not left grabbed

    def test_stop_ungrabs_and_releases_keys_the_system_thinks_are_down(self):
        inj = self.inj(mapping([BTN_SIDE], "KEY_F5"))
        inj.handle(self.node, Ev(KEY_Q, 1))
        inj.handle(self.node, Ev(BTN_SIDE, 1))
        inj.stop()
        self.assertFalse(self.node.grabbed)
        self.assertEqual(self.clone_events()[-1], (KEY_Q, 0))          # no stuck Q
        self.assertEqual(self.rig.events("out:keyboard")[-1], (KEY_F5, 0))   # no stuck F5

    def test_hold_macro_runs_while_pressed(self):
        inj = self.inj(mapping([BTN_SIDE], "hold(key(KEY_A))"))
        inj.handle(self.node, Ev(BTN_SIDE, 1))
        self.assertTrue(wait_for(lambda: len([e for e in self.rig.events("out:keyboard") if e == (KEY_A, 1)]) >= 3))
        inj.handle(self.node, Ev(BTN_SIDE, 0))
        run = next(iter(inj.runs.values()))
        run.join(2)
        self.assertFalse(run.is_alive())
        self.assertEqual(inj.out.down, set())

    def test_toggle_macro_starts_and_stops_on_press(self):
        inj = self.inj(mapping([BTN_SIDE], "toggle(key(KEY_A))"))
        inj.handle(self.node, Ev(BTN_SIDE, 1))
        inj.handle(self.node, Ev(BTN_SIDE, 0))
        run = next(iter(inj.runs.values()))
        time.sleep(0.05)
        self.assertTrue(run.is_alive())                                # release does not stop a toggle
        inj.handle(self.node, Ev(BTN_SIDE, 1))
        run.join(2)
        self.assertFalse(run.is_alive())

    def test_plain_macro_ignores_retrigger_while_running(self):
        inj = self.inj(mapping([BTN_SIDE], "key_down(KEY_A).wait(200).key_up(KEY_A)"))
        inj.handle(self.node, Ev(BTN_SIDE, 1))
        inj.handle(self.node, Ev(BTN_SIDE, 0))
        inj.handle(self.node, Ev(BTN_SIDE, 1))
        inj.handle(self.node, Ev(BTN_SIDE, 0))
        run = next(iter(inj.runs.values()))
        run.join(2)
        self.assertEqual(len([e for e in self.rig.events("out:keyboard") if e == (KEY_A, 1)]), 1)

    def test_failed_grab_cleans_up(self):
        DEVICES["/dev/input/event2"]["busy"] = True
        inj = engine.Injector("Dev", [self.node], [mapping([BTN_SIDE], "KEY_F5")], self.rig.make_uinput,
                              self.rig.make_clone, print)
        with self.assertRaises(OSError):
            inj.start()
        self.assertFalse(self.node.grabbed)


class EngineTests(unittest.TestCase):
    def setUp(self):
        tr.reset()
        self.rig = Rig()
        self.tmp = Path(tempfile.mkdtemp(dir="/tmp"))
        self.paths = core.Paths(self.tmp / "cfg", self.tmp / "cfg", self.tmp / "data")
        self.store = core.PresetStore(self.paths)
        self.store.save("Test Mouse", "Default", [
            {"input_combination": [{"type": 1, "code": BTN_SIDE}], "target_uinput": "keyboard",
             "output_symbol": "KEY_F5"}])
        self.store.save("Test Keyboard", "Default", [
            {"input_combination": [{"type": 1, "code": KEY_A}], "target_uinput": "keyboard",
             "output_symbol": "KEY_B"}])
        self.logs = []
        self.engine = engine.Engine(self.tmp / "e.sock", self.logs.append, make_uinput=self.rig.make_uinput,
                                    make_clone=self.rig.make_clone, idle_exit=0, settle=0)

    def preset(self, dev):
        return str(self.store.file(dev, "Default"))

    def test_only_nodes_with_mapped_keys_are_grabbed(self):
        res = self.engine.start_device("Test Mouse", self.preset("Test Mouse"))
        self.assertTrue(res["ok"])
        self.assertTrue(DEVICES["/dev/input/event2"]["instance"].grabbed)       # has BTN_SIDE
        self.assertFalse(DEVICES["/dev/input/event5"]["instance"].grabbed)      # only BTN_LEFT: untouched

    def test_events_flow_through_the_pump(self):
        self.engine.start_device("Test Mouse", self.preset("Test Mouse"))
        node = DEVICES["/dev/input/event2"]["instance"]
        node.push(Ev(BTN_SIDE, 1), Ev(BTN_LEFT, 1), Ev(BTN_LEFT, 0), Ev(BTN_SIDE, 0))
        self.engine.pump(0.2)
        self.assertEqual(self.rig.events("out:keyboard"), [(KEY_F5, 1), (KEY_F5, 0)])
        self.assertEqual(self.rig.events("clone:"), [(BTN_LEFT, 1), (BTN_LEFT, 0)])

    def test_panic_chord_releases_everything(self):
        self.engine.start_device("Test Mouse", self.preset("Test Mouse"))
        self.engine.start_device("Test Keyboard", self.preset("Test Keyboard"))
        kbd = DEVICES["/dev/input/event1"]["instance"]
        self.assertTrue(kbd.grabbed)
        kbd.push(Ev(29, 1), Ev(56, 1), Ev(42, 1), Ev(1, 1))
        self.engine.pump(0.2)
        self.assertEqual(self.engine.injectors, {})
        self.assertFalse(kbd.grabbed)
        self.assertFalse(DEVICES["/dev/input/event2"]["instance"].grabbed)
        self.assertTrue(any("panic" in m for m in self.logs))

    def test_restart_replaces_and_stop_ungrabs(self):
        self.engine.start_device("Test Mouse", self.preset("Test Mouse"))
        first = DEVICES["/dev/input/event2"]["instance"]
        self.engine.start_device("Test Mouse", self.preset("Test Mouse"))
        self.assertFalse(first.grabbed)                                         # old instance released
        self.engine.stop_device("Test Mouse")
        self.assertFalse(DEVICES["/dev/input/event2"]["instance"].grabbed)

    def test_errors(self):
        with self.assertRaises(engine.EngineError):
            self.engine.start_device("Nope", self.preset("Test Mouse"))
        with self.assertRaises(engine.EngineError):
            self.engine.start_device("Test Mouse", str(self.tmp / "missing.json"))
        DEVICES["/dev/input/event2"]["busy"] = True
        with self.assertRaises(engine.EngineError) as cm:
            self.engine.start_device("Test Mouse", self.preset("Test Mouse"))
        self.assertIn("grab", str(cm.exception))
        self.assertEqual(self.engine.injectors, {})
        self.store.save("Test Mouse", "Empty", [
            {"input_combination": [{"type": 3, "code": 0}], "output_type": 2, "output_code": 0}])
        DEVICES["/dev/input/event2"]["busy"] = False
        res = self.engine.start_device("Test Mouse", str(self.store.file("Test Mouse", "Empty")))
        self.assertIn("left untouched", res["msg"])
        self.assertEqual(self.engine.injectors, {})

    def test_unexpected_startup_error_becomes_a_clean_message(self):
        self.rig.fail_clone = True
        with self.assertRaises(engine.EngineError) as cm:
            self.engine.start_device("Test Mouse", self.preset("Test Mouse"))
        self.assertIn("left untouched", str(cm.exception))
        self.assertFalse(DEVICES["/dev/input/event2"]["instance"].grabbed)
        self.assertEqual(self.engine.injectors, {})
        self.rig.fail_clone = False
        self.assertTrue(self.engine.start_device("Test Mouse", self.preset("Test Mouse"))["ok"])   # still usable

    def test_idle_exit(self):
        eng = engine.Engine(self.tmp / "i.sock", print, idle_exit=0.2)
        end = time.monotonic() + 3
        while not eng.quit and time.monotonic() < end:
            eng.pump(0.05)
        self.assertTrue(eng.quit)

    def test_socket_round_trip_with_the_client(self):
        thread = threading.Thread(target=self.engine.run, daemon=True)
        thread.start()
        self.assertTrue(wait_for(self.engine.sock_path.exists))
        ctl = engine.BuiltinControl(self.paths, self.engine.sock_path,
                                    spawn=lambda: self.fail("engine should already be running"))
        try:
            self.assertTrue(ctl.hello())
            self.assertEqual(ctl.start("Test Mouse", "Default")[0], 0)
            self.assertEqual(ctl.running, {"Test Mouse": "Default"})
            self.assertTrue(wait_for(lambda: "Test Mouse" in self.engine.injectors))
            code, msg = ctl.start("Nope", "Default")
            self.assertEqual(code, 1)
            self.assertIn("not found", msg)
            self.assertEqual(ctl.stop("Test Mouse")[0], 0)
            self.assertTrue(wait_for(lambda: not self.engine.injectors))
            ctl.start("Test Keyboard", "Default")
            ctl.stop_all()
            self.assertEqual(ctl.running, {})
            self.assertEqual(ctl.autoload(), (0, ""))
            self.assertTrue(oct(self.engine.sock_path.stat().st_mode & 0o777) == "0o600")
        finally:
            ctl.shutdown_engine()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.engine.sock_path.exists())

    def test_client_spawns_engine_when_missing_and_reports_failure(self):
        calls = []
        ctl = engine.BuiltinControl(self.paths, self.tmp / "none.sock", spawn=lambda: calls.append(1))
        ctl_start = time.monotonic()
        code, msg = ctl.start("Test Mouse", "Default")
        self.assertEqual(code, 1)
        self.assertEqual(calls, [1])
        self.assertIn("did not start", msg)
        self.assertLess(time.monotonic() - ctl_start, 8)
        self.assertEqual(ctl.stop_all(), (0, ""))                 # never spawns just to stop things

    def test_a_restarted_gui_can_ask_what_is_running(self):
        thread = threading.Thread(target=self.engine.run, daemon=True)
        thread.start()
        self.assertTrue(wait_for(self.engine.sock_path.exists))
        first = engine.BuiltinControl(self.paths, self.engine.sock_path, spawn=lambda: self.fail("no spawn"))
        try:
            first.start("Test Mouse", "Default")
            second = engine.BuiltinControl(self.paths, self.engine.sock_path,   # "the GUI after a restart"
                                           spawn=lambda: self.fail("a status poll must never spawn"))
            self.assertEqual(second.running, {})
            second.refresh_running()
            self.assertEqual(second.running, {"Test Mouse": "Default"})
            first.stop_all()
            second.refresh_running()
            self.assertEqual(second.running, {})
        finally:
            first.shutdown_engine()
            thread.join(3)
        second.refresh_running()                                                  # engine gone
        self.assertEqual(second.running, {})

    def test_warnings_reach_the_client(self):
        self.store.save("Test Mouse", "Mixed", [
            {"input_combination": [{"type": 1, "code": BTN_SIDE}], "output_symbol": "KEY_F5"},
            {"input_combination": [{"type": 1, "code": BTN_EXTRA}], "output_symbol": "key(KEY_A"}])
        thread = threading.Thread(target=self.engine.run, daemon=True)
        thread.start()
        self.assertTrue(wait_for(self.engine.sock_path.exists))
        ctl = engine.BuiltinControl(self.paths, self.engine.sock_path, spawn=lambda: self.fail("no spawn"))
        try:
            code, _msg = ctl.start("Test Mouse", "Mixed")
            self.assertEqual(code, 0)
            self.assertEqual(len([w for w in ctl.last_warnings if "skipped" in w]), 1)
        finally:
            ctl.shutdown_engine()
            thread.join(3)

    def test_requests_are_logged_with_the_client_but_polls_are_not(self):
        thread = threading.Thread(target=self.engine.run, daemon=True)
        thread.start()
        self.assertTrue(wait_for(self.engine.sock_path.exists))
        ctl = engine.BuiltinControl(self.paths, self.engine.sock_path, spawn=lambda: self.fail("no spawn"))
        try:
            ctl.refresh_running()
            ctl.hello()
            ctl.start("Test Mouse", "Default")
        finally:
            ctl.shutdown_engine()
            thread.join(3)
        requests = [m for m in self.logs if m.startswith("[request]")]
        self.assertEqual(len([m for m in requests if " start " in m]), 1)
        self.assertTrue(all("pid" in m for m in requests))                       # who asked
        self.assertFalse(any(" status " in m or " hello " in m for m in requests))

    def test_a_polling_client_keeps_an_idle_engine_alive(self):
        eng = engine.Engine(self.tmp / "p.sock", lambda m: None, idle_exit=0.6, settle=0)
        thread = threading.Thread(target=eng.run, daemon=True)
        thread.start()
        self.assertTrue(wait_for(eng.sock_path.exists))
        ctl = engine.BuiltinControl(self.paths, eng.sock_path, spawn=lambda: self.fail("no spawn"))
        end = time.monotonic() + 1.6                                               # > 2x the idle time
        while time.monotonic() < end:
            ctl.refresh_running()
            time.sleep(0.1)
        self.assertFalse(eng.quit)                                                 # still wanted
        thread.join(4)                                                             # nobody asks: it leaves
        self.assertFalse(thread.is_alive())

    def test_report(self):
        ctl = engine.BuiltinControl(self.paths, self.tmp / "none.sock")
        report = ctl.report(self.paths)
        self.assertEqual(len(report), 5)
        self.assertTrue(any("/dev/uinput" in w for _ok, w, _h in report))


if __name__ == "__main__":
    unittest.main()
