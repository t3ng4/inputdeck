"""Drives the real gui.py code with a fake Qt and fake evdev (see qt_stub.py)."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import qt_stub  # noqa: E402

qt_stub.install()
import test_recorder as tr  # noqa: E402  (installs fake evdev)
from inputdeck import core, gui  # noqa: E402

Ev, DEVICES, wait_for = tr.Ev, tr.DEVICES, tr.wait_for
QDialog, QMessageBox = qt_stub.QDialog, qt_stub.QMessageBox


class GuiSmoke(unittest.TestCase):
    def setUp(self):
        tr.reset()
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = core.Paths(self.tmp / "ir", self.tmp / "cfg", self.tmp / "data")
        self.cmds = []
        control = core.RemapperControl(runner=lambda c: (self.cmds.append(c[1:]), (0, ""))[1], binary="irc")
        core.PresetStore(self.paths).create("Test Mouse", "Default")
        self.state = gui.State(self.paths, self.paths.rules_file, control)
        self.win = gui.MainWindow(self.state)
        self.tab = self.win.mappings_tab
        QMessageBox.log.clear()
        QDialog.on_exec = None

    def tearDown(self):
        self.state.switcher.stop()
        QDialog.on_exec = None

    def select_mouse(self):
        item = next(i for i in self.tab.devices.rows if i.data(gui.ROLE) == "Test Mouse")
        self.tab.devices.setCurrentItem(item)

    def test_device_list_and_preset_selection(self):
        names = [i.data(gui.ROLE) for i in self.tab.devices.rows]
        self.assertEqual(names, ["Test Keyboard", "Test Mouse", "WH-1000XM3 (AVRCP)"])
        self.select_mouse()
        self.assertEqual(self.tab.preset.currentText(), "Default")
        self.assertEqual(self.tab.mappings, [])

    def test_record_input_add_mapping_save_and_apply(self):
        self.select_mouse()

        def fill(dlg):
            dlg.rec_in.clicked.emit()                              # "Record…"
            self.assertTrue(wait_for(lambda: dlg._rec is not None and
                                     DEVICES["/dev/input/event2"].get("instance") is not None and
                                     DEVICES["/dev/input/event2"]["instance"].grabbed))
            node = DEVICES["/dev/input/event2"]["instance"]
            node.push(Ev(275, 1), Ev(275, 0))                      # side button on the mouse
            self.assertTrue(wait_for(lambda: dlg.inputs))
            self.assertEqual(dlg.input_label.text(), "BTN_SIDE")
            dlg.output.setPlainText("KEY_F5")
            dlg.loop.setCurrentIndex(0)
            dlg._ok()
            return dlg._accepted

        QDialog.on_exec = fill
        self.tab.add_mapping()
        saved = core.PresetStore(self.paths).load("Test Mouse", "Default")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["output_symbol"], "KEY_F5")
        self.assertEqual(saved[0]["target_uinput"], "keyboard")
        self.assertEqual([(i["type"], i["code"]) for i in saved[0]["input_combination"]], [(1, 275)])
        self.assertIn("origin_hash", saved[0]["input_combination"][0])
        # remapping paused for recording, then restored + preset applied
        self.assertEqual(self.cmds[0], ["--command", "stop-all"])
        self.assertIn(["--command", "autoload"], self.cmds)
        self.assertEqual(self.cmds[-1], ["--command", "start", "--device", "Test Mouse", "--preset", "Default"])
        self.assertEqual(self.tab.table.rowCount(), 1)
        self.assertEqual(self.tab.table.item(0, 0).text(), "BTN_SIDE")

    def test_cancel_after_recording_restores_what_was_running(self):
        self.select_mouse()
        self.state.control.start("Test Mouse", "Default")        # something was running
        self.cmds.clear()

        def cancel(dlg):
            dlg.rec_in.clicked.emit()
            wait_for(lambda: dlg._rec is not None)
            dlg.reject()                                           # closing mid-recording
            self.assertIsNone(dlg._rec)
            return False

        QDialog.on_exec = cancel
        self.tab.add_mapping()
        self.assertEqual(self.cmds[0], ["--command", "stop-all"])
        self.assertEqual(self.cmds[-1], ["--command", "start", "--device", "Test Mouse", "--preset", "Default"])
        self.assertFalse(DEVICES["/dev/input/event2"]["instance"].grabbed)
        self.assertEqual(self.tab.mappings, [])

    def test_macro_recording_flows_into_output_with_loop(self):
        self.select_mouse()

        def mapping_dialog(dlg):
            dlg.inputs = [(1, 276, None)]
            dlg._show_input()

            def macro_dialog(m):
                m.toggle.pressed.emit()
                m.toggle.clicked.emit()                            # start
                self.assertIsNotNone(m._rec)
                DEVICES["/dev/input/event1"]["instance"].push(Ev(30, 1, 100.0), Ev(30, 0, 100.05))
                DEVICES["/dev/input/event2"]["instance"].push(Ev(272, 1, 101.0), Ev(272, 0, 101.1))
                self.assertTrue(wait_for(lambda: m._count >= 4))
                m.toggle.pressed.emit()
                m.toggle.clicked.emit()                            # stop
                self.assertEqual([s.text() for s in m.steps], ["key(KEY_A)", "wait(950)", "key(BTN_LEFT)"])
                m.accept()
                return True

            QDialog.on_exec = macro_dialog
            dlg._record_macro()
            self.assertEqual(dlg.output.toPlainText(), "key(KEY_A).wait(950).key(BTN_LEFT)")
            dlg.loop.setCurrentIndex(1)
            dlg.loop_delay.setValue(30)
            dlg._ok()
            return True

        QDialog.on_exec = mapping_dialog
        self.tab.add_mapping()
        saved = core.PresetStore(self.paths).load("Test Mouse", "Default")
        self.assertEqual(saved[0]["output_symbol"], "hold(key(KEY_A).wait(950).key(BTN_LEFT).wait(30))")
        self.assertEqual(saved[0]["target_uinput"], "keyboard + mouse")

    def test_edit_existing_keeps_hidden_fields_and_unwraps_loop(self):
        self.select_mouse()
        store = core.PresetStore(self.paths)
        store.save("Test Mouse", "Default", [{
            "input_combination": [{"type": 1, "code": 276, "origin_hash": "abc"}],
            "target_uinput": "mouse", "output_symbol": "hold(key(BTN_LEFT).wait(50))",
            "macro_key_sleep_ms": 25}])
        self.tab._preset_changed()
        self.tab.table.selectRow(0)

        def edit(dlg):
            self.assertEqual(dlg.output.toPlainText(), "key(BTN_LEFT)")
            self.assertEqual(dlg.loop.currentIndex(), 1)
            self.assertEqual(dlg.loop_delay.value(), 50)
            dlg.output.setPlainText("key(BTN_RIGHT)")
            dlg._ok()
            return True

        QDialog.on_exec = edit
        self.tab.edit_mapping()
        saved = store.load("Test Mouse", "Default")
        self.assertEqual(saved[0]["output_symbol"], "hold(key(BTN_RIGHT).wait(50))")
        self.assertEqual(saved[0]["macro_key_sleep_ms"], 25)
        self.assertEqual(saved[0]["input_combination"][0]["origin_hash"], "abc")

    def test_left_click_guard_prompts(self):
        self.select_mouse()
        QMessageBox.answer = qt_stub._Enum("QMessageBox.StandardButton.No")

        def swallow(dlg):
            dlg.inputs = [(1, 272, None)]
            dlg.output.setPlainText("KEY_A")
            dlg._ok()
            return dlg.__dict__.get("_accepted", False)

        QDialog.on_exec = swallow
        self.tab.add_mapping()
        QMessageBox.answer = qt_stub._Enum("QMessageBox.StandardButton.Yes")
        self.assertTrue(any("left mouse button" in t for k, t in QMessageBox.log if k == "question"))
        self.assertEqual(core.PresetStore(self.paths).load("Test Mouse", "Default"), [])

    def test_analog_mapping_not_editable(self):
        self.select_mouse()
        core.PresetStore(self.paths).save("Test Mouse", "Default", [{
            "input_combination": [{"type": 3, "code": 1}], "target_uinput": "mouse",
            "output_type": 2, "output_code": 1}])
        self.tab._preset_changed()
        self.tab.table.selectRow(0)
        QDialog.on_exec = lambda d: self.fail("must not open")
        self.tab.edit_mapping()
        self.assertTrue(any("analog" in t for k, t in QMessageBox.log))

    def test_rules_dialog_saves_rules_file(self):
        self.select_mouse()

        def rule(dlg):
            dlg.process.setText("mygame")
            dlg.device.setCurrentText("Test Mouse")
            dlg._ok()
            return True

        QDialog.on_exec = rule
        self.win.auto_tab.add_rule()
        self.assertEqual(core.load_rules(self.paths.rules_file), [core.Rule("mygame", "Test Mouse", "Default")])
        self.assertEqual(self.win.auto_tab.table.rowCount(), 1)
        self.win.auto_tab.table.selectRow(0)
        self.win.auto_tab.move(1)                                   # no-op at the end, must not crash
        self.win.auto_tab.remove_rule()
        self.assertEqual(core.load_rules(self.paths.rules_file), [])

    def test_switcher_thread_applies_rule(self):
        core.save_rules(self.paths.rules_file, [core.Rule("zzz-no-such-proc", "Test Mouse", "Default"),
                                                core.Rule("DEFAULT", "Test Mouse", "Default")])
        self.state.rules = core.load_rules(self.paths.rules_file)
        self.state.switcher.stop()
        self.state.switcher.interval = 0.05
        self.state.switcher.start()
        self.assertTrue(wait_for(lambda: ["--command", "start", "--device", "Test Mouse", "--preset", "Default"]
                                 in self.cmds))

    def test_apply_and_stop_buttons(self):
        self.select_mouse()
        self.tab.apply_preset()
        self.assertEqual(self.cmds[-1], ["--command", "start", "--device", "Test Mouse", "--preset", "Default"])
        self.tab.stop_device()
        self.assertEqual(self.cmds[-1], ["--command", "stop", "--device", "Test Mouse"])

    def test_ctrl_c_does_not_pop_an_error_dialog(self):
        old, old_instance = sys.excepthook, gui.QApplication.instance
        gui.QApplication.instance = staticmethod(lambda: object())      # pretend an app is running
        try:
            gui._install_excepthook()
            sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
            self.assertEqual(QMessageBox.log, [])
            sys.excepthook(ValueError, ValueError("boom"), None)
            self.assertEqual(len(QMessageBox.log), 1)
        finally:
            sys.excepthook, gui.QApplication.instance = old, old_instance

    def test_device_icons_and_settings_defaults(self):
        self.assertEqual(gui.device_icon({"mouse", "keyboard"}), "keyboard")
        self.assertEqual(gui.device_icon({"gamepad"}), "gamepad")
        self.assertEqual(gui.device_icon(set()), "other")
        self.assertEqual(gui.device_icon({"media"}), "media")
        self.assertFalse(self.state.settings["close_to_tray"])

    def test_keyboard_group_includes_consumer_control_node(self):
        names = [i.data(gui.ROLE) for i in self.tab.devices.rows]
        self.assertNotIn("Test Keyboard Consumer Control", names)

    # ---- macro library, right-click menu, credit
    def seed_two_mappings(self):
        self.select_mouse()
        store = core.PresetStore(self.paths)
        store.save("Test Mouse", "Default", [
            {"input_combination": [{"type": 1, "code": 275, "origin_hash": "h1"}],
             "target_uinput": "mouse", "output_symbol": "hold(key(BTN_LEFT).wait(50))"},
            {"input_combination": [{"type": 1, "code": 276, "origin_hash": "h2"}],
             "target_uinput": "keyboard", "output_symbol": "KEY_B"}])
        self.tab._preset_changed()

    def test_save_assign_and_rebind_macro(self):
        self.seed_two_mappings()
        qt_stub.QInputDialog.text_answer = ("Autoclick", True)
        self.tab.save_output_to_library(0)
        self.assertEqual(self.state.library.get("autoclick"), "hold(key(BTN_LEFT).wait(50))")

        # right-click menu on row 1 offers it, choosing it keeps the input and its device hash
        entries = self.tab.context_entries(1)
        labels = [e[0] for e in entries]
        self.assertIn("Assign a saved macro", labels)
        sub = dict(next(e for e in entries if e[0] == "Assign a saved macro")[1])
        sub["Autoclick"]()
        saved = core.PresetStore(self.paths).load("Test Mouse", "Default")
        self.assertEqual(saved[1]["output_symbol"], "hold(key(BTN_LEFT).wait(50))")
        self.assertEqual(saved[1]["target_uinput"], "mouse")
        self.assertEqual(saved[1]["input_combination"], [{"type": 1, "code": 276, "origin_hash": "h2"}])
        self.assertEqual(self.cmds[-1][:2], ["--command", "start"])        # applied right away

        # the use case: same macro, different button  (276 -> 275 is taken; use the keyboard-free route)
        def rebind(dlg):
            self.assertTrue(dlg.__dict__.get("_rebind_flag", True))
            self.assertEqual(dlg.output.toPlainText(), "key(BTN_LEFT)")           # output kept
            dlg.rec_in.clicked.emit()
            wait_for(lambda: DEVICES["/dev/input/event2"]["instance"].grabbed)
            DEVICES["/dev/input/event2"]["instance"].push(Ev(277, 1), Ev(277, 0))   # BTN_FORWARD
            self.assertTrue(wait_for(lambda: dlg.inputs and dlg.inputs[0][1] == 277))
            dlg._ok()
            return True

        QDialog.on_exec = rebind
        self.tab.edit_row(1, record_input_first=True)
        saved = core.PresetStore(self.paths).load("Test Mouse", "Default")
        self.assertEqual([i["code"] for i in saved[1]["input_combination"]], [277])
        self.assertEqual(saved[1]["output_symbol"], "hold(key(BTN_LEFT).wait(50))")

    def test_empty_area_menu_and_add_with_macro(self):
        self.select_mouse()
        self.state.library.save("Greeting", "key(KEY_H).key(KEY_I)")
        entries = self.tab.context_entries(None)
        self.assertEqual([e[0] for e in entries], ["Add mapping…", "Add mapping with a saved macro"])

        def fill(dlg):
            self.assertEqual(dlg.output.toPlainText(), "key(KEY_H).key(KEY_I)")   # prefilled from the library
            dlg.inputs = [(1, 275, None)]
            dlg._ok()
            return True

        QDialog.on_exec = fill
        dict(entries[1][1])["Greeting"]()
        saved = core.PresetStore(self.paths).load("Test Mouse", "Default")
        self.assertEqual(saved[0]["output_symbol"], "key(KEY_H).key(KEY_I)")

    def test_dialog_inserts_saved_macro_and_saves_new_one(self):
        self.select_mouse()
        self.state.library.save("Spam", "hold(key(KEY_A).wait(30))")

        def work(dlg):
            dlg._insert_saved("Spam")
            self.assertEqual(dlg.output.toPlainText(), "key(KEY_A)")
            self.assertEqual(dlg.loop.currentIndex(), 1)
            self.assertEqual(dlg.loop_delay.value(), 30)
            dlg.output.setPlainText("key(KEY_Z)")
            qt_stub.QInputDialog.text_answer = ("Zed", True)
            dlg._save_to_library()
            return False

        QDialog.on_exec = work
        self.tab.add_mapping()
        self.assertEqual(self.state.library.get("Zed"), "hold(key(KEY_Z).wait(30))")

    def test_library_dialog_rename_delete_and_edit_steps(self):
        self.state.library.save("One", "hold(key(KEY_A).wait(40))")
        lib = gui.LibraryDialog(self.state)
        lib.list.setCurrentRow(0)
        self.assertEqual(lib.preview.toPlainText(), "hold(key(KEY_A).wait(40))")
        qt_stub.QInputDialog.text_answer = ("Uno", True)
        lib.rename()
        self.assertEqual(self.state.library.names(), ["Uno"])
        lib.list.setCurrentRow(0)

        def edit(m):
            self.assertEqual([x.text() for x in m.steps], ["key(KEY_A)"])        # loop wrapper stripped
            m.steps = core.parse_macro("key(KEY_B)")
            m.accept()
            return True

        QDialog.on_exec = edit
        lib.edit_steps()
        self.assertEqual(self.state.library.get("Uno"), "hold(key(KEY_B).wait(40))")     # loop kept
        lib.list.setCurrentRow(0)
        lib.delete()
        self.assertEqual(self.state.library.names(), [])

    def test_credit_and_about(self):
        self.assertIn("t3ng4", self.win.windowTitle())
        self.assertEqual(gui.__author_url__, "https://github.com/t3ng4")
        QDialog.on_exec = lambda d: True
        self.win.show_about()                                     # builds the dialog without errors
        self.win.show_library()

    def test_switching_engine_stops_the_old_one_and_swaps_the_control(self):
        self.state.control.start("Test Mouse", "Default")
        self.cmds.clear()
        self.win._pick_backend("builtin")
        self.assertEqual(self.cmds[0], ["--command", "stop-all"])              # old engine released first
        self.assertEqual(getattr(self.state.control, "kind", ""), "builtin")
        self.assertIs(self.state.switcher.control, self.state.control)
        self.assertEqual(self.state.settings["backend"], "builtin")
        self.assertEqual(self.win.engine_name(), "built-in engine (experimental)")
        self.assertEqual(core.load_settings(self.paths)["backend"], "builtin")
        self.win._pick_backend("input-remapper")
        self.assertEqual(self.win.engine_name(), "input-remapper")

    def test_reset_to_defaults_keeps_presets_rules_and_macros(self):
        self.select_mouse()
        self.seed_two_mappings()
        self.state.library.save("Keep", "key(KEY_A)")
        self.state.rules = [core.Rule("game", "Test Mouse", "Default")]
        self.state.save_rules()
        self.state.settings.update({"backend": "builtin", "interval": 12.0, "close_to_tray": True})
        self.state.save_settings()
        before = {p: p.read_bytes() for p in (self.paths.presets_dir / "Test Mouse" / "Default.json",
                                              self.paths.rules_file, self.state.library.path)}
        orig = gui.recovery.find_engine_pids
        gui.recovery.find_engine_pids = lambda *a, **k: []
        try:
            self.win.reset_to_defaults()
        finally:
            gui.recovery.find_engine_pids = orig
        defaults = {k: v for k, v in core.DEFAULT_SETTINGS.items() if k != "last_device"}   # re-remembered
        for got in (self.state.settings, core.load_settings(self.paths)):
            self.assertEqual({k: v for k, v in got.items() if k != "last_device"}, defaults)
        self.assertEqual({p: p.read_bytes() for p in before}, before)             # data untouched
        self.assertIn(["--command", "stop-all"], self.cmds)
        self.assertEqual(self.state.switcher.interval, core.DEFAULT_SETTINGS["interval"])
        self.assertTrue(any(k == "question" and "NOT touched" in t for k, t in QMessageBox.log))

    def test_reset_can_be_declined(self):
        self.state.settings["interval"] = 12.0
        QMessageBox.answer = qt_stub._Enum("QMessageBox.StandardButton.No")
        try:
            self.win.reset_to_defaults()
        finally:
            QMessageBox.answer = qt_stub._Enum("QMessageBox.StandardButton.Yes")
        self.assertEqual(self.state.settings["interval"], 12.0)
        self.assertNotIn(["--command", "stop-all"], self.cmds)

    # ---- which preset is running
    def make_valk(self):
        core.PresetStore(self.paths).create("Test Mouse", "Valk")
        self.tab._device_changed()

    def test_banner_marks_the_active_preset(self):
        self.select_mouse()
        self.make_valk()
        self.assertIn("unknown", self.tab.active_label.text())                    # input-remapper can't tell
        self.tab.preset.setCurrentText("Valk")
        self.tab.apply_preset()
        self.assertIn("<b>Valk</b> is <b>active</b>", self.tab.active_label.text())
        self.assertIn("● Valk", [i for i in self.tab.devices.rows if i.data(gui.ROLE) == "Test Mouse"][0].text())
        self.assertFalse(self.tab.show_active.__dict__.get("_visible", False))
        self.tab.preset.setCurrentText("Default")                                  # viewing another one
        self.tab.refresh_active()
        text = self.tab.active_label.text()
        self.assertIn("Active now: <b>Valk</b>", text)
        self.assertIn("viewing <b>Default</b> (not active)", text)
        self.assertTrue(self.tab.show_active.__dict__["_visible"])
        self.tab._show_active_clicked()
        self.assertEqual(self.tab.preset.currentText(), "Valk")
        self.tab.stop_device()
        self.assertIn("unknown", self.tab.active_label.text())

    def test_opens_on_the_running_preset_not_on_default(self):
        core.PresetStore(self.paths).create("Test Mouse", "Valk")
        self.state.control.running["Test Mouse"] = "Valk"                         # engine outlived the GUI
        self.select_mouse()
        self.tab._device_changed()
        self.assertEqual(self.tab.preset.currentText(), "Valk")

    def test_builtin_backend_says_nothing_is_active_instead_of_unknown(self):
        from inputdeck import engine
        self.state.control = engine.BuiltinControl(self.paths, self.tmp / "none.sock")
        self.select_mouse()
        self.tab.refresh_active()
        self.assertIn("No preset is active", self.tab.active_label.text())

    def test_apply_warns_about_skipped_mappings(self):
        self.select_mouse()
        self.state.control.last_warnings = ["KEY_8: expected ')' — skipped"]
        self.tab.apply_preset()
        self.assertTrue(any(k == "warning" and "skipped" in t for k, t in QMessageBox.log))

    def test_auto_switch_summary(self):
        self.win.auto_tab.refresh_summary()
        self.assertIn("No preset is active", self.win.auto_tab.active_summary.text())
        self.state.control.running["Test Mouse"] = "Valk"
        self.win.auto_tab.refresh_summary()
        self.assertIn("Test Mouse → <b>Valk</b>", self.win.auto_tab.active_summary.text())

    # ---- logs and menus
    def test_menu_titles(self):
        self.assertEqual(gui.MENU_TITLES, ("&Options", "&Logs", "&Help"))

    def test_log_file_and_viewer(self):
        self.state._log_line("[switch] Dev → Valk")
        self.assertIn("[switch] Dev → Valk", self.state.log_file.read_text())
        dlg = gui.LogDialog("Auto-switch log", self.state.log_file)
        self.assertIn("[switch] Dev → Valk", dlg.view.toPlainText())
        empty = gui.LogDialog("Engine log", self.paths.data_dir / "engine.log")
        self.assertEqual(empty.view.toPlainText(), "(nothing logged yet)")
        self.state._log_line("second")                                            # follow mode picks it up
        dlg._tick()
        self.assertIn("second", dlg.view.toPlainText())

    def test_create_and_delete_preset(self):
        self.select_mouse()
        qt_stub.QInputDialog.text_answer = ("Gaming", True)
        self.tab.duplicate_preset()
        self.assertEqual(self.tab.preset.currentText(), "Gaming")
        self.tab.delete_preset()
        self.assertEqual(core.PresetStore(self.paths).presets("Test Mouse"), ["Default"])


if __name__ == "__main__":
    unittest.main()
