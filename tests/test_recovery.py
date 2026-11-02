import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inputdeck import core, recovery  # noqa: E402


class FakeControl:
    def __init__(self, fail=False):
        self.fail, self.stopped = fail, 0

    def stop_all(self):
        if self.fail:
            raise OSError("engine hung")
        self.stopped += 1
        return 0, ""


class FindTests(unittest.TestCase):
    def test_matches_only_real_engine_processes(self):
        tmp = Path(tempfile.mkdtemp())

        def proc(pid, *argv):
            (tmp / str(pid)).mkdir()
            (tmp / str(pid) / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())

        proc(101, "/usr/bin/python3", "-m", "inputdeck", "--engine")
        proc(102, "/usr/bin/python3", "-m", "inputdeck", "--engine-stop")      # not an engine
        proc(103, "/usr/bin/python3", "-m", "inputdeck")                        # the GUI
        proc(104, "/usr/bin/vim", "notes-about-inputdeck--engine.txt")
        proc(105, "/home/u/.local/bin/inputdeck", "--engine")                   # direct launcher
        (tmp / "self").mkdir()
        self.assertEqual(sorted(recovery.find_engine_pids(tmp)), [101, 105])

    def test_stops_a_real_process_that_looks_like_the_engine(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 "-m", "inputdeck", "--engine"])
        try:
            deadline = time.monotonic() + 3                              # /proc lags right after fork
            while proc.pid not in recovery.find_engine_pids() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIn(proc.pid, recovery.find_engine_pids())
            lines = recovery.stop_engine_processes(timeout=3)
            self.assertEqual(proc.wait(timeout=3) is not None, True)
            self.assertTrue(lines and "Stopped the built-in engine" in lines[0])
            self.assertNotIn(proc.pid, recovery.find_engine_pids())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(recovery.stop_engine_processes(), [])                 # nothing left: no-op


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = core.Paths(self.tmp / "ir", self.tmp / "cfg", self.tmp / "data")
        store = core.PresetStore(self.paths)
        store.save("Mouse", "Game", [core.make_mapping([(1, 275, "h")], "KEY_F5", "keyboard")])
        core.save_rules(self.paths.rules_file, [core.Rule("game", "Mouse", "Game")])
        core.MacroLibrary(self.paths).save("Autoclick", "hold(key(BTN_LEFT))")
        self.paths.backups_dir.mkdir(parents=True)
        (self.paths.backups_dir / "keep.me").write_text("backup")
        core.save_settings(self.paths, {"backend": "builtin", "interval": 9.0, "close_to_tray": True})
        self.snapshot = self.snap()
        self.runtime = self.tmp / "run"
        self.runtime.mkdir()
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def snap(self):
        files = [self.paths.presets_dir / "Mouse" / "Game.json", self.paths.rules_file,
                 self.paths.config_dir / "macros.json", self.paths.backups_dir / "keep.me"]
        return {str(f): f.read_bytes() for f in files}

    def test_resets_settings_but_never_touches_user_data(self):
        (self.runtime / "inputdeck-engine.sock").write_text("stale")
        ctl = FakeControl()
        lines = recovery.reset(self.paths, ctl, find=lambda: [])
        self.assertEqual(ctl.stopped, 1)
        self.assertEqual(core.load_settings(self.paths), core.DEFAULT_SETTINGS)
        old = json.loads(self.paths.settings_file.with_name("settings.json.bak").read_text())
        self.assertEqual(old["backend"], "builtin")                           # the old settings are kept
        self.assertFalse((self.runtime / "inputdeck-engine.sock").exists())
        self.assertEqual(self.snap(), self.snapshot)                          # presets/rules/macros/backups intact
        self.assertTrue(any("not touched" in l for l in lines))

    def test_survives_a_hung_engine_and_can_leave_settings_alone(self):
        lines = recovery.reset(self.paths, FakeControl(fail=True), reset_settings=False, find=lambda: [])
        self.assertTrue(any("Could not ask the engine" in l for l in lines))
        self.assertEqual(core.load_settings(self.paths)["backend"], "builtin")
        self.assertEqual(self.snap(), self.snapshot)


if __name__ == "__main__":
    unittest.main()
