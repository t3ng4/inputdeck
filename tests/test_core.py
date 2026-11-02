import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inputdeck import core  # noqa: E402
from inputdeck.core import Step  # noqa: E402


def ev(t, name, v):
    return (t, name, v)


class MacroTests(unittest.TestCase):
    def test_tap_and_wait(self):
        steps = core.events_to_steps([ev(0.0, "KEY_A", 1), ev(0.05, "KEY_A", 0),
                                      ev(0.65, "KEY_B", 1), ev(0.70, "KEY_B", 0)])
        self.assertEqual(core.steps_to_text(steps), "key(KEY_A).wait(600).key(KEY_B)")

    def test_chord_uses_down_up(self):
        steps = core.events_to_steps([
            ev(0.0, "KEY_LEFTCTRL", 1), ev(0.1, "KEY_C", 1), ev(0.15, "KEY_C", 0),
            ev(0.3, "KEY_LEFTCTRL", 0)])
        self.assertEqual(core.steps_to_text(steps),
                         "key_down(KEY_LEFTCTRL).wait(100).key(KEY_C).wait(150).key_up(KEY_LEFTCTRL)")

    def test_long_press_is_not_a_tap(self):
        steps = core.events_to_steps([ev(0, "BTN_LEFT", 1), ev(0.5, "BTN_LEFT", 0)])
        self.assertEqual(core.steps_to_text(steps), "key_down(BTN_LEFT).wait(500).key_up(BTN_LEFT)")

    def test_orphan_release_dropped_and_stuck_key_released(self):
        steps = core.events_to_steps([ev(0, "BTN_LEFT", 0), ev(1, "KEY_A", 1)])
        self.assertEqual(core.steps_to_text(steps), "key(KEY_A)")   # still held at the end -> released at once

    def test_fixed_delay(self):
        steps = core.events_to_steps([ev(0, "KEY_A", 1), ev(0.01, "KEY_A", 0),
                                      ev(3, "KEY_B", 1), ev(3.01, "KEY_B", 0)], fixed_delay_ms=50)
        self.assertEqual(core.steps_to_text(steps), "key(KEY_A).wait(50).key(KEY_B)")

    def test_parse_roundtrip(self):
        text = "key_down(KEY_LEFTCTRL).wait(100).key(KEY_C).key_up(KEY_LEFTCTRL)"
        steps = core.parse_macro(text)
        self.assertEqual(core.steps_to_text(steps), text)
        self.assertEqual(steps[1], Step("wait", 100))

    def test_parse_rejects_unknown_functions(self):
        self.assertIsNone(core.parse_macro("repeat(3, key(a))"))
        self.assertIsNone(core.parse_macro("hold_keys(a, b)"))
        self.assertEqual(core.parse_macro(""), [])

    def test_loop_wrap_unwrap(self):
        self.assertEqual(core.wrap_loop("key(BTN_LEFT)", "hold", 50), "hold(key(BTN_LEFT).wait(50))")
        self.assertEqual(core.wrap_loop("key(a)", "toggle"), "toggle(key(a))")
        self.assertEqual(core.wrap_loop("key(a)", ""), "key(a)")
        self.assertEqual(core.unwrap_loop("hold(key(BTN_LEFT).wait(50))"), ("hold", "key(BTN_LEFT)", 50))
        self.assertEqual(core.unwrap_loop("toggle(key(a))"), ("toggle", "key(a)", 0))
        self.assertEqual(core.unwrap_loop("key(a).key(b)"), ("", "key(a).key(b)", 0))

    def test_keys_to_output(self):
        self.assertEqual(core.keys_to_output(["KEY_A"]), "KEY_A")
        self.assertEqual(core.keys_to_output(["KEY_LEFTCTRL", "KEY_C"]), "hold_keys(KEY_LEFTCTRL, KEY_C)")

    def test_guess_target(self):
        self.assertEqual(core.guess_target("KEY_A"), "keyboard")
        self.assertEqual(core.guess_target("Control_L + a"), "keyboard")
        self.assertEqual(core.guess_target("BTN_LEFT"), "mouse")
        self.assertEqual(core.guess_target("key(BTN_LEFT).wait(50)"), "mouse")
        self.assertEqual(core.guess_target("key(KEY_A).key(BTN_RIGHT)"), "keyboard + mouse")
        self.assertEqual(core.guess_target("hold_keys(KEY_A, BTN_SIDE)"), "keyboard + mouse")
        self.assertEqual(core.guess_target("wheel(up, 10)"), "mouse")

    def test_left_click_guard(self):
        swallow = core.make_mapping([(1, 272, None)], "KEY_A", "keyboard")
        autofire = core.make_mapping([(1, 272, None)], "hold(key(BTN_LEFT).wait(50))", "mouse")
        chord = core.make_mapping([(1, 29, None), (1, 272, None)], "KEY_A", "keyboard")
        self.assertTrue(core.blocks_left_click([swallow]))
        self.assertFalse(core.blocks_left_click([autofire]))
        self.assertFalse(core.blocks_left_click([chord]))

    def test_syntax_check(self):
        self.assertIsNone(core.check_macro_syntax("key(a).wait(1)"))
        self.assertIsNotNone(core.check_macro_syntax("key(a"))
        self.assertIsNotNone(core.check_macro_syntax("key(a))"))


class MappingTests(unittest.TestCase):
    def test_make_mapping_keeps_unknown_fields(self):
        base = {"macro_key_sleep_ms": 100, "release_timeout": 0.2, "output_type": 2}
        m = core.make_mapping([(1, 30, "abc")], "KEY_B", "keyboard", base)
        self.assertEqual(m["macro_key_sleep_ms"], 100)
        self.assertEqual(m["release_timeout"], 0.2)
        self.assertNotIn("output_type", m)
        self.assertEqual(m["input_combination"], [{"type": 1, "code": 30, "origin_hash": "abc"}])
        self.assertEqual(core.describe_input(m), "KEY_A")


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = core.Paths(self.tmp / "ir", self.tmp / "cfg", self.tmp / "data")
        self.store = core.PresetStore(self.paths)

    def test_create_save_load_backup_delete(self):
        self.assertEqual(self.store.devices(), [])
        self.store.create("Mouse X", "Default")
        self.assertEqual(self.store.presets("Mouse X"), ["Default"])
        m = [core.make_mapping([(1, 275, None)], "KEY_F", "keyboard")]
        self.store.save("Mouse X", "Default", m)
        self.assertEqual(self.store.load("Mouse X", "Default"), m)
        self.store.save("Mouse X", "Default", [])           # second write -> backup of first
        self.assertTrue(list((self.paths.backups_dir / "Mouse X").glob("Default.json.*")))
        self.store.create("Mouse X", "Game", base="Default")
        self.store.delete("Mouse X", "Game")
        self.assertEqual(self.store.presets("Mouse X"), ["Default"])
        self.assertTrue(list((self.paths.trash_dir / "Mouse X").glob("Game.json.*")))

    def test_duplicate_and_bad_files(self):
        self.store.create("K", "A")
        with self.assertRaises(core.PresetError):
            self.store.create("K", "A")
        self.store.file("K", "A").write_text("{not json")
        with self.assertRaises(core.PresetError):
            self.store.load("K", "A")
        self.store.file("K", "A").write_text('{"a": 1}')
        with self.assertRaises(core.PresetError):
            self.store.load("K", "A")
        self.store.file("K", "A").write_text("")
        self.assertEqual(self.store.load("K", "A"), [])

    def test_loads_documented_example(self):
        example = [{"input_combination": [{"type": 1, "code": 307}], "target_uinput": "keyboard",
                    "output_symbol": "key(2).key(3)", "macro_key_sleep_ms": 100},
                   {"input_combination": [{"type": 3, "code": 1}], "target_uinput": "mouse",
                    "output_type": 2, "output_code": 1, "gain": 0.5}]
        self.store.file("gamepad", "a").parent.mkdir(parents=True)
        self.store.file("gamepad", "a").write_text(json.dumps(example))
        loaded = self.store.load("gamepad", "a")
        self.assertEqual(core.describe_output(loaded[1]), "analog 2:1")
        self.store.save("gamepad", "a", loaded)                 # round trip untouched
        self.assertEqual(self.store.load("gamepad", "a"), example)


class HashRepairTests(unittest.TestCase):
    def test_repair_unique_and_ambiguous(self):
        nodes = [{"hash": "n1", "codes": {(1, 30), (1, 31)}},
                 {"hash": "n2", "codes": {(1, 272), (1, 273)}},
                 {"hash": "n3", "codes": {(1, 272), (1, 274)}}]
        mappings = [core.make_mapping([(1, 30, "stale")], "KEY_B", "keyboard"),
                    core.make_mapping([(1, 272, "stale2")], "KEY_C", "keyboard"),
                    core.make_mapping([(1, 273, "n2")], "KEY_D", "keyboard"),     # already valid
                    core.make_mapping([(1, 999, "stale3")], "KEY_E", "keyboard")]
        out, changes, unresolved = core.repair_origin_hashes(mappings, nodes)
        self.assertEqual(out[0]["input_combination"][0]["origin_hash"], "n1")
        self.assertEqual(out[1]["input_combination"][0]["origin_hash"], "n2")   # n2 is the trusted node
        self.assertEqual(len(changes), 2)
        self.assertEqual(len(unresolved), 1)

    def test_origin_hash_recipe(self):
        h = core.origin_hash({1: [30]}, "Kbd")
        self.assertEqual(len(h), 32)
        self.assertEqual(h, core.origin_hash({1: [30]}, "Kbd"))
        self.assertNotEqual(h, core.origin_hash({1: [31]}, "Kbd"))


class RuleTests(unittest.TestCase):
    TEXT = """# comment
Palworld-Win64-Shipping.exe|Razer Tartarus|Palworld
tModLoader | Razer Tartarus | Terraria   # trailing
DEFAULT|Razer Tartarus|Default
steam|Mouse|Browsing
bad line
"""

    def test_parse_dump_roundtrip(self):
        rules = core.parse_rules(self.TEXT)
        self.assertEqual(len(rules), 4)
        self.assertEqual(core.parse_rules(core.dump_rules(rules)), rules)

    def test_decide_first_match_per_device_and_default(self):
        rules = core.parse_rules(self.TEXT)
        self.assertEqual(core.decide(rules, ["/usr/bin/bash"]), {"Razer Tartarus": "Default"})
        got = core.decide(rules, ["/x/Palworld-Win64-Shipping.exe -foo", "tmodloader"])
        self.assertEqual(got, {"Razer Tartarus": "Palworld"})           # first rule wins
        got = core.decide(rules, ["/home/u/.steam/steam"])
        self.assertEqual(got, {"Razer Tartarus": "Default", "Mouse": "Browsing"})

    def test_scan_processes(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "123").mkdir()
        (tmp / "123" / "cmdline").write_bytes(b"/usr/bin/game\0--fullscreen\0")
        (tmp / "124").mkdir()
        (tmp / "124" / "cmdline").write_bytes(b"")                     # kernel thread
        (tmp / "self").mkdir()
        self.assertEqual(core.scan_processes(tmp), ["/usr/bin/game --fullscreen"])

    def test_save_rules_makes_backup(self):
        tmp = Path(tempfile.mkdtemp()) / "rules.txt"
        core.save_rules(tmp, core.parse_rules(self.TEXT))
        core.save_rules(tmp, [])
        self.assertTrue(tmp.with_name("rules.txt.bak").is_file())
        self.assertEqual(core.load_rules(tmp), [])


class LogFileTests(unittest.TestCase):
    def test_append_tail_rotate(self):
        p = Path(tempfile.mkdtemp()) / "sub" / "x.log"
        self.assertEqual(core.read_tail(p), "(nothing logged yet)")
        for i in range(50):
            core.append_log(p, f"line {i}")
        self.assertEqual(core.read_tail(p, 3), "line 47\nline 48\nline 49")
        core.rotate_log(p, max_bytes=10_000)                      # small file: untouched
        self.assertEqual(len(p.read_text().splitlines()), 50)
        core.rotate_log(p, max_bytes=100, keep_lines=5)
        self.assertEqual(p.read_text().splitlines(), [f"line {i}" for i in range(45, 50)])
        core.rotate_log(p.with_name("missing.log"))               # no file: no error


class SwitcherWarningTests(unittest.TestCase):
    def test_skipped_mappings_show_up_in_the_switch_log(self):
        tmp = Path(tempfile.mkdtemp())
        paths = core.Paths(tmp / "ir", tmp / "cfg", tmp / "data")
        store = core.PresetStore(paths)
        store.create("Pad", "Game")

        class Ctl:
            last_warnings = ["KEY_8: expected ')' — skipped", "KEY_3: “p” was read as KEY_P"]

            def start(self, d, p):
                return 0, ""

        logs = []
        sw = core.Switcher(Ctl(), store, lambda: [core.Rule("x", "Pad", "Game")], lambda: ["x"], logs.append)
        sw.tick()
        self.assertEqual(len(logs), 1)
        self.assertIn("1 mapping(s) skipped", logs[0])             # only real skips are counted


class LibraryTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.lib = core.MacroLibrary(core.Paths(tmp / "ir", tmp / "cfg", tmp / "data"))

    def test_save_get_rename_delete(self):
        self.assertEqual(self.lib.all(), [])
        self.lib.save(" Autoclick ", "hold(key(BTN_LEFT).wait(50))")
        self.assertEqual(self.lib.get("autoclick"), "hold(key(BTN_LEFT).wait(50))")
        with self.assertRaises(core.LibraryError):
            self.lib.save("AUTOCLICK", "key(a)")
        self.lib.save("AUTOCLICK", "key(a)", overwrite=True)
        self.assertEqual(self.lib.names(), ["Autoclick"])
        self.lib.save("Other", "key(b)")
        with self.assertRaises(core.LibraryError):
            self.lib.rename("Other", "autoclick")
        self.lib.rename("Other", "Second")
        self.lib.delete("Autoclick")
        self.assertEqual(self.lib.names(), ["Second"])
        with self.assertRaises(core.LibraryError):
            self.lib.save("", "key(a)")

    def test_survives_garbage_file(self):
        self.lib.path.parent.mkdir(parents=True, exist_ok=True)
        self.lib.path.write_text("not json")
        self.assertEqual(self.lib.all(), [])
        self.lib.save("x", "key(a)")
        self.assertEqual(self.lib.names(), ["x"])


class PathsTests(unittest.TestCase):
    def test_standalone_uses_own_folder_and_prefers_input_remappers_when_present(self):
        import os
        import unittest.mock as m
        tmp = Path(tempfile.mkdtemp())
        env = {"XDG_CONFIG_HOME": str(tmp / "cfg"), "XDG_DATA_HOME": str(tmp / "data")}
        with m.patch.dict(os.environ, env), m.patch.object(core.shutil, "which", lambda e: None):
            os.environ.pop("INPUT_REMAPPER_CONFIG_DIR", None)
            p = core.Paths.default()                              # nothing installed: own folder
            self.assertEqual(p.presets_dir, tmp / "cfg" / "inputdeck" / "presets")
            (tmp / "cfg" / "input-remapper-2").mkdir(parents=True)
            self.assertEqual(core.Paths.default().remapper_dir, tmp / "cfg" / "input-remapper-2")
        with m.patch.dict(os.environ, env), m.patch.object(core.shutil, "which", lambda e: "/bin/x"):
            os.environ.pop("INPUT_REMAPPER_CONFIG_DIR", None)
            (tmp / "cfg" / "input-remapper-2").rmdir()            # input-remapper installed, no dir yet
            self.assertEqual(core.Paths.default().remapper_dir, tmp / "cfg" / "input-remapper-2")


class SetupTests(unittest.TestCase):
    def test_hints_follow_package_manager(self):
        import unittest.mock as m
        for exe, family, expect in (("pacman", "pacman", "sudo pacman -S --needed python-pyqt6 python-evdev"),
                                    ("apt-get", "apt", "sudo apt install python3-pyqt6 python3-evdev"),
                                    ("dnf", "dnf", "sudo dnf install python3-qt6 python3-evdev")):
            with m.patch.object(core.shutil, "which", lambda e, exe=exe: "/bin/x" if e == exe else None):
                self.assertEqual(core.package_manager(), family)
                self.assertEqual(core.python_deps_hint(), expect)
        with m.patch.object(core.shutil, "which", lambda e: None):
            self.assertEqual(core.package_manager(), "")
            self.assertIn("venv", core.python_deps_hint())

    def test_report_flags_old_1x_folder(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "input-remapper").mkdir()
        paths = core.Paths(tmp / "input-remapper", tmp / "c", tmp / "d")
        ctl = core.RemapperControl(runner=lambda c: (1, ""), binary="/nonexistent")
        report = {w: ok for ok, w, _h in core.setup_report(ctl, paths)}
        self.assertFalse(report["input-remapper 2.x preset folder (not the 1.x layout)"])
        paths2 = core.Paths(tmp / "input-remapper-2", tmp / "c", tmp / "d")
        report2 = {w: ok for ok, w, _h in core.setup_report(ctl, paths2)}
        self.assertTrue(report2["input-remapper 2.x preset folder (not the 1.x layout)"])


class FakeControl:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def start(self, device, preset):
        self.calls.append((device, preset))
        return (1, "boom") if self.fail else (0, "")


class SwitcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = core.Paths(self.tmp / "ir", self.tmp / "cfg", self.tmp / "data")
        self.store = core.PresetStore(self.paths)
        for p in ("Default", "Game"):
            self.store.create("Pad", p)
        self.procs = ["bash"]
        self.logs = []

    def make(self, control):
        rules = core.parse_rules("mygame|Pad|Game\nDEFAULT|Pad|Default\nother|Pad|Missing")
        return core.Switcher(control, self.store, lambda: rules, lambda: list(self.procs),
                             self.logs.append)

    def test_switches_only_on_change(self):
        c = FakeControl()
        s = self.make(c)
        s.tick(); s.tick()
        self.assertEqual(c.calls, [("Pad", "Default")])
        self.procs.append("/opt/mygame")
        s.tick(); s.tick()
        self.assertEqual(c.calls, [("Pad", "Default"), ("Pad", "Game")])
        self.procs.remove("/opt/mygame")
        s.tick()
        self.assertEqual(c.calls[-1], ("Pad", "Default"))

    def test_missing_preset_is_skipped_and_failures_back_off(self):
        c = FakeControl()
        s = self.make(c)
        self.procs.append("other")
        s.tick()
        self.assertEqual(c.calls, [])                                        # "Missing" wins, skipped
        self.assertTrue(any("does not exist" in l for l in self.logs))
        self.procs.remove("other")
        c2 = FakeControl(fail=True)
        s2 = self.make(c2)
        s2.tick(); s2.tick()
        self.assertEqual(len(c2.calls), 1)                                   # backs off after failure

    def test_pause_resume_reapplies(self):
        c = FakeControl()
        s = self.make(c)
        s.tick()
        s.pause(); s.tick()
        self.assertEqual(len(c.calls), 1)
        s.resume(); s.tick()
        self.assertEqual(len(c.calls), 2)
        s.pause(); s.resume(keep=["Pad"]); s.tick()          # manual apply sticks
        self.assertEqual(len(c.calls), 2)
        self.procs.append("mygame"); s.tick()                # ...until the rules change their mind
        self.assertEqual(c.calls[-1], ("Pad", "Game"))


class ControlTests(unittest.TestCase):
    def test_commands(self):
        seen = []
        ctl = core.RemapperControl(runner=lambda cmd: (seen.append(cmd), (0, "Daemon answered with \"hello\""))[1],
                                   binary="irc")
        self.assertTrue(ctl.hello())
        ctl.start("Dev", "P")
        ctl.stop("Dev")
        ctl.stop_all()
        self.assertEqual(seen[1], ["irc", "--command", "start", "--device", "Dev", "--preset", "P"])
        self.assertEqual(seen[2], ["irc", "--command", "stop", "--device", "Dev"])
        self.assertEqual(seen[3], ["irc", "--command", "stop-all"])
        self.assertEqual(ctl.running, {})

    def test_tracks_running_presets(self):
        ctl = core.RemapperControl(runner=lambda cmd: (0, ""), binary="irc")
        ctl.start("A", "P1"); ctl.start("B", "P2"); ctl.stop("A")
        self.assertEqual(ctl.running, {"B": "P2"})
        ctl.stop_all()
        self.assertEqual(ctl.running, {})
        failing = core.RemapperControl(runner=lambda cmd: (1, "x"), binary="irc")
        failing.start("A", "P")
        self.assertEqual(failing.running, {})
        self.assertEqual(ctl.autoload()[0], 0)


if __name__ == "__main__":
    unittest.main()
