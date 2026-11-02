"""InputDeck GUI (PyQt6)."""
from __future__ import annotations

import copy
import html
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QDesktopServices, QFontDatabase, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import __author__, __author_url__, __url__, __version__, backends, core, icons, recorder, recovery, windows

ROLE = Qt.ItemDataRole.UserRole
MOUSE_BUTTON_CHOICES = [("Left click", "BTN_LEFT"), ("Right click", "BTN_RIGHT"),
                        ("Middle click", "BTN_MIDDLE"), ("Side button", "BTN_SIDE"),
                        ("Extra button", "BTN_EXTRA"), ("Forward", "BTN_FORWARD"),
                        ("Back", "BTN_BACK")]
LOOP_MODES = ["", "hold", "toggle"]


def mono_font():
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


def button(text: str, icon: str = "") -> QPushButton:
    b = QPushButton(text)
    if icon:
        b.setIcon(icons.qt_icon(icon))
    return b


def device_icon(kinds) -> str:
    for kind in ("keyboard", "mouse", "gamepad", "media"):
        if kind in (kinds or ()):
            return kind
    return "other"


GREEN = "#2ecc71"
MENU_TITLES = ("&Options", "&Logs", "&Help")


def open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


def open_folder(path: Path) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def fill_menu(menu, entries) -> None:
    """entries: ("sep",) | (label, callable) | (label, None=disabled) | (label, [sub entries])."""
    for entry in entries:
        if entry[0] == "sep":
            menu.addSeparator()
        elif isinstance(entry[1], list):
            fill_menu(menu.addMenu(entry[0]), entry[1])
        else:
            act = menu.addAction(entry[0])
            if entry[1] is None:
                act.setEnabled(False)
            else:
                act.triggered.connect(lambda _c=False, fn=entry[1]: fn())


def warn(parent, text: str, title: str = "InputDeck") -> None:
    QMessageBox.warning(parent, title, text)


def ask(parent, text: str, title: str = "InputDeck", default_no: bool = False) -> bool:
    default = QMessageBox.StandardButton.No if default_no else QMessageBox.StandardButton.Yes
    reply = QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, default)
    return reply == QMessageBox.StandardButton.Yes


# --------------------------------------------------------------------------- shared state

class State(QObject):
    """Everything the windows share. Qt-free logic lives in core.py."""
    log = pyqtSignal(str)

    def __init__(self, paths: core.Paths, rules_file: Path,
                 control: Optional[core.RemapperControl] = None):
        super().__init__()
        self.paths = paths
        self.store = core.PresetStore(paths)
        self.settings = core.load_settings(paths)
        self.control = control or backends.make_control(self.settings, paths)
        self.rules_file = rules_file
        self.rules = core.load_rules(rules_file)
        self.library = core.MacroLibrary(paths)
        self.devices: list = []
        self.denied = 0
        self.log_file = paths.data_dir / "inputdeck.log"
        core.rotate_log(self.log_file)
        self.switcher = core.Switcher(
            self.control, self.store, lambda: list(self.rules),
            log=self._log_line,
            interval=float(self.settings["interval"]),
            notify=core.send_notification if self.settings["notify"] else None,
            windows=windows.XpropProvider().snapshot)
        self.switcher.use_windows = bool(self.settings.get("window_rules", True))

    def _log_line(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}"
        core.append_log(self.log_file, line)
        self.log.emit(line[11:])                       # the on-screen log omits the date

    def refresh_devices(self) -> None:
        self.devices, self.denied = recorder.list_input_devices()

    def device_names(self) -> list:
        return sorted({d.name for d in self.devices} | set(self.store.devices()), key=str.lower)

    def device_info(self, name: str):
        return next((d for d in self.devices if d.name == name), None)

    def save_rules(self) -> None:
        core.save_rules(self.rules_file, self.rules)

    def apply_default_settings(self) -> None:
        """After recovery.reset(): adopt the default settings (presets/rules/macros are untouched)."""
        self.settings = dict(core.DEFAULT_SETTINGS)
        self.control = backends.make_control(self.settings, self.paths)
        self.switcher.control = self.control
        self.switcher.interval = float(self.settings["interval"])
        self.switcher.reapply()

    def set_backend(self, choice: str) -> None:
        """Switch between input-remapper and the built-in engine at runtime."""
        self.settings["backend"] = choice
        self.save_settings()
        self.control.stop_all()
        self.control = backends.make_control(self.settings, self.paths)
        self.switcher.control = self.control
        self.switcher.reapply()

    def save_settings(self) -> None:
        core.save_settings(self.paths, self.settings)

    # Remapping has to be off while recording, otherwise the daemon owns the raw keys.
    # Paused lazily (first Record click) and restored when the editing dialog closes.
    _paused = False
    _snapshot: dict = {}

    def begin_edit(self) -> None:
        if self._paused:
            return
        self._paused = True
        self._snapshot = dict(self.control.running)
        self.switcher.pause()
        self.control.stop_all()

    def end_edit(self, device: str, preset: str, apply: bool) -> Optional[str]:
        """Put remapping back. Returns an error message, if any."""
        if not self._paused:
            return None
        self._paused = False
        self.control.autoload()                        # whatever input-remapper loads at login
        for dev, pre in self._snapshot.items():        # then what was running from here
            if self.store.exists(dev, pre):
                self.control.start(dev, pre)
        message, keep = None, []
        if apply and device and preset and self.store.exists(device, preset):
            code, out = self.control.start(device, preset)
            if code == 0:
                keep.append(device)
            else:
                message = f"Could not apply “{preset}”: {out}"
        self.switcher.resume(keep=keep)
        return message


# --------------------------------------------------------------------------- dialogs

class ChordBus(QObject):
    done = pyqtSignal(object, str)


class MacroBus(QObject):
    event = pyqtSignal(object)
    error = pyqtSignal(str)


class ProcessPicker(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pick a running program")
        self.resize(420, 520)
        self.choice = ""
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Pick the program. The rule matches any process whose command "
                             "line contains this text, so a distinctive part is enough."))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter…")
        self.list = QListWidget()
        self.list.addItems(core.process_names())
        lay.addWidget(self.filter)
        lay.addWidget(self.list, 1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.filter.textChanged.connect(self._apply_filter)
        self.list.itemDoubleClicked.connect(lambda _i: self.accept())

    def _apply_filter(self, text: str) -> None:
        text = text.lower()
        for i in range(self.list.count()):
            self.list.item(i).setHidden(text not in self.list.item(i).text().lower())

    def accept(self) -> None:
        item = self.list.currentItem()
        self.choice = item.text() if item else ""
        super().accept()


class RuleDialog(QDialog):
    def __init__(self, state: State, rule: Optional[core.Rule] = None, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("Auto-switch rule")
        lay = QVBoxLayout(self)
        self.default_box = QCheckBox("Fallback: use this preset whenever no other rule matches")
        self.process = QLineEdit()
        self.process.setPlaceholderText("e.g. Palworld-Win64-Shipping.exe or firefox")
        pick = QPushButton("Pick running program…")
        pick.clicked.connect(self._pick)
        row = QHBoxLayout()
        row.addWidget(self.process, 1)
        row.addWidget(pick)
        self.device = QComboBox()
        self.device.addItems(state.device_names())
        self.preset = QComboBox()
        lay.addWidget(self.default_box)
        self.when = QComboBox()
        for key, text in core.WHEN_CHOICES:
            self.when.addItem(text, key)
        lay.addWidget(QLabel("Program (matched against its command line, window class and title):"))
        lay.addLayout(row)
        self.when_label = QLabel("Apply this rule while the program…")
        lay.addWidget(self.when_label)
        lay.addWidget(self.when)
        lay.addWidget(QLabel("…switch this device:"))
        lay.addWidget(self.device)
        lay.addWidget(QLabel("…to this preset:"))
        lay.addWidget(self.preset)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._ok)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.default_box.toggled.connect(self._default_toggled)
        self.device.currentTextChanged.connect(self._reload_presets)
        if rule:
            self.process.setText("" if rule.is_default else rule.process)
            self.default_box.setChecked(rule.is_default)
            i = self.when.findData(rule.when)
            if i >= 0:
                self.when.setCurrentIndex(i)
            i = self.device.findText(rule.device)
            if i >= 0:
                self.device.setCurrentIndex(i)
        self._reload_presets(self.device.currentText())
        if rule:
            i = self.preset.findText(rule.preset)
            if i >= 0:
                self.preset.setCurrentIndex(i)
        self._default_toggled(self.default_box.isChecked())

    def _default_toggled(self, on: bool) -> None:
        self.process.setEnabled(not on)
        self.when.setEnabled(not on)
        self.when_label.setEnabled(not on)

    def _reload_presets(self, device: str) -> None:
        self.preset.clear()
        self.preset.addItems(self.state.store.presets(device))

    def _pick(self) -> None:
        dlg = ProcessPicker(self)
        if dlg.exec() and dlg.choice:
            self.process.setText(dlg.choice)

    def _ok(self) -> None:
        if not self.default_box.isChecked() and not self.process.text().strip():
            warn(self, "Enter a program name, or tick the fallback box.")
            return
        if not self.device.currentText() or not self.preset.currentText():
            warn(self, "This device has no presets yet. Create one on the Mappings tab first.")
            return
        self.accept()

    def result_rule(self) -> core.Rule:
        proc = core.DEFAULT_RULE if self.default_box.isChecked() else self.process.text().strip()
        when = "running" if self.default_box.isChecked() else (self.when.currentData() or "running")
        return core.Rule(proc.replace("|", " "), self.device.currentText(), self.preset.currentText(), when)


class MacroDialog(QDialog):
    """Record a sequence of keys / mouse buttons with real timing and edit it as steps."""

    def __init__(self, state: State, initial_text: str = "", parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("Record macro")
        self.resize(560, 620)
        self.result_text = initial_text
        self.steps: list = []
        self.raw_text = ""
        self._rec: Optional[recorder.MacroRecorder] = None
        self._press_ts: Optional[float] = None
        self._count = 0
        self.bus = MacroBus()
        self.bus.event.connect(self._on_event)
        self.bus.error.connect(self._on_error)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Record from these devices (their presses and releases, with the pauses "
                             "between them, become the macro):"))
        self.devices = QListWidget()
        self.devices.setMaximumHeight(110)
        for d in state.devices:
            if d.kinds & {"keyboard", "mouse"}:
                item = QListWidgetItem(f"{d.name}   [{d.kind_label}]")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                item.setData(ROLE, d.paths)
                self.devices.addItem(item)
        lay.addWidget(self.devices)

        row = QHBoxLayout()
        self.toggle = button("Start recording", "record")
        self.toggle.pressed.connect(self._toggle_pressed)
        self.toggle.clicked.connect(self._toggle_clicked)
        self.status = QLabel("Idle. Clicks on this window keep working while recording.")
        self.status.setWordWrap(True)
        row.addWidget(self.toggle)
        row.addWidget(self.status, 1)
        lay.addLayout(row)

        opts = QHBoxLayout()
        self.fixed = QCheckBox("Use a fixed pause between steps:")
        self.fixed_ms = QSpinBox()
        self.fixed_ms.setRange(0, 5000)
        self.fixed_ms.setValue(50)
        self.fixed_ms.setSuffix(" ms")
        self.fixed_ms.setSingleStep(10)
        opts.addWidget(self.fixed)
        opts.addWidget(self.fixed_ms)
        opts.addStretch(1)
        lay.addLayout(opts)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._edit_wait)
        delete_key = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.list)
        delete_key.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_key.activated.connect(self._delete_selected)
        lay.addWidget(self.list, 1)
        lay.addWidget(QLabel("Double-click a wait to retime it. Select steps and press Delete to remove them."))
        row2 = QHBoxLayout()
        for text, fn, ico in (("Delete selected", self._delete_selected, "delete"),
                              ("Clear all", self._clear, "remove")):
            b = button(text, ico)
            b.clicked.connect(fn)
            row2.addWidget(b)
        row2.addStretch(1)
        lay.addLayout(row2)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono_font())
        self.preview.setMaximumHeight(80)
        lay.addWidget(QLabel("Resulting macro:"))
        lay.addWidget(self.preview)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

        parsed = core.parse_macro(initial_text)
        if parsed is None:
            self.raw_text = initial_text
            self.status.setText("This macro uses functions the recorder can't edit visually. "
                                "It is kept as it is unless you record or clear steps.")
        else:
            self.steps = parsed
        self._refresh()

    # ----- recording
    def _set_toggle(self, recording: bool) -> None:
        self.toggle.setText("Stop recording" if recording else "Start recording")
        self.toggle.setIcon(icons.qt_icon("stop" if recording else "record"))

    def _toggle_pressed(self) -> None:
        self._press_ts = time.time()

    def _toggle_clicked(self) -> None:
        if self._rec is None:
            self._start()
        else:
            self._stop()
        self._press_ts = None

    def _start(self) -> None:
        if not recorder.available():
            warn(self, "python-evdev is not installed.\n\nsudo pacman -S python-evdev")
            return
        paths = []
        for i in range(self.devices.count()):
            item = self.devices.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                paths += item.data(ROLE)
        if not paths:
            warn(self, "Tick at least one device to record from.")
            return
        self._count = 0
        self.state.begin_edit()
        self._rec = recorder.MacroRecorder(paths, on_event=self.bus.event.emit, on_error=self.bus.error.emit)
        self._rec.start()
        if not self._rec.started_ok.wait(1.0):
            self._rec.stop()
            self._rec = None
            return                       # _on_error explains why
        self._set_toggle(True)
        self.status.setText("Recording… perform the sequence, then press Stop.")
        self.devices.setEnabled(False)

    def _stop(self) -> None:
        events = self._rec.stop() if self._rec else []
        self._rec = None
        self._set_toggle(False)
        self.devices.setEnabled(True)
        cut = self._press_ts or time.time()
        trimmed = []
        for t, name, value in events:
            if t > cut:
                continue                                  # the click that pressed Stop
            if name.startswith("BTN_") and t >= cut - 0.25:
                continue
            if self._press_ts is None and name in ("KEY_SPACE", "KEY_ENTER") and t >= cut - 0.3:
                continue                                  # Stop was triggered from the keyboard
            trimmed.append((t, name, value))
        fixed = self.fixed_ms.value() if self.fixed.isChecked() else None
        new = core.events_to_steps(trimmed, fixed_delay_ms=fixed)
        if self.raw_text and new:
            self.raw_text = ""                           # replacing the unparsed macro
            self.steps = []
        self.steps += new
        self.status.setText(f"Recorded {len(new)} step(s)." if new else "Nothing was recorded.")
        self._refresh()

    def _on_event(self, item) -> None:
        self._count += 1
        if self._rec is not None:
            self.status.setText(f"Recording… {self._count} event(s)")

    def _on_error(self, msg: str) -> None:
        self.status.setText(f"Could not read the devices: {msg}")
        warn(self, f"Could not read the devices:\n{msg}\n\nCheck Options → Setup check.")

    # ----- editing
    def _refresh(self) -> None:
        self.list.clear()
        for i, step in enumerate(self.steps):
            item = QListWidgetItem(step.label())
            item.setData(ROLE, i)
            self.list.addItem(item)
        self.preview.setPlainText(self.raw_text or core.steps_to_text(self.steps))

    def _edit_wait(self, item) -> None:
        step = self.steps[item.data(ROLE)]
        if step.kind != "wait":
            return
        value, ok = QInputDialog.getInt(self, "Retime wait", "Milliseconds:", int(step.arg), 0, 600000, 10)
        if ok:
            step.arg = value
            self._refresh()

    def _delete_selected(self) -> None:
        drop = {i.data(ROLE) for i in self.list.selectedItems()}
        if drop:
            self.steps = [s for i, s in enumerate(self.steps) if i not in drop]
            self._refresh()

    def _clear(self) -> None:
        self.steps, self.raw_text = [], ""
        self._refresh()

    def _stop_quietly(self) -> None:
        if self._rec is not None:
            self._rec.stop()
            self._rec = None

    def accept(self) -> None:
        self._stop_quietly()
        self.result_text = self.raw_text or core.steps_to_text(self.steps)
        super().accept()

    def reject(self) -> None:
        self._stop_quietly()
        super().reject()


class MappingDialog(QDialog):
    """One mapping: input (recorded from the device) -> output (key, hotkey or macro)."""

    def __init__(self, state: State, device_name: str, mapping: Optional[dict], parent=None,
                 prefill_output: Optional[str] = None, record_input_first: bool = False):
        super().__init__(parent)
        self.state = state
        self.device_name = device_name
        self.info = state.device_info(device_name)
        self.base = copy.deepcopy(mapping) if mapping else {}
        self.inputs = core.mapping_inputs(mapping) if mapping else []
        self._rec: Optional[recorder.ChordRecorder] = None
        self._rec_kind = ""
        self.bus = ChordBus()
        self.bus.done.connect(self._chord_done)
        self.setWindowTitle(f"Mapping — {device_name}")
        self.resize(560, 470)

        lay = QVBoxLayout(self)
        note = QLabel("Remapping pauses when you start recording (so every key reports its real code) "
                      "and resumes when you close this window.")
        note.setWordWrap(True)
        lay.addWidget(note)

        lay.addWidget(QLabel("<b>1. Input</b> — the key, button or combination on this device"))
        row = QHBoxLayout()
        self.input_label = QLabel()
        self.input_label.setFont(mono_font())
        self.rec_in = button("Record…", "record")
        self.rec_in.clicked.connect(self._record_input)
        clear = button("Clear", "remove")
        clear.clicked.connect(self._clear_input)
        row.addWidget(self.input_label, 1)
        row.addWidget(self.rec_in)
        row.addWidget(clear)
        lay.addLayout(row)

        lay.addWidget(QLabel("<b>2. Output</b> — what it should do"))
        self.output = QPlainTextEdit()
        self.output.setFont(mono_font())
        self.output.setMaximumHeight(90)
        self.output.setPlaceholderText("e.g.  KEY_F5   or   hold_keys(KEY_LEFTCTRL, KEY_C)   or a macro")
        lay.addWidget(self.output)
        row = QHBoxLayout()
        self.rec_key = button("Record key / hotkey", "hotkey")
        self.rec_key.clicked.connect(self._record_output_key)
        self.rec_macro = button("Record macro…", "macro")
        self.rec_macro.clicked.connect(self._record_macro)
        mouse = QToolButton()
        mouse.setText("Mouse button")
        mouse.setIcon(icons.qt_icon("mouse"))
        mouse.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        mouse.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(mouse)
        for label, name in MOUSE_BUTTON_CHOICES:
            act = QAction(f"{label}  ({name})", menu)
            act.triggered.connect(lambda _c=False, n=name: self.output.setPlainText(n))
            menu.addAction(act)
        mouse.setMenu(menu)
        row.addWidget(self.rec_key)
        row.addWidget(self.rec_macro)
        row.addWidget(mouse)
        row.addStretch(1)
        lay.addLayout(row)

        row = QHBoxLayout()
        self.lib_btn = QToolButton()
        self.lib_btn.setText("Saved macros")
        self.lib_btn.setIcon(icons.qt_icon("macro"))
        self.lib_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.lib_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.save_lib = button("Save to library…", "new")
        self.save_lib.clicked.connect(self._save_to_library)
        row.addWidget(self.lib_btn)
        row.addWidget(self.save_lib)
        row.addStretch(1)
        lay.addLayout(row)
        self._rebuild_library_menu()

        row = QHBoxLayout()
        self.loop = QComboBox()
        self.loop.addItems(["Run once per press", "Repeat while the key is held", "Toggle: press to start / stop"])
        self.loop_delay = QSpinBox()
        self.loop_delay.setRange(0, 10000)
        self.loop_delay.setSingleStep(10)
        self.loop_delay.setValue(50)
        self.loop_delay.setSuffix(" ms between repeats")
        row.addWidget(self.loop, 1)
        row.addWidget(self.loop_delay)
        lay.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Sends to:"))
        self.target = QComboBox()
        self.target.addItem("Automatic")
        self.target.addItems(core.TARGETS)
        self.hint = QLabel()
        row.addWidget(self.target)
        row.addWidget(self.hint, 1)
        lay.addLayout(row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._ok)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

        symbol = prefill_output if prefill_output is not None else self.base.get("output_symbol", "")
        mode, inner, delay = core.unwrap_loop(symbol)
        self.output.setPlainText(inner)
        self.loop.setCurrentIndex(LOOP_MODES.index(mode))
        if delay:
            self.loop_delay.setValue(delay)
        existing = self.base.get("target_uinput")
        if existing in core.TARGETS:
            self.target.setCurrentIndex(core.TARGETS.index(existing) + 1)
        self.output.textChanged.connect(self._update_hint)
        self.target.currentIndexChanged.connect(self._update_hint)
        self.loop.currentIndexChanged.connect(self._update_hint)
        self._show_input()
        self._update_hint()
        if record_input_first:
            QTimer.singleShot(0, self._record_input)

    # ----- helpers
    def _full_output(self) -> str:
        mode = LOOP_MODES[self.loop.currentIndex()]
        return core.wrap_loop(self.output.toPlainText().strip(), mode, self.loop_delay.value() if mode else 0)

    def _rebuild_library_menu(self) -> None:
        menu = QMenu(self.lib_btn)
        names = self.state.library.names()
        fill_menu(menu, [(n, lambda n=n: self._insert_saved(n)) for n in names]
                  or [("(nothing saved yet)", None)])
        self.lib_btn.setMenu(menu)

    def _insert_saved(self, name: str) -> None:
        out = self.state.library.get(name)
        if out is None:
            return
        mode, inner, delay = core.unwrap_loop(out)
        self.output.setPlainText(inner)
        self.loop.setCurrentIndex(LOOP_MODES.index(mode))
        if delay:
            self.loop_delay.setValue(delay)
        self.status.setText(f"Inserted “{name}”.")

    def _save_to_library(self) -> None:
        out = self._full_output()
        if not out:
            warn(self, "The output is empty, there is nothing to save.")
            return
        name, ok = QInputDialog.getText(self, "Save to library", "Name for this macro:")
        if not ok or not name.strip():
            return
        try:
            try:
                saved = self.state.library.save(name, out)
            except core.LibraryError:
                if not ask(self, f"“{name.strip()}” already exists. Replace it?", default_no=True):
                    return
                saved = self.state.library.save(name, out, overwrite=True)
        except (core.LibraryError, OSError) as exc:
            warn(self, str(exc))
            return
        self._rebuild_library_menu()
        self.status.setText(f"Saved to the library as “{saved}”.")

    def _show_input(self) -> None:
        names = [core.code_name(c) for t, c, _h in self.inputs if t == core.EV_KEY]
        self.input_label.setText(" + ".join(names) if names else "— not set —")

    def _update_hint(self, *_args) -> None:
        self.loop_delay.setEnabled(self.loop.currentIndex() > 0)
        guess = core.guess_target(self.output.toPlainText())
        self.hint.setText(f"→ {guess}" if self.target.currentIndex() == 0 else "")

    def _clear_input(self) -> None:
        self.inputs = []
        self._show_input()

    # ----- recording
    def _record_input(self) -> None:
        if self._rec:
            self._cancel_recording()
            return
        if not recorder.available():
            warn(self, "python-evdev is not installed.\n\nsudo pacman -S python-evdev")
            return
        if not self.info or not self.info.paths:
            warn(self, f"“{self.device_name}” is not connected (or not readable), so its keys can't be recorded.\n"
                       "See Options → Setup check.")
            return
        self._begin("input", self.info.paths, f"Press the key or combination on {self.device_name} now…")

    def _record_output_key(self) -> None:
        if self._rec:
            self._cancel_recording()
            return
        if not recorder.available():
            warn(self, "python-evdev is not installed.\n\nsudo pacman -S python-evdev")
            return
        paths = [p for d in self.state.devices if "keyboard" in d.kinds for p in d.paths]
        if not paths:
            warn(self, "No readable keyboard found. See Options → Setup check.")
            return
        self._begin("output", paths, "Press the key or hotkey you want it to produce…")

    def _begin(self, kind: str, paths: list, message: str) -> None:
        self._rec_kind = kind
        self.state.begin_edit()
        self._rec = recorder.ChordRecorder(paths, self.bus.done.emit, grab=True)
        active = self.rec_in if kind == "input" else self.rec_key
        active.setText("Cancel recording")
        active.setIcon(icons.qt_icon("stop"))
        self.status.setText(message + " (click Cancel recording to abort)")
        self._rec.start()

    def _cancel_recording(self) -> None:
        if self._rec:
            self._rec.cancel()

    def _chord_done(self, result, message: str) -> None:
        kind = self._rec_kind
        self._rec, self._rec_kind = None, ""
        self.rec_in.setText("Record…")
        self.rec_in.setIcon(icons.qt_icon("record"))
        self.rec_key.setText("Record key / hotkey")
        self.rec_key.setIcon(icons.qt_icon("hotkey"))
        if not result:
            self.status.setText(message or "")
            return
        self.status.setText("")
        if kind == "input":
            self.inputs = [(core.EV_KEY, code, h) for code, h in result]
            self._show_input()
        elif kind == "output":
            self.output.setPlainText(core.keys_to_output([core.code_name(c) for c, _h in result]))

    def _record_macro(self) -> None:
        mode, inner, _d = core.unwrap_loop(self.output.toPlainText())
        dlg = MacroDialog(self.state, self.output.toPlainText() if not mode else inner, self)
        if dlg.exec():
            self.output.setPlainText(dlg.result_text)

    # ----- result
    def _ok(self) -> None:
        text = self.output.toPlainText().strip()
        if not self.inputs:
            warn(self, "Record the input first.")
            return
        if not text:
            warn(self, "The output is empty.")
            return
        problem = core.check_macro_syntax(text)
        if problem:
            warn(self, f"The output looks wrong: {problem}.")
            return
        if (len(self.inputs) == 1 and self.inputs[0][:2] == (core.EV_KEY, core.BTN_LEFT_CODE)
                and "BTN_LEFT" not in text):
            if not ask(self, "This swallows your left mouse button without producing a left click anywhere.\n"
                             "You could lock yourself out of clicking.\n\nSave it anyway?", default_no=True):
                return
        self.accept()

    def _stop_recording_sync(self) -> None:
        if self._rec:
            self._rec.cancel()
            self._rec.join(1.5)
            self._rec = None

    def accept(self) -> None:
        self._stop_recording_sync()
        super().accept()

    def reject(self) -> None:
        self._stop_recording_sync()
        super().reject()

    def result_mapping(self) -> dict:
        out = self._full_output()
        target = (core.guess_target(out) if self.target.currentIndex() == 0
                  else core.TARGETS[self.target.currentIndex() - 1])
        return core.make_mapping(self.inputs, out, target, self.base)


class LibraryDialog(QDialog):
    """Manage saved macros: rename, edit the steps, delete."""

    def __init__(self, state: State, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("Macro library")
        self.resize(560, 420)
        lay = QHBoxLayout(self)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._show)
        lay.addWidget(self.list, 1)
        right = QVBoxLayout()
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono_font())
        right.addWidget(QLabel("Saved output (assign it from the right-click menu on any input):"))
        right.addWidget(self.preview, 1)
        for text, fn, ico in (("Rename…", self.rename, "edit"), ("Edit steps…", self.edit_steps, "macro"),
                              ("Delete", self.delete, "delete")):
            b = button(text, ico)
            b.clicked.connect(fn)
            right.addWidget(b)
        close = button("Close", "")
        close.clicked.connect(self.accept)
        right.addWidget(close)
        lay.addLayout(right, 1)
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        for n in self.state.library.names():
            item = QListWidgetItem(n)
            item.setData(ROLE, n)
            self.list.addItem(item)
        self.preview.setPlainText("")

    def selected(self) -> str:
        item = self.list.currentItem()
        return (item.data(ROLE) or "") if item else ""

    def _show(self, *_args) -> None:
        name = self.selected()
        self.preview.setPlainText(self.state.library.get(name) or "" if name else "")

    def rename(self) -> None:
        name = self.selected()
        if not name:
            return
        new, ok = QInputDialog.getText(self, "Rename macro", "New name:", text=name)
        if ok and new.strip():
            try:
                self.state.library.rename(name, new)
            except (core.LibraryError, OSError) as exc:
                warn(self, str(exc))
            self.reload()

    def edit_steps(self) -> None:
        name = self.selected()
        out = self.state.library.get(name) if name else None
        if out is None:
            return
        mode, inner, delay = core.unwrap_loop(out)
        dlg = MacroDialog(self.state, inner, self)
        accepted = bool(dlg.exec())
        self.state.end_edit("", "", False)            # recording pauses remapping; put it back
        if accepted and dlg.result_text.strip():
            self.state.library.save(name, core.wrap_loop(dlg.result_text, mode, delay), overwrite=True)
            self.reload()

    def delete(self) -> None:
        name = self.selected()
        if name and ask(self, f"Delete “{name}” from the library?", default_no=True):
            self.state.library.delete(name)
            self.reload()


class AboutDialog(QDialog):
    def __init__(self, engine_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About InputDeck")
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icons.app_icon().pixmap(96, 96))
        top.addWidget(logo)
        text = QLabel()
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        text.setWordWrap(True)
        text.setText(
            f"<h2>InputDeck {__version__}</h2>"
            "<p>Hotkeys, macro recording and per-app presets for Linux input devices.</p>"
            f"<p>Created by <b>{__author__}</b> &nbsp;·&nbsp; "
            f'<a href="{__author_url__}">{__author_url__}</a><br>'
            f'Project page: <a href="{__url__}">{__url__}</a></p>'
            f"<p>Remapping engine in use: <b>{engine_name}</b></p>"
            '<p style="color:gray">Compatible with presets of '
            '<a href="https://github.com/sezanzeb/input-remapper">input-remapper</a> by sezanzeb. '
            "Auto-switching and macro recording were inspired by Saito's AutoSwitcher/Recorder.</p>")
        top.addWidget(text, 1)
        lay.addLayout(top)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        lay.addWidget(box)


class LogDialog(QDialog):
    """Read-only live view of a log file."""

    def __init__(self, title: str, path: Path, parent=None, tail_lines: int = 500):
        super().__init__(parent)
        self.path, self.tail_lines, self._last = Path(path), tail_lines, None
        self.setWindowTitle(title)
        self.resize(820, 520)
        lay = QVBoxLayout(self)
        self.where = QLabel(f"<small>{html.escape(str(self.path))}</small>")
        lay.addWidget(self.where)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(mono_font())
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.follow = QCheckBox("Follow (refresh every 2 s)")
        self.follow.setChecked(True)
        row.addWidget(self.follow)
        row.addStretch(1)
        for text, fn, ico in (("Refresh", self.reload, "refresh"), ("Copy all", self.copy_all, "duplicate"),
                              ("Open folder", lambda: open_folder(self.path.parent), "other")):
            b = button(text, ico)
            b.clicked.connect(fn)
            row.addWidget(b)
        close = button("Close", "")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(2000)
        self.finished.connect(self.timer.stop)
        self.reload()

    def _tick(self) -> None:
        if self.follow.isChecked():
            self.reload()

    def reload(self, *_args) -> None:
        text = core.read_tail(self.path, self.tail_lines)
        if text == self._last:
            return
        self._last = text
        self.view.setPlainText(text)
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())                    # newest line at the bottom

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self._last or "")


# --------------------------------------------------------------------------- tabs

class MappingsTab(QWidget):
    def __init__(self, state: State, window: "MainWindow"):
        super().__init__()
        self.state, self.window = state, window
        self.mappings: list = []
        self._loading = False
        split = QSplitter(Qt.Orientation.Horizontal)
        outer = QHBoxLayout(self)
        outer.addWidget(split)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("<b>Devices</b>"))
        self.devices = QListWidget()
        ll.addWidget(self.devices, 1)
        refresh = button("Refresh", "refresh")
        refresh.clicked.connect(self.refresh_devices)
        ll.addWidget(refresh)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        banner = QHBoxLayout()
        self.active_label = QLabel()
        self.active_label.setTextFormat(Qt.TextFormat.RichText)
        self.active_label.setWordWrap(True)
        self.show_active = button("Show active preset", "active")
        self.show_active.clicked.connect(self._show_active_clicked)
        banner.addWidget(self.active_label, 1)
        banner.addWidget(self.show_active)
        rl.addLayout(banner)
        row = QHBoxLayout()
        row.addWidget(QLabel("Preset:"))
        self.preset = QComboBox()
        self.preset.setMinimumWidth(180)
        row.addWidget(self.preset)
        for text, fn, ico in (("New", self.new_preset, "new"), ("Duplicate", self.duplicate_preset, "duplicate"),
                              ("Delete", self.delete_preset, "delete")):
            b = button(text, ico)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        rl.addLayout(row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Input", "Output", "Sends to"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemDoubleClicked.connect(lambda _i: self.edit_mapping())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        rl.addWidget(self.table, 1)

        row = QHBoxLayout()
        for text, fn, ico in (("Add mapping…", self.add_mapping, "add"), ("Edit…", self.edit_mapping, "edit"),
                              ("Remove", self.remove_mapping, "remove")):
            b = button(text, ico)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        apply_b = button("Apply preset", "apply")
        apply_b.setToolTip("Start remapping this device with the selected preset")
        apply_b.clicked.connect(self.apply_preset)
        stop_b = button("Stop", "stop")
        stop_b.clicked.connect(self.stop_device)
        row.addWidget(apply_b)
        row.addWidget(stop_b)
        rl.addLayout(row)
        self.auto_apply = QCheckBox("Apply the preset right after saving a mapping")
        self.auto_apply.setChecked(bool(state.settings["apply_after_save"]))
        self.auto_apply.toggled.connect(self._auto_apply_toggled)
        rl.addWidget(self.auto_apply)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

        self.devices.currentItemChanged.connect(self._device_changed)
        self.preset.currentTextChanged.connect(self._preset_changed)

    # ----- device / preset selection
    def device(self) -> str:
        item = self.devices.currentItem()
        return (item.data(ROLE) or "") if item else ""

    def refresh_devices(self) -> None:
        self.state.refresh_devices()
        keep = self.device() or self.state.settings.get("last_device", "")
        self._loading = True
        self.devices.clear()
        connected = {d.name: d for d in self.state.devices}
        for name in self.state.device_names():
            info = connected.get(name)
            item = QListWidgetItem(self._device_label(name, info))
            item.setIcon(icons.qt_icon(device_icon(info.kinds) if info else "other"))
            item.setData(ROLE, name)
            if not info:
                item.setForeground(QBrush(QColor('gray')))
            self.devices.addItem(item)
            if name == keep:
                self.devices.setCurrentItem(item)
        self._loading = False
        if not self.devices.count():
            hint = ("No devices found.\n\n" + ("Your user can't read /dev/input — see Options → Setup check."
                                                if self.state.denied or not recorder.available()
                                                else "Plug something in, or create a preset in input-remapper."))
            item = QListWidgetItem(hint)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.devices.addItem(item)
        elif not self.devices.currentItem():
            self.devices.setCurrentRow(0)
        self._device_changed()

    def _device_changed(self, *_args) -> None:
        if self._loading:
            return
        device = self.device()
        self._loading = True
        self.preset.clear()
        if device:
            self.preset.addItems(self.state.store.presets(device))
            # open on the preset that is actually running, otherwise on "Default"
            i = self.preset.findText(self.active_preset(device))
            if i < 0:
                i = self.preset.findText(core.DEFAULT_PRESET)
            if i >= 0:
                self.preset.setCurrentIndex(i)
            self.state.settings["last_device"] = device
            self.state.save_settings()
        self._loading = False
        self._preset_changed()
        self.refresh_active()

    # ----- "what is running right now"
    def active_preset(self, device: str) -> str:
        return self.state.control.running.get(device, "") if device else ""

    def _device_label(self, name: str, info) -> str:
        active = self.active_preset(name)
        if not info:
            return f"{name}\n(not connected)" + (f"  ·  ● {active}" if active else "")
        return f"{name}\n{info.kind_label}" + (f"  ·  ● {active}" if active else "")

    def refresh_active(self) -> None:
        connected = {d.name: d for d in self.state.devices}
        for i in range(self.devices.count()):
            item = self.devices.item(i)
            name = item.data(ROLE)
            if not name:
                continue
            label = self._device_label(name, connected.get(name))
            if item.text() != label:
                item.setText(label)
            active = self.active_preset(name)
            item.setToolTip(f"Active preset: {active}" if active else "No preset active")
        device, viewed = self.device(), self.preset.currentText()
        active = self.active_preset(device)
        for i in range(self.preset.count()):
            self.preset.setItemIcon(i, icons.qt_icon("active") if self.preset.itemText(i) == active else QIcon())
        dot = f'<span style="color:{GREEN}">●</span>&nbsp;'
        grey = '<span style="color:gray">○</span>&nbsp;'
        if not device:
            text = ""
        elif active and active == viewed:
            text = f"{dot}<b>{html.escape(active)}</b> is <b>active</b> on this device"
        elif active:
            text = (f"{dot}Active now: <b>{html.escape(active)}</b> &nbsp;·&nbsp; you are viewing "
                    f"<b>{html.escape(viewed)}</b> (not active)")
        elif getattr(self.state.control, "kind", "") == "input-remapper":
            text = (f"{grey}Active preset unknown — input-remapper can't report it. "
                    "Press <b>Apply preset</b> to be sure.")
        else:
            text = f"{grey}No preset is active on this device — its keys are not being remapped"
        self.active_label.setText(text)
        self.show_active.setVisible(bool(active) and active != viewed)

    def _show_active_clicked(self) -> None:
        i = self.preset.findText(self.active_preset(self.device()))
        if i >= 0:
            self.preset.setCurrentIndex(i)

    def _preset_changed(self, *_args) -> None:
        if self._loading:
            return
        device, preset = self.device(), self.preset.currentText()
        self.mappings = []
        if device and preset:
            try:
                self.mappings = self.state.store.load(device, preset)
            except core.PresetError as exc:
                warn(self, str(exc))
        self._fill_table()

    def _fill_table(self) -> None:
        self.table.setRowCount(len(self.mappings))
        for r, m in enumerate(self.mappings):
            for c, text in enumerate((core.describe_input(m), core.describe_output(m),
                                      m.get("target_uinput", ""))):
                item = QTableWidgetItem(text)
                if c == 1:
                    item.setFont(mono_font())
                self.table.setItem(r, c, item)

    def _save(self) -> bool:
        try:
            self.state.store.save(self.device(), self.preset.currentText(), self.mappings)
            return True
        except OSError as exc:
            warn(self, f"Could not save the preset: {exc}")
            return False

    # ----- presets
    def new_preset(self) -> None:
        self._create_preset(base=None)

    def duplicate_preset(self) -> None:
        if self.preset.currentText():
            self._create_preset(base=self.preset.currentText())

    def _create_preset(self, base: Optional[str]) -> None:
        device = self.device()
        if not device:
            return
        name, ok = QInputDialog.getText(self, "New preset", "Preset name:",
                                        text=core.DEFAULT_PRESET if not self.state.store.presets(device) else "")
        if not ok or not name.strip():
            return
        try:
            created = self.state.store.create(device, name.strip(), base)
        except (core.PresetError, OSError) as exc:
            warn(self, str(exc))
            return
        self._device_changed()
        self.preset.setCurrentText(created)

    def delete_preset(self) -> None:
        device, preset = self.device(), self.preset.currentText()
        if not preset:
            return
        if preset == core.DEFAULT_PRESET:
            warn(self, "The “Default” preset can't be deleted here.")
            return
        if ask(self, f"Delete preset “{preset}”?\n\nIt is moved to {self.state.paths.trash_dir}, not erased.",
               default_no=True):
            try:
                self.state.store.delete(device, preset)
            except (core.PresetError, OSError) as exc:
                warn(self, str(exc))
            self._device_changed()

    # ----- mappings
    def _ensure_preset(self) -> bool:
        if self.preset.currentText():
            return True
        if not self.device():
            return False
        if ask(self, f"“{self.device()}” has no preset yet. Create “{core.DEFAULT_PRESET}”?"):
            try:
                self.state.store.create(self.device(), core.DEFAULT_PRESET)
            except (core.PresetError, OSError) as exc:
                warn(self, str(exc))
                return False
            self._device_changed()
            return True
        return False

    def add_mapping(self) -> None:
        if self._ensure_preset():
            self._edit(None)

    @staticmethod
    def _editable(m: dict) -> bool:
        return (all(t == core.EV_KEY for t, _c, _h in core.mapping_inputs(m))
                and bool(m.get("output_symbol")))

    def edit_mapping(self) -> None:
        self.edit_row(self.table.currentRow())

    def edit_row(self, row: int, record_input_first: bool = False) -> None:
        if row < 0 or row >= len(self.mappings):
            return
        if not self._editable(self.mappings[row]):
            warn(self, "This is an analog (axis) mapping. InputDeck only edits key/button mappings; "
                       "use input-remapper itself for this one.")
            return
        self._edit(row, record_input_first=record_input_first)

    def _edit(self, row: Optional[int], prefill_output: Optional[str] = None,
              record_input_first: bool = False) -> None:
        device, preset = self.device(), self.preset.currentText()
        dlg = MappingDialog(self.state, device, self.mappings[row] if row is not None else None, self,
                            prefill_output=prefill_output, record_input_first=record_input_first)
        accepted = bool(dlg.exec())
        message = None
        if accepted:
            new = dlg.result_mapping()
            if row is None:
                self.mappings.append(new)
            else:
                self.mappings[row] = new
            if self._save():
                self._fill_table()
            else:
                accepted = False
        message = self.state.end_edit(device, preset, accepted and self.auto_apply.isChecked())
        self.window.status(message or ("Saved." if accepted else ""))
        self.refresh_active()

    def remove_mapping(self) -> None:
        self.remove_row(self.table.currentRow())

    def remove_row(self, row: int) -> None:
        if 0 <= row < len(self.mappings) and ask(
                self, f"Remove the mapping for {core.describe_input(self.mappings[row])}?", default_no=True):
            del self.mappings[row]
            if self._save():
                self._fill_table()
                if self.auto_apply.isChecked():
                    self.apply_preset()

    # ----- right-click menu and the macro library
    def context_entries(self, row: Optional[int]) -> list:
        names = self.state.library.names()
        if row is None or not (0 <= row < len(self.mappings)):
            return [("Add mapping…", self.add_mapping),
                    ("Add mapping with a saved macro",
                     [(n, lambda n=n: self.add_with_macro(n)) for n in names] or [("(nothing saved yet)", None)])]
        return [
            ("Edit…", lambda: self.edit_row(row)),
            ("Change input… (keep the output)", lambda: self.edit_row(row, record_input_first=True)),
            ("sep",),
            ("Assign a saved macro",
             [(n, lambda n=n: self.assign_macro(row, n)) for n in names] or [("(nothing saved yet)", None)]),
            ("Save this output to the macro library…", lambda: self.save_output_to_library(row)),
            ("sep",),
            ("Remove", lambda: self.remove_row(row)),
        ]

    def _context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row >= 0:
            self.table.selectRow(row)
        menu = QMenu(self)
        fill_menu(menu, self.context_entries(row if row >= 0 else None))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def assign_macro(self, row: int, name: str) -> None:
        out = self.state.library.get(name)
        if out is None or not (0 <= row < len(self.mappings)):
            return
        m = self.mappings[row]
        if not self._editable(m):
            warn(self, "This is an analog (axis) mapping and can't be given a macro here.")
            return
        self.mappings[row] = core.make_mapping(core.mapping_inputs(m), out, core.guess_target(out), m)
        if self._save():
            self._fill_table()
            if self.auto_apply.isChecked():
                self.apply_preset()
            self.window.status(f"Assigned “{name}” to {core.describe_input(m)}.")

    def add_with_macro(self, name: str) -> None:
        out = self.state.library.get(name)
        if out is not None and self._ensure_preset():
            self._edit(None, prefill_output=out, record_input_first=True)

    def save_output_to_library(self, row: int) -> None:
        if not (0 <= row < len(self.mappings)):
            return
        out = str(self.mappings[row].get("output_symbol", "")).strip()
        if not out:
            return
        name, ok = QInputDialog.getText(self, "Save to library", "Name for this macro:")
        if not ok or not name.strip():
            return
        try:
            try:
                saved = self.state.library.save(name, out)
            except core.LibraryError:
                if not ask(self, f"“{name.strip()}” already exists. Replace it?", default_no=True):
                    return
                saved = self.state.library.save(name, out, overwrite=True)
        except (core.LibraryError, OSError) as exc:
            warn(self, str(exc))
            return
        self.window.status(f"Saved to the library as “{saved}”.")

    # ----- daemon
    def apply_preset(self) -> None:
        device, preset = self.device(), self.preset.currentText()
        if not (device and preset):
            return
        if core.blocks_left_click(self.mappings) and not ask(
                self, "This preset swallows your left mouse button without producing a click.\n\nApply anyway?",
                default_no=True):
            return
        code, out = self.state.control.start(device, preset)
        skipped = [w for w in getattr(self.state.control, "last_warnings", []) if "skipped" in w] if code == 0 else []
        if code == 0:
            self.window.status(f"Applied “{preset}” to {device}."
                               + (f"  {len(skipped)} mapping(s) skipped." if skipped else ""))
            if skipped:
                warn(self, "Applied, but these mappings can't be used and were skipped:\n\n  •  "
                           + "\n  •  ".join(skipped) + "\n\n(Details: Logs → Engine log)")
        else:
            self.window.status(f"Could not apply: {out or 'input-remapper-control failed'}")
        self.refresh_active()

    def stop_device(self) -> None:
        device = self.device()
        if device:
            code, out = self.state.control.stop(device)
            self.window.status(f"Stopped remapping {device}." if code == 0 else f"Could not stop: {out}")
            self.refresh_active()

    def _auto_apply_toggled(self, on: bool) -> None:
        self.state.settings["apply_after_save"] = on
        self.state.save_settings()


class AutoSwitchTab(QWidget):
    def __init__(self, state: State, window: "MainWindow"):
        super().__init__()
        self.state, self.window = state, window
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.enabled = QCheckBox("Auto-switch presets while this app is running")
        self.enabled.toggled.connect(self._enabled_toggled)
        top.addWidget(self.enabled)
        top.addStretch(1)
        top.addWidget(QLabel("Check every"))
        self.interval = QDoubleSpinBox()
        self.interval.setRange(1.0, 60.0)
        self.interval.setSingleStep(1.0)
        self.interval.setSuffix(" s")
        self.interval.setValue(float(state.settings["interval"]))
        self.interval.valueChanged.connect(self._interval_changed)
        top.addWidget(self.interval)
        lay.addLayout(top)
        self.tray_box = QCheckBox("Keep running in the system tray when the window is closed")
        self.tray_box.setChecked(bool(state.settings["close_to_tray"]))
        self.tray_box.toggled.connect(self._tray_toggled)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_box.setEnabled(False)
            self.tray_box.setToolTip("No system tray found on this desktop")
        lay.addWidget(self.tray_box)
        self.window_box = QCheckBox("Use window state for rules set to “window visible / focused” "
                                    "(turn off: every rule just checks that the program is running)")
        self.window_box.setChecked(bool(state.settings.get("window_rules", True)))
        self.window_box.toggled.connect(self._window_rules_toggled)
        lay.addWidget(self.window_box)
        self.window_status = QLabel(f"Window detection: {windows.describe()}")
        self.window_status.setWordWrap(True)
        lay.addWidget(self.window_status)
        lay.addWidget(QLabel("Per device, the first matching rule (top to bottom) wins; the fallback "
                             "applies when nothing matches. Order matters."))

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Program contains", "While", "Device", "Preset"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemDoubleClicked.connect(lambda _i: self.edit_rule())
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        for text, fn, ico in (("Add rule…", self.add_rule, "add"), ("Edit…", self.edit_rule, "edit"),
                              ("Remove", self.remove_rule, "remove"),
                              ("Move up", lambda: self.move(-1), "up"), ("Move down", lambda: self.move(1), "down")):
            b = button(text, ico)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        reapply = button("Re-apply now", "refresh")
        reapply.setToolTip("Forget what was applied and apply the matching presets again")
        reapply.clicked.connect(self._reapply)
        row.addWidget(reapply)
        lay.addLayout(row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFont(mono_font())
        self.log.setMaximumHeight(160)
        self.active_summary = QLabel()
        self.active_summary.setTextFormat(Qt.TextFormat.RichText)
        self.active_summary.setWordWrap(True)
        lay.addWidget(self.active_summary)
        lay.addWidget(self.log)
        state.log.connect(self.log.appendPlainText)
        self._fill()
        self.enabled.setChecked(bool(state.settings["autoswitch"]))

    def _fill(self) -> None:
        rules = self.state.rules
        self.table.setRowCount(len(rules))
        for r, rule in enumerate(rules):
            text = "(fallback — nothing else matches)" if rule.is_default else rule.process
            when = "—" if rule.is_default else core.WHEN_SHORT.get(rule.when, rule.when)
            for c, value in enumerate((text, when, rule.device, rule.preset)):
                item = QTableWidgetItem(value)
                if rule.device and not self.state.store.exists(rule.device, rule.preset):
                    item.setForeground(QBrush(QColor('red')))
                    item.setToolTip("This preset does not exist")
                self.table.setItem(r, c, item)

    def _commit(self, rules: list) -> None:
        self.state.rules = rules
        try:
            self.state.save_rules()
        except OSError as exc:
            warn(self, f"Could not save the rules: {exc}")
        self._fill()

    def add_rule(self) -> None:
        dlg = RuleDialog(self.state, None, self)
        if dlg.exec():
            self._commit(self.state.rules + [dlg.result_rule()])

    def edit_rule(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.state.rules):
            dlg = RuleDialog(self.state, self.state.rules[row], self)
            if dlg.exec():
                rules = list(self.state.rules)
                rules[row] = dlg.result_rule()
                self._commit(rules)

    def remove_rule(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.state.rules):
            rules = list(self.state.rules)
            del rules[row]
            self._commit(rules)

    def move(self, delta: int) -> None:
        row = self.table.currentRow()
        new = row + delta
        if 0 <= row < len(self.state.rules) and 0 <= new < len(self.state.rules):
            rules = list(self.state.rules)
            rules[row], rules[new] = rules[new], rules[row]
            self._commit(rules)
            self.table.selectRow(new)

    def replace_rules(self, rules: list) -> None:
        self._commit(rules)

    def _enabled_toggled(self, on: bool) -> None:
        self.state.settings["autoswitch"] = on
        self.state.save_settings()
        if on:
            self.state.switcher.start()
        else:
            self.state.switcher.stop()

    def refresh_summary(self) -> None:
        running = self.state.control.running
        if running:
            parts = [f"{html.escape(d)} → <b>{html.escape(p)}</b>" for d, p in sorted(running.items())]
            self.active_summary.setText(f'<span style="color:{GREEN}">●</span>&nbsp;Active now: ' + "; ".join(parts))
        else:
            self.active_summary.setText("○&nbsp;No preset is active right now")

    def sync_from_settings(self) -> None:
        s = self.state.settings
        self.interval.setValue(float(s["interval"]))
        self.tray_box.setChecked(bool(s["close_to_tray"]))
        self.window_box.setChecked(bool(s.get("window_rules", True)))
        self.enabled.setChecked(bool(s["autoswitch"]))

    def _window_rules_toggled(self, on: bool) -> None:
        self.state.settings["window_rules"] = on
        self.state.save_settings()
        self.state.switcher.use_windows = on
        self.state.switcher.reapply()

    def _tray_toggled(self, on: bool) -> None:
        self.state.settings["close_to_tray"] = on
        self.state.save_settings()

    def _interval_changed(self, value: float) -> None:
        self.state.settings["interval"] = value
        self.state.switcher.interval = value
        self.state.save_settings()

    def _reapply(self) -> None:
        self.state.switcher.reapply()
        self.state.log.emit("re-applying on the next check")


# --------------------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self, state: State):
        super().__init__()
        self.state = state
        self._quitting = False
        self.setWindowTitle(f"InputDeck {__version__} — by {__author__}")
        self.setWindowIcon(icons.app_icon())
        self.resize(980, 640)
        self.tabs = QTabWidget()
        self.mappings_tab = MappingsTab(state, self)
        self.auto_tab = AutoSwitchTab(state, self)
        self.tabs.addTab(self.mappings_tab, icons.qt_icon("keyboard"), "Mappings")
        self.tabs.addTab(self.auto_tab, icons.qt_icon("switch"), "Auto-switch")
        self.setCentralWidget(self.tabs)

        menu = self.menuBar().addMenu(MENU_TITLES[0])
        for text, fn in (("Macro library…", self.show_library),
                         ("Import rules from Saito's config.txt…", self.import_saito),
                         ("Repair stale device IDs for this device…", self.repair_hashes),
                         ("Setup check…", self.show_setup),
                         ("Reset to defaults…", self.reset_to_defaults)):
            act = QAction(text, self)
            act.triggered.connect(fn)
            menu.addAction(act)
        engine_menu = menu.addMenu("Remapping &engine")
        self.engine_actions = {}
        for key, text in backends.CHOICES:
            act = QAction(text, self)
            act.setCheckable(True)
            act.setChecked(self.state.settings.get("backend", "auto") == key)
            act.triggered.connect(lambda _c=False, k=key: self._pick_backend(k))
            engine_menu.addAction(act)
            self.engine_actions[key] = act
        menu.addSeparator()
        quit_act = QAction("&Quit", self)
        quit_act.setShortcut(QKeySequence("Ctrl+Q"))
        quit_act.triggered.connect(self.quit)
        menu.addAction(quit_act)
        logs_menu = self.menuBar().addMenu(MENU_TITLES[1])
        for text, fn in (("Engine log…", lambda: LogDialog("Engine log", self.state.paths.data_dir / "engine.log",
                                                            self).exec()),
                         ("Auto-switch && app log…", lambda: LogDialog("Auto-switch & app log",
                                                                      self.state.log_file, self).exec()),
                         ("Open log folder", lambda: open_folder(self.state.paths.data_dir))):
            act = QAction(text, self)
            act.triggered.connect(lambda _c=False, f=fn: f())
            logs_menu.addAction(act)
        help_menu = self.menuBar().addMenu(MENU_TITLES[2])
        for text, fn in (("InputDeck on GitHub", lambda: open_url(__url__)),
                         ("Report a problem…", lambda: open_url(__url__ + "/issues/new/choose")),
                         (f"About InputDeck (by {__author__})", self.show_about)):
            act = QAction(text, self)
            act.triggered.connect(lambda _c=False, f=fn: f())
            help_menu.addAction(act)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(icons.app_icon(), self)
            tray_menu = QMenu()
            show = QAction("Show InputDeck", self)
            show.triggered.connect(self.show_normal)
            q = QAction("Quit", self)
            q.triggered.connect(self.quit)
            tray_menu.addAction(show)
            tray_menu.addAction(q)
            self.tray.setContextMenu(tray_menu)
            self.tray.activated.connect(self._tray_clicked)
            self.tray.show()

        state.control.refresh_running()                  # the engine may have outlived the last GUI
        state.refresh_devices()
        self.mappings_tab.refresh_devices()
        self.auto_tab.refresh_summary()
        state.log.connect(self._on_state_log)
        self._active_timer = QTimer(self)
        self._active_timer.timeout.connect(self._poll_active)
        self._active_timer.start(3000)
        QTimer.singleShot(300, self._startup_check)

    def _on_state_log(self, _text: str) -> None:
        self._poll_active()

    def _poll_active(self) -> None:
        self.state.control.refresh_running()
        self.mappings_tab.refresh_active()
        self.auto_tab.refresh_summary()

    def _startup_check(self) -> None:
        failed = [w for ok, w, _h in core.setup_report(self.state.control, self.state.paths)
                  if not ok and not w.startswith(core.OPTIONAL)]
        if failed:
            self.statusBar().showMessage("Setup incomplete: " + "; ".join(failed)
                                         + "  (Options → Setup check)")

    def status(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000)

    # ----- tray / lifecycle
    def show_normal(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_clicked(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_normal()

    def closeEvent(self, event) -> None:
        if (self.tray and not self._quitting and self.state.settings["close_to_tray"]
                and self.state.switcher.running):
            event.ignore()
            self.hide()
            self.tray.showMessage("InputDeck", "Still running in the tray; auto-switch is active.",
                                  QSystemTrayIcon.MessageIcon.Information, 3000)
            return
        self.quit()
        event.accept()

    def quit(self) -> None:
        self._quitting = True
        if self.state.switcher.running:
            self.state.switcher.stop()
        QApplication.quit()

    # ----- menu actions
    def import_saito(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Saito's config.txt", str(Path.home()),
                                              "Text files (*.txt);;All files (*)")
        if not path:
            return
        rules = core.load_rules(Path(path))
        if not rules:
            warn(self, "No rules found in that file (expected lines like  process|device|preset).")
            return
        if ask(self, f"Replace your current {len(self.state.rules)} rule(s) with the {len(rules)} from this file?"):
            self.auto_tab.replace_rules(rules)
            self.tabs.setCurrentWidget(self.auto_tab)

    def repair_hashes(self) -> None:
        device = self.mappings_tab.device()
        if not device:
            return
        nodes = recorder.live_nodes(device)
        if not nodes:
            warn(self, f"“{device}” isn't connected or isn't readable, so there is nothing to compare against.")
            return
        fixes, unresolved, pending = 0, [], {}
        for preset in self.state.store.presets(device):
            try:
                mappings = self.state.store.load(device, preset)
            except core.PresetError:
                continue
            fixed, changes, bad = core.repair_origin_hashes(copy.deepcopy(mappings), nodes)
            if changes:
                pending[preset] = fixed
                fixes += len(changes)
            unresolved += [f"{preset}: {b}" for b in bad]
        if not fixes and not unresolved:
            QMessageBox.information(self, "InputDeck", "All device IDs match the connected hardware.")
            return
        text = f"{fixes} input(s) in {len(pending)} preset(s) point at a device node that no longer exists " \
               "and can be repointed. Backups are kept."
        if unresolved:
            text += "\n\nCan't decide automatically (re-record these):\n" + "\n".join(unresolved[:15])
        if pending and ask(self, text + "\n\nRepair now?"):
            for preset, mappings in pending.items():
                self.state.store.save(device, preset, mappings)
            self.mappings_tab._preset_changed()
            self.status(f"Repaired {fixes} input(s).")
        elif not pending:
            warn(self, text)

    def reset_to_defaults(self) -> None:
        if not ask(self, "This will:\n"
                         "  •  stop all remapping and give your devices back to the system\n"
                         "  •  stop the built-in engine process (it restarts when needed)\n"
                         "  •  reset InputDeck's own settings (engine choice, check interval, tray option…) "
                         "to their defaults; the old file is kept as settings.json.bak\n\n"
                         "Your presets, auto-switch rules, macro library and backups are NOT touched.\n\n"
                         "Continue?", "Reset to defaults", default_no=True):
            return
        self.state.switcher.pause()
        try:
            lines = recovery.reset(self.state.paths, self.state.control)
        finally:
            self.state.apply_default_settings()
            self.state.switcher.resume()
        self.mappings_tab.auto_apply.setChecked(bool(self.state.settings["apply_after_save"]))
        self.auto_tab.sync_from_settings()
        for key, act in self.engine_actions.items():
            act.setChecked(key == self.state.settings["backend"])
        self.mappings_tab.refresh_devices()
        self.status("Reset to defaults.")
        QMessageBox.information(self, "Reset to defaults", "\n".join(lines))

    def show_library(self) -> None:
        LibraryDialog(self.state, self).exec()

    def engine_name(self) -> str:
        return backends.label(self.state.control)

    def _pick_backend(self, choice: str) -> None:
        self.state.set_backend(choice)
        for key, act in self.engine_actions.items():
            act.setChecked(key == choice)
        self.status(f"Engine: {self.engine_name()}")
        self.refresh_after_engine_change()

    def refresh_after_engine_change(self) -> None:
        self.mappings_tab.refresh_devices()
        QTimer.singleShot(200, self._startup_check)

    def show_about(self) -> None:
        AboutDialog(self.engine_name(), self).exec()

    def show_setup(self) -> None:
        rows = []
        for ok, what, hint in core.setup_report(self.state.control, self.state.paths):
            mark = "✔" if ok else ("–" if what.startswith(core.OPTIONAL) else "✘")
            what = html.escape(what)
            extra = "" if ok or not hint else f"<br>&nbsp;&nbsp;&nbsp;&nbsp;<code>{hint}</code>"
            rows.append(f"{mark}&nbsp; {what}{extra}")
        QMessageBox.information(self, "Setup check", "<br>".join(rows))


# --------------------------------------------------------------------------- entry

def _install_excepthook() -> None:
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):       # Ctrl+C that surfaced mid-callback
            QApplication.quit()
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        print(text, file=sys.stderr)
        if QApplication.instance():
            QMessageBox.critical(None, "InputDeck — unexpected error", text[-1500:])
    sys.excepthook = hook


def run(paths: core.Paths, rules_file: Path, start_hidden: bool = False) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("InputDeck")
    app.setDesktopFileName("inputdeck")                   # matches inputdeck.desktop / its icon
    app.setWindowIcon(icons.app_icon())
    _install_excepthook()
    state = State(paths, rules_file)
    window = MainWindow(state)
    if window.tray:
        app.setQuitOnLastWindowClosed(False)

    # Qt's event loop never lets Python run signal handlers by itself: a periodic no-op
    # timer gives the interpreter a chance, so Ctrl+C in the terminal / SIGTERM quit cleanly.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: window.quit())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(200)

    if not (start_hidden and window.tray):
        window.show()
    return app.exec()
