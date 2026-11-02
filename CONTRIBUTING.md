# Contributing

Bug reports, ideas and pull requests are welcome.

## Reporting a bug
Use the bug report form. Please include the output of `inputdeck --check` and the relevant log
(*Logs* menu → *Copy all*, or `~/.local/share/inputdeck/`).

## Development setup
```bash
git clone https://github.com/t3ng4/inputdeck && cd inputdeck
python3 -m unittest discover -s tests        # needs nothing installed: evdev/uinput/Qt are faked
python3 -m inputdeck                         # run from the checkout (needs PyQt6 + python-evdev)
```

## Layout
| Path | What |
|---|---|
| `inputdeck/core.py` | Qt-free logic: presets, macros, rules, auto-switch, macro library |
| `inputdeck/engine.py` | the built-in remapping engine and its client |
| `inputdeck/recorder.py` | device discovery (grouped like input-remapper) and key/macro recording |
| `inputdeck/gui.py` | the PyQt6 interface |
| `inputdeck/recovery.py` | "reset to defaults" (also `inputdeck --reset`) |
| `tests/` | unit and flow tests against fake devices (`qt_stub.py`, `test_recorder.py` hold the fakes) |

## Guidelines
- Keep Python 3.9 compatibility.
- New behaviour comes with a test. The fakes are deliberately strict (for example, fake input devices are
  unhashable like the real ones); if you extend them, keep them honest.
- Anything that grabs devices must fail safe: on any error, release the device.
- Never delete or rewrite user data (presets, rules, macros) as a side effect.
- Hardware matters: say in the pull request which device and engine you tried it on.
