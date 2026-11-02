"""Device discovery and recording via python-evdev (no Qt in here).

Reading /dev/input/event* needs the `input` group (or root). While a preset is being
injected by input-remapper the real device is grabbed by its daemon, so callers must
stop injection for that device before recording (the GUI does this).
"""
from __future__ import annotations

import re
import selectors
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import core

try:
    import evdev
    from evdev import ecodes
except ImportError:  # GUI shows a setup hint instead of crashing
    evdev = None
    ecodes = None


def available() -> bool:
    return evdev is not None


@dataclass
class DeviceInfo:
    """One input-remapper *device group*: every /dev/input node of one physical device.

    `name` is the group key input-remapper uses for preset folders and for
    `input-remapper-control --device`.
    """
    name: str
    kinds: set = field(default_factory=set)
    paths: list = field(default_factory=list)       # nodes that can emit keys/buttons
    all_paths: list = field(default_factory=list)   # every node in the group
    names: list = field(default_factory=list)       # evdev names of those nodes

    @property
    def kind_label(self) -> str:
        labels = {"media": "media buttons", "buttons": "buttons"}
        return ", ".join(labels.get(k, k) for k in sorted(self.kinds)) if self.kinds else "other"


# Devices input-remapper itself never offers (subset of its denylist).
DENYLIST = [r"Power Button", r"Sleep Button", r"Video Bus", r"Lid Switch", r"PC Speaker",
            r"Eee PC WMI hotkeys", r".*YubiKey.*"]


def _denied(name: str) -> bool:
    return any(re.match(p, name, re.IGNORECASE) for p in DENYLIST)


# Codes that headsets, remotes and "consumer control" interfaces report.
MEDIA_CODES = {113, 114, 115, 128, 161, 163, 164, 165, 166, 167, 168, 200, 201, 207, 208, 209, 248}
GENERIC_KINDS = {"media", "buttons"}


def _classify(caps: dict) -> set:
    """keyboard / mouse / gamepad, or "media" (headset & remote buttons) / "buttons" (anything
    else with keys but no letters, e.g. a macro keypad)."""
    keys = set(caps.get(ecodes.EV_KEY, []))
    low = {k for k in keys if 0 < k < 0x100}
    kinds = set()
    if ecodes.BTN_LEFT in keys and ecodes.EV_REL in caps:
        kinds.add("mouse")
    if ecodes.KEY_A in keys:
        kinds.add("keyboard")
    if ecodes.EV_ABS in caps and any(0x120 <= k < 0x150 for k in keys):
        kinds.add("gamepad")
    if not kinds and low:
        kinds.add("media" if len(low & MEDIA_CODES) * 2 >= len(low) else "buttons")
    return kinds


def unique_key(dev) -> str:
    """Same recipe as input-remapper's get_unique_key(): bus, vendor, product, uniq and the
    phys prefix, so all nodes of one hardware device (keyboard + consumer control + mouse
    interface ...) end up in one group."""
    info = dev.info
    phys = (dev.phys or "").split("/")[0] or "-"
    return f"{info.bustype}_{info.vendor}_{info.product}_{dev.uniq or ''}_{phys}"


def _event_number(path: str) -> int:
    m = re.search(r"(\d+)$", path)
    return int(m.group(1)) if m else 0


def list_input_devices() -> tuple:
    """Returns (device groups, number of nodes we were not allowed to open).

    Grouping and naming follow input-remapper: nodes with the same unique key form a group,
    the group is named after its shortest node name, and duplicates get " 2", " 3" suffixes.
    """
    if evdev is None:
        return [], 0
    groups: dict = {}
    denied = 0
    for path in sorted(evdev.list_devices(), key=_event_number):
        try:
            dev = evdev.InputDevice(path)
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        try:
            name = dev.name
            caps = dev.capabilities(absinfo=False)
            key = unique_key(dev)
        finally:
            dev.close()
        if _denied(name) or "input-remapper" in name.lower():
            continue
        g = groups.setdefault(key, {"names": [], "paths": [], "all": [], "kinds": set()})
        g["names"].append(name)
        g["all"].append(path)
        if ecodes.EV_KEY in caps:
            g["paths"].append(path)
        g["kinds"] |= _classify(caps)

    result, seen = [], {}
    for g in groups.values():
        if not g["kinds"]:
            continue
        if g["kinds"] - GENERIC_KINDS:                 # a keyboard's consumer-control node etc.
            g["kinds"] -= GENERIC_KINDS
        base = sorted(g["names"], key=len)[0]
        seen[base] = seen.get(base, 0) + 1
        key = base if seen[base] == 1 else f"{base} {seen[base]}"
        result.append(DeviceInfo(key, g["kinds"], g["paths"], g["all"], g["names"]))
    return sorted(result, key=lambda d: d.name.lower()), denied


def live_nodes(group_name: str) -> list:
    """[{"hash", "codes", "path"}] for every event node of a device group (for hash repair)."""
    out = []
    if evdev is None:
        return out
    devices, _denied_count = list_input_devices()
    group = next((d for d in devices if d.name == group_name), None)
    for path in (group.all_paths if group else []):
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        try:
            caps = dev.capabilities(absinfo=False)
            out.append({"hash": core.origin_hash(caps, dev.name), "path": path,
                        "codes": {(t, c) for t, codes in caps.items() for c in codes}})
        finally:
            dev.close()
    return out


def _open_nodes(paths: list, grab: bool):
    """Open and register nodes. Returns (selector, [(device, hash)], problems)."""
    sel = selectors.DefaultSelector()
    opened, problems = [], []
    for path in paths:
        try:
            dev = evdev.InputDevice(path)
        except OSError as exc:
            problems.append(f"{path}: {exc.strerror or exc}")
            continue
        if grab:
            try:
                dev.grab()
            except OSError as exc:
                problems.append(f"{path}: could not grab ({exc.strerror or exc}) — "
                                "is input-remapper still injecting this device?")
                dev.close()
                continue
        ohash = core.origin_hash(dev.capabilities(absinfo=False), dev.name)
        sel.register(dev, selectors.EVENT_READ)
        opened.append((dev, ohash))
    return sel, opened, problems


def _close_nodes(sel, opened, grabbed: bool) -> None:
    for dev, _h in opened:
        try:
            sel.unregister(dev)
        except (KeyError, ValueError):
            pass
        if grabbed:
            try:
                dev.ungrab()
            except OSError:
                pass
        dev.close()
    sel.close()


def _drain(dev):
    """Yield pending events; a vanished device just ends the iteration."""
    try:
        yield from dev.read()
    except BlockingIOError:
        return
    except OSError:
        return


class ChordRecorder(threading.Thread):
    """Capture one key / button / combination from a device.

    Done as soon as everything pressed has been released again. The device is grabbed
    exclusively so the press doesn't also reach the desktop.
    on_done(result, message): result is [(code, origin_hash)] in press order, or None.
    """

    def __init__(self, paths: list, on_done: Callable, grab: bool = True, timeout: float = 15.0):
        super().__init__(name="inputdeck-chord", daemon=True)
        self.paths, self.on_done, self.grab, self.timeout = paths, on_done, grab, timeout
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        sel, opened, problems = _open_nodes(self.paths, self.grab)
        result, message = None, ""
        try:
            if not opened:
                message = "; ".join(problems) or "no device nodes to read"
                return
            pressed: list = []
            down: set = set()
            deadline = time.monotonic() + self.timeout
            hash_of = {id(d): h for d, h in opened}
            while not self._cancel.is_set() and time.monotonic() < deadline:
                for key, _mask in sel.select(0.1):
                    dev = key.fileobj
                    for e in _drain(dev):
                        if e.type != ecodes.EV_KEY or e.value not in (0, 1) or e.code == 0:
                            continue
                        item = (e.code, hash_of[id(dev)])
                        if e.value == 1:
                            if item not in pressed:
                                pressed.append(item)
                            down.add(item)
                        else:
                            down.discard(item)
                            if pressed and not down:
                                result = pressed
                                return
            message = "cancelled" if self._cancel.is_set() else "timed out — nothing was pressed"
        finally:
            _close_nodes(sel, opened, self.grab)
            self.on_done(result, message)


class MacroRecorder(threading.Thread):
    """Passively record key/button presses (not grabbed, so clicks still work)."""

    def __init__(self, paths: list, on_event: Optional[Callable] = None,
                 on_error: Optional[Callable] = None):
        super().__init__(name="inputdeck-macro", daemon=True)
        self.paths, self.on_event, self.on_error = paths, on_event, on_error
        self.events: list = []        # (timestamp, key name, 1|0)
        self._stop_flag = threading.Event()
        self.started_ok = threading.Event()

    def stop(self) -> list:
        self._stop_flag.set()
        self.join(timeout=3)
        return list(self.events)

    def run(self) -> None:
        sel, opened, problems = _open_nodes(self.paths, grab=False)
        try:
            if not opened:
                if self.on_error:
                    self.on_error("; ".join(problems) or "no device nodes to read")
                return
            self.started_ok.set()
            while not self._stop_flag.is_set():
                for key, _mask in sel.select(0.05):
                    for e in _drain(key.fileobj):
                        if e.type == ecodes.EV_KEY and e.value in (0, 1) and e.code != 0:
                            item = (e.timestamp(), core.code_name(e.code), e.value)
                            self.events.append(item)
                            if self.on_event:
                                self.on_event(item)
        finally:
            _close_nodes(sel, opened, grabbed=False)
