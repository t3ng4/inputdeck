"""Recovery: put InputDeck back into a clean, default state when something misbehaves.

Never touches your data: presets, auto-switch rules, the macro library and the backups stay
exactly as they are. It only
  * stops all remapping (devices go back to the system),
  * stops the built-in engine process (it restarts on demand),
  * removes a stale engine socket,
  * resets InputDeck's own settings file to defaults (the old one is kept as settings.json.bak).
Also available without the GUI:  inputdeck --reset
"""
from __future__ import annotations

import os
import shutil
import signal
import time
from pathlib import Path
from typing import Callable

from . import core, engine

PRESERVED = "Your presets, rules, macro library and backups were not touched."


def find_engine_pids(proc_root: Path = Path("/proc")) -> list:
    """PIDs of running `python -m inputdeck --engine` processes (never our own)."""
    me, found = os.getpid(), []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        for i in range(len(argv) - 2):
            if argv[i:i + 3] == ["-m", "inputdeck", "--engine"]:
                found.append(int(entry.name))
                break
        else:
            if argv and Path(argv[0]).name == "inputdeck" and "--engine" in argv[1:]:
                found.append(int(entry.name))
    return found


def stop_engine_processes(timeout: float = 3.0, find: Callable = find_engine_pids) -> list:
    """SIGTERM every engine process, SIGKILL whatever is still there after `timeout`.
    The kernel releases every device grab when the process dies."""
    pids = find()
    if not pids:
        return []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.05)
    stuck = [p for p in pids if _alive(p)]
    for pid in stuck:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return [f"Stopped the built-in engine ({len(pids)} process{'es' if len(pids) != 1 else ''}"
            + (f", {len(stuck)} had to be killed)" if stuck else ")")]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                     # a finished-but-unreaped child is a zombie: not alive
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def reset(paths: core.Paths, control, reset_settings: bool = True,
          find: Callable = find_engine_pids) -> list:
    """Returns human-readable lines describing what was done."""
    done = []
    try:
        code, out = control.stop_all()
        done.append("Stopped all remapping." if code == 0 else f"Could not stop remapping cleanly ({out}).")
    except Exception as exc:                 # recovery must keep going whatever is broken
        done.append(f"Could not ask the engine to stop ({exc}).")
    done += stop_engine_processes(find=find)
    try:
        sock = engine.socket_path()
        if sock.exists():
            sock.unlink()
            done.append("Removed a stale engine socket.")
    except OSError:
        pass
    if reset_settings:
        try:
            if paths.settings_file.is_file():
                shutil.copy2(paths.settings_file, paths.settings_file.with_name(paths.settings_file.name + ".bak"))
            core.save_settings(paths, dict(core.DEFAULT_SETTINGS))
            done.append("Reset InputDeck's settings to defaults (the old file is kept as settings.json.bak).")
        except OSError as exc:
            done.append(f"Could not reset the settings file ({exc}).")
    done.append(PRESERVED)
    return done
