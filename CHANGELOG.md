# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-11-01

### Added
- **Window-aware auto-switch rules.** Each rule now has a *While* setting: *running* (as before), *window
  visible* (on screen, not minimized, not hidden in the tray, on the current desktop) or *window focused*.
  Use it to give a game its preset only while you can see it and fall back to your desktop preset while
  it is minimized or sitting in the tray.
- A switch on the Auto-switch tab to turn window checks off, plus a status line showing whether window
  detection works. Setup check / `--check` report it as an optional item.
- `rules.txt` takes an optional fourth field (`process|device|preset|visible`); existing files and Saito's
  `config.txt` format work unchanged.
- `install.sh` offers to install `xprop` (`xorg-xprop`, `x11-utils` or `xorg-x11-utils`).

### Notes
- Window detection uses `xprop`: all windows on X11, and windows of X11 programs (including Wine/Proton
  games) on Wayland. Native Wayland windows can't be seen by other programs; without detection, window
  rules act like "running" and the log says so.

## [0.3.0] - 2026-10-31

### Added
- The preset that is actually running is now shown everywhere: a banner above the mappings, a green
  marker on the preset list and in the device list, and a summary on the Auto-switch tab. InputDeck opens
  on the running preset instead of "Default".
- **Logs** menu: live viewers for the engine log and the auto-switch/app log, copy-all, open log folder.
- **Help** menu: link to the GitHub page and to the issue tracker.
- A warning (dialog on manual Apply, note in the auto-switch log) when mappings had to be skipped, e.g. a
  broken macro, instead of failing silently.
- **Reset to defaults** (menu, or `inputdeck --reset`): stops all remapping and the built-in engine, resets
  InputDeck's settings; presets, rules and the macro library are never touched.
- The engine logs which process sent each request, and log files are rotated.

### Changed
- The **File** menu is now **Options**.
- Own application icon.

### Fixed
- The built-in engine could not start on real systems (python-evdev devices cannot be used as dictionary
  keys). Any failure while starting now releases the device again.
- Virtual output devices are created up front, so the first key of a macro is no longer lost.
- Kernel autorepeat events are no longer forwarded twice.
- The engine no longer exits and restarts in a loop while a client keeps talking to it.

## [0.2.0] and earlier - development builds

- Device detection grouped like input-remapper, key/hotkey recording, macro recording with timing,
  loop modes (repeat while held / toggle), auto-switching per program, macro library with right-click
  assignment, About dialog, Saito `config.txt` import, stale device-ID repair.
- Experimental built-in remapping engine (no input-remapper needed).
- Installer for Arch-, Debian- and Fedora-based systems.
