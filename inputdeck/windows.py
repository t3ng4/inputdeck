"""Window awareness for auto-switching: is a program's window visible, minimized, focused?

Works through the standard `xprop` tool and the EWMH properties window managers publish:
  * X11 sessions: every window.
  * Wayland sessions: windows of X11 programs (XWayland), which includes games run through
    Wine/Proton. Windows of native Wayland programs are not visible to other programs by design.

A program hidden in the system tray has no mapped window, so it simply does not appear in the
list: "visible" and "focused" rules do not match it.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import core

ALL_DESKTOPS = 0xFFFFFFFF


@dataclass
class Window:
    id: str
    pid: int = 0
    classes: tuple = ()                 # WM_CLASS (instance, class); Wine games put the .exe name here
    title: str = ""
    minimized: bool = False
    on_current_desktop: bool = True
    active: bool = False


Interesting = Callable[[int, tuple, str], bool]   # (pid, window classes, title) -> worth querying?


def _after_equals(text: str, name: str) -> Optional[str]:
    """Value part of an `xprop` line `NAME(TYPE) = value` / `NAME(TYPE): value`, or None."""
    m = re.search(rf"^{re.escape(name)}\([^)]*\)[ \t]*[=:][ \t]*(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _window_ids(value: Optional[str]) -> list:
    return re.findall(r"0x[0-9a-fA-F]+", value or "")


def _quoted(value: Optional[str]) -> list:
    return re.findall(r'"((?:[^"\\]|\\.)*)"', value or "")


class XpropProvider:
    """Reads the window list through `xprop`. Static facts (pid, class, title) are cached per window;
    only windows that a rule could be about are queried for their changing state."""

    name = "xprop (X11 / XWayland)"

    def __init__(self, runner: Callable = core._run, which: Callable = shutil.which,
                 environ: Optional[dict] = None):
        self.runner, self.which = runner, which
        self.environ = environ if environ is not None else os.environ
        self._static: dict = {}

    def available(self) -> bool:
        return bool(self.which("xprop")) and bool(self.environ.get("DISPLAY"))

    def _xprop(self, *args: str) -> Optional[str]:
        code, out = self.runner(["xprop", *args], 2.0)
        return out if code == 0 else None

    def snapshot(self, interesting: Optional[Interesting] = None) -> Optional[list]:
        """Windows on screen, or None when window detection isn't possible here."""
        if not self.available():
            return None
        root = self._xprop("-root", "_NET_CLIENT_LIST", "_NET_ACTIVE_WINDOW", "_NET_CURRENT_DESKTOP")
        if root is None:
            return None
        ids = _window_ids(_after_equals(root, "_NET_CLIENT_LIST"))
        if _after_equals(root, "_NET_CLIENT_LIST") is None:
            return None                                      # window manager without EWMH support
        active_ids = _window_ids(_after_equals(root, "_NET_ACTIVE_WINDOW"))
        active = active_ids[0].lower() if active_ids else ""
        current_raw = _after_equals(root, "_NET_CURRENT_DESKTOP")
        current = int(current_raw) if current_raw and current_raw.isdigit() else None

        self._static = {wid: self._static[wid] for wid in ids if wid in self._static}   # forget closed windows
        windows = []
        for wid in ids:
            info = self._static.get(wid)
            if info is None:
                info = self._static[wid] = self._read_static(wid)
            if info is None:
                continue
            pid, classes, title = info
            if interesting is not None and not interesting(pid, classes, title):
                continue
            state = self._read_state(wid)
            if state is None or state == "withdrawn":
                continue
            minimized, desktop = state
            on_current = (desktop is None or desktop == ALL_DESKTOPS or current is None or desktop == current)
            windows.append(Window(wid, pid, classes, title, minimized, on_current, wid.lower() == active))
        return windows

    def _read_static(self, wid: str) -> Optional[tuple]:
        out = self._xprop("-id", wid, "_NET_WM_PID", "WM_CLASS", "_NET_WM_NAME")
        if out is None:
            return None
        pid_raw = _after_equals(out, "_NET_WM_PID")
        pid = int(pid_raw) if pid_raw and pid_raw.isdigit() else 0
        classes = tuple(_quoted(_after_equals(out, "WM_CLASS")))
        title = (_quoted(_after_equals(out, "_NET_WM_NAME")) or [""])[0]
        return pid, classes, title

    def _read_state(self, wid: str):
        """(minimized, desktop) for a mapped window; "withdrawn" if it's not shown at all; None on error."""
        out = self._xprop("-id", wid, "_NET_WM_STATE", "_NET_WM_DESKTOP", "WM_STATE")
        if out is None:
            return None
        atoms = set(re.findall(r"_NET_WM_STATE_\w+", _after_equals(out, "_NET_WM_STATE") or ""))
        wm_state = re.search(r"window state:\s*(\w+)", out)
        wm_state = wm_state.group(1).lower() if wm_state else ""
        if wm_state == "withdrawn":
            return "withdrawn"
        desktop_raw = _after_equals(out, "_NET_WM_DESKTOP")
        desktop = int(desktop_raw) if desktop_raw and desktop_raw.isdigit() else None
        return ("_NET_WM_STATE_HIDDEN" in atoms or wm_state == "iconic"), desktop


def default_provider() -> Optional[XpropProvider]:
    provider = XpropProvider()
    return provider if provider.available() else None


def describe() -> str:
    """One line for the UI / setup check."""
    if not shutil.which("xprop"):
        return "not available: `xprop` is not installed"
    if not os.environ.get("DISPLAY"):
        return "not available: no X11/XWayland display in this environment"
    session = os.environ.get("XDG_SESSION_TYPE", "")
    return ("available (X11 session: all windows)" if session == "x11"
            else "available (Wayland session: windows of X11 programs, such as Wine/Proton games)"
            if session == "wayland" else "available (via xprop)")
