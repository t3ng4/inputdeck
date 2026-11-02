"""Command line entry: `python3 -m inputdeck [--daemon | --check | --tray]`."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

from . import __author__, __author_url__, __version__, backends, core, recovery, windows


def run_daemon(paths: core.Paths, rules_file: Path) -> int:
    """Headless auto-switcher (for a systemd user service). Reloads rules when they change."""
    settings = core.load_settings(paths)
    control = backends.make_control(settings, paths)
    store = core.PresetStore(paths)
    cache = {"mtime": None, "rules": []}

    def get_rules():
        try:
            mtime = rules_file.stat().st_mtime
        except OSError:
            return cache["rules"]
        if mtime != cache["mtime"]:
            cache["mtime"], cache["rules"] = mtime, core.load_rules(rules_file)
        return cache["rules"]

    def log(msg: str) -> None:
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    switcher = core.Switcher(control, store, get_rules, log=log,
                             interval=float(settings["interval"]),
                             notify=core.send_notification if settings["notify"] else None,
                             windows=windows.XpropProvider().snapshot)
    switcher.use_windows = bool(settings.get("window_rules", True))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log(f"rules: {rules_file}")
    switcher.start()
    stop.wait()
    switcher.stop()
    return 0


def run_check(paths: core.Paths) -> int:
    control = backends.make_control(core.load_settings(paths), paths)
    print(f"engine: {backends.label(control)}")
    report = core.setup_report(control, paths)
    for ok, what, hint in report:
        tag = "[ ok ] " if ok else ("[ -- ] " if what.startswith(core.OPTIONAL) else "[FAIL] ")
        print(tag + what + ("" if ok or not hint else f"\n         → {hint}"))
    return 0 if all(ok for ok, what, _h in report if not what.startswith(core.OPTIONAL)) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="inputdeck",
                                 description="GUI for input-remapper: hotkeys, macro recording, per-app presets")
    ap.add_argument("--daemon", action="store_true", help="run only the auto-switcher, no GUI")
    ap.add_argument("--check", action="store_true", help="print a setup checklist and exit")
    ap.add_argument("--tray", action="store_true", help="start hidden in the system tray")
    ap.add_argument("--engine", action="store_true", help="run the built-in remapping engine (used internally)")
    ap.add_argument("--engine-stop", action="store_true", help="stop a running built-in engine")
    ap.add_argument("--version", action="version",
                    version=f"InputDeck {__version__} by {__author__} ({__author_url__})")
    ap.add_argument("--reset", action="store_true",
                    help="stop all remapping and the built-in engine and reset InputDeck's settings "
                         "(presets, rules and macros are never touched)")
    ap.add_argument("--rules", type=Path, help="rules file (default: ~/.config/inputdeck/rules.txt)")
    args = ap.parse_args(argv)
    paths = core.Paths.default()
    rules_file = args.rules.expanduser() if args.rules else paths.rules_file
    if args.engine:
        from . import engine
        return engine.run_engine(paths)
    if args.engine_stop:
        from . import engine
        engine.BuiltinControl(paths).shutdown_engine()
        return 0
    if args.reset:
        control = backends.make_control(core.load_settings(paths), paths)
        print("\n".join(recovery.reset(paths, control)))
        return 0
    if args.check:
        return run_check(paths)
    if args.daemon:
        return run_daemon(paths, rules_file)
    from . import gui
    return gui.run(paths, rules_file, start_hidden=args.tray)


if __name__ == "__main__":
    sys.exit(main())
