"""InputDeck core logic.

Everything here is plain Python: no Qt, and evdev is only imported lazily for key
names. That keeps it unit-testable and lets the headless daemon run without a GUI.

InputDeck does not import input-remapper. It edits the same preset JSON files
(~/.config/input-remapper-2/presets/<device>/<preset>.json) and drives the daemon
through `input-remapper-control`, so it keeps working across input-remapper updates
as long as the 2.x preset format stays the same.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

APP_ID = "inputdeck"
DEFAULT_PRESET = "Default"
DEFAULT_RULE = "DEFAULT"
EV_KEY = 1
BTN_LEFT_CODE = 272

TARGETS = ("keyboard", "mouse", "keyboard + mouse", "gamepad")
MOUSE_BUTTONS = {"BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE", "BTN_SIDE",
                 "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK", "BTN_TASK"}


# --------------------------------------------------------------------------- paths

@dataclass
class Paths:
    remapper_dir: Path
    config_dir: Path
    data_dir: Path

    @property
    def presets_dir(self) -> Path:
        return self.remapper_dir / "presets"

    @property
    def rules_file(self) -> Path:
        return self.config_dir / "rules.txt"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "deleted-presets"

    @classmethod
    def default(cls) -> "Paths":
        home = Path.home()
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
        env = os.environ.get("INPUT_REMAPPER_CONFIG_DIR")
        if env:
            rem = Path(env).expanduser()
        else:
            rem = cfg / "input-remapper-2"
            if not rem.is_dir():
                if (cfg / "input-remapper").is_dir():
                    rem = cfg / "input-remapper"
                elif (cfg / APP_ID / "presets").is_dir() or not shutil.which("input-remapper-control"):
                    rem = cfg / APP_ID      # standalone: InputDeck keeps its own presets folder
        return cls(rem, cfg / APP_ID, data / APP_ID)


# --------------------------------------------------------------------------- key names

_FALLBACK_NAMES = {
    1: "KEY_ESC", 14: "KEY_BACKSPACE", 15: "KEY_TAB", 28: "KEY_ENTER",
    29: "KEY_LEFTCTRL", 42: "KEY_LEFTSHIFT", 54: "KEY_RIGHTSHIFT", 56: "KEY_LEFTALT",
    57: "KEY_SPACE", 97: "KEY_RIGHTCTRL", 100: "KEY_RIGHTALT", 125: "KEY_LEFTMETA",
    103: "KEY_UP", 108: "KEY_DOWN", 105: "KEY_LEFT", 106: "KEY_RIGHT",
    272: "BTN_LEFT", 273: "BTN_RIGHT", 274: "BTN_MIDDLE", 275: "BTN_SIDE",
    276: "BTN_EXTRA", 277: "BTN_FORWARD", 278: "BTN_BACK", 279: "BTN_TASK",
    87: "KEY_F11", 88: "KEY_F12",
}
for _i, _c in enumerate("1234567890"):
    _FALLBACK_NAMES[2 + _i] = f"KEY_{_c}"
for _i in range(10):
    _FALLBACK_NAMES[59 + _i] = f"KEY_F{_i + 1}"
for _row, _start in (("QWERTYUIOP", 16), ("ASDFGHJKL", 30), ("ZXCVBNM", 44)):
    for _i, _c in enumerate(_row):
        _FALLBACK_NAMES[_start + _i] = f"KEY_{_c}"
_FALLBACK_CODES = {v: k for k, v in _FALLBACK_NAMES.items()}

_IGNORED_ALIASES = {"KEY_MIN_INTERESTING", "BTN_MISC", "BTN_MOUSE", "BTN_JOYSTICK",
                    "BTN_GAMEPAD", "BTN_DIGI", "BTN_WHEEL", "BTN_TRIGGER_HAPPY"}


def code_name(code: int) -> str:
    """evdev key/button code -> symbolic name ("KEY_A", "BTN_LEFT")."""
    try:
        from evdev import ecodes
        name = ecodes.keys.get(code)
    except Exception:  # evdev missing -> small built-in table
        name = _FALLBACK_NAMES.get(code)
    if isinstance(name, (list, tuple)):
        usable = [n for n in name if n not in _IGNORED_ALIASES]
        name = (usable or list(name))[0]
    return name or f"KEY_UNKNOWN_{code}"


def name_code(name: str) -> Optional[int]:
    try:
        from evdev import ecodes
        value = ecodes.ecodes.get(name)
        if isinstance(value, int):
            return value
    except Exception:
        pass
    return _FALLBACK_CODES.get(name)


def all_key_names() -> list[str]:
    """Names for autocompletion in the GUI."""
    try:
        from evdev import ecodes
        names = set()
        for value in ecodes.keys.values():
            for n in (value if isinstance(value, (list, tuple)) else [value]):
                if n not in _IGNORED_ALIASES:
                    names.add(n)
        return sorted(names)
    except Exception:
        return sorted(_FALLBACK_CODES)


# --------------------------------------------------------------------------- presets

class PresetError(Exception):
    pass


def safe_name(name: str) -> str:
    return re.sub(r"[/\\:\x00]", "-", name or "").strip() or "Unnamed"


class PresetStore:
    """Reads and writes input-remapper 2.x preset files."""

    def __init__(self, paths: Paths):
        self.paths = paths

    def devices(self) -> list[str]:
        d = self.paths.presets_dir
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def presets(self, device: str) -> list[str]:
        d = self.paths.presets_dir / device
        return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []

    def file(self, device: str, preset: str) -> Path:
        return self.paths.presets_dir / device / f"{preset}.json"

    def exists(self, device: str, preset: str) -> bool:
        return self.file(device, preset).is_file()

    def load(self, device: str, preset: str) -> list[dict]:
        path = self.file(device, preset)
        if not path.is_file():
            raise PresetError(f"{preset}: no such preset for {device}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise PresetError(f"{path.name}: invalid JSON ({exc})") from exc
        if not isinstance(data, list):
            raise PresetError(f"{path.name}: not an input-remapper 2.x preset (expected a list)")
        return data

    def save(self, device: str, preset: str, mappings: list[dict]) -> None:
        path = self.file(device, preset)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size:
            self._backup(device, preset, path)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(mappings, indent=4) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def create(self, device: str, preset: str, base: Optional[str] = None) -> str:
        preset = safe_name(preset)
        if self.exists(device, preset):
            raise PresetError(f"“{preset}” already exists")
        mappings = self.load(device, base) if base and self.exists(device, base) else []
        self.save(device, preset, mappings)
        return preset

    def delete(self, device: str, preset: str) -> None:
        """Moves the file to the trash folder instead of unlinking it."""
        src = self.file(device, preset)
        if not src.is_file():
            raise PresetError(f"“{preset}” not found")
        dest = self.paths.trash_dir / device
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest / f"{preset}.json.{time.strftime('%Y%m%d-%H%M%S')}"))

    def _backup(self, device: str, preset: str, path: Path, keep: int = 20) -> None:
        dest = self.paths.backups_dir / device
        dest.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, dest / f"{preset}.json.{stamp}")
        old = sorted(dest.glob(f"{preset}.json.*"))
        for extra in old[:-keep]:
            extra.unlink(missing_ok=True)


# --------------------------------------------------------------------------- mappings

def make_mapping(inputs: list[tuple[int, int, Optional[str]]], output: str,
                 target: str, base: Optional[dict] = None) -> dict:
    """Build (or update) a mapping dict. `inputs` is [(type, code, origin_hash)]."""
    mapping = dict(base or {})
    combo = []
    for ev_type, code, ohash in inputs:
        item = {"type": ev_type, "code": code}
        if ohash:
            item["origin_hash"] = ohash
        combo.append(item)
    mapping["input_combination"] = combo
    mapping["target_uinput"] = target
    mapping["output_symbol"] = output
    mapping.pop("output_type", None)
    mapping.pop("output_code", None)
    return mapping


def mapping_inputs(mapping: dict) -> list[tuple[int, int, Optional[str]]]:
    return [(i.get("type", EV_KEY), i.get("code", 0), i.get("origin_hash"))
            for i in mapping.get("input_combination", [])]


def describe_input(mapping: dict) -> str:
    parts = []
    for ev_type, code, _h in mapping_inputs(mapping):
        parts.append(code_name(code) if ev_type == EV_KEY else f"axis {ev_type}:{code}")
    return " + ".join(parts) or "(empty)"


def describe_output(mapping: dict) -> str:
    if mapping.get("output_symbol"):
        return str(mapping["output_symbol"])
    if "output_code" in mapping:
        return f"analog {mapping.get('output_type')}:{mapping['output_code']}"
    return "(none)"


def keys_to_output(names: list[str]) -> str:
    """One key -> its name; several -> hold_keys() so they act as a combination."""
    if not names:
        return ""
    return names[0] if len(names) == 1 else f"hold_keys({', '.join(names)})"


def guess_target(output: str) -> str:
    """Which virtual device the output must go to (keyboard / mouse / both)."""
    syms: list[str] = []
    if "(" not in output:
        syms = [s.strip() for s in output.split("+") if s.strip()]
    else:
        for m in re.finditer(r"\b(?:key|key_down|key_up|hold_keys)\(([^()]*)", output):
            for arg in m.group(1).split(","):
                arg = arg.strip()
                if "=" in arg:
                    arg = arg.split("=", 1)[1].strip()
                if arg and not arg.startswith("$"):
                    syms.append(arg)
    mouse = any(s in MOUSE_BUTTONS for s in syms) or bool(
        re.search(r"\b(?:mouse|mouse_xy|wheel)\(", output))
    keyboard = any(s not in MOUSE_BUTTONS for s in syms)
    if mouse and keyboard:
        return "keyboard + mouse"
    return "mouse" if mouse else "keyboard"


def blocks_left_click(mappings: Iterable[dict]) -> bool:
    """True if a preset swallows BTN_LEFT without producing one anywhere.

    That would leave you unable to click, including on the window you'd need to fix it.
    """
    for m in mappings:
        ins = mapping_inputs(m)
        if len(ins) == 1 and ins[0][:2] == (EV_KEY, BTN_LEFT_CODE):
            if "BTN_LEFT" not in str(m.get("output_symbol", "")):
                return True
    return False


# --------------------------------------------------------------------------- macros

@dataclass
class Step:
    kind: str            # "tap" | "down" | "up" | "wait"
    arg: object          # key name, or milliseconds for "wait"

    def text(self) -> str:
        if self.kind == "tap":
            return f"key({self.arg})"
        if self.kind == "down":
            return f"key_down({self.arg})"
        if self.kind == "up":
            return f"key_up({self.arg})"
        return f"wait({int(self.arg)})"

    def label(self) -> str:
        return {"tap": "tap", "down": "hold down", "up": "release",
                "wait": "wait"}[self.kind] + f"  {self.arg}" + (" ms" if self.kind == "wait" else "")


def steps_to_text(steps: list[Step]) -> str:
    return ".".join(s.text() for s in steps)


_CALL = re.compile(r"(\w+)\(([^()]*)\)")


def parse_macro(text: str) -> Optional[list[Step]]:
    """Parse the subset InputDeck writes. None means "can't edit visually"."""
    text = "".join(text.split())
    if not text:
        return []
    matches = list(_CALL.finditer(text))
    if not matches or ".".join(m.group(0) for m in matches) != text:
        return None
    steps = []
    for m in matches:
        fn, arg = m.group(1), m.group(2)
        if fn in ("key", "key_down", "key_up") and re.fullmatch(r"[A-Za-z0-9_]+", arg):
            steps.append(Step({"key": "tap", "key_down": "down", "key_up": "up"}[fn], arg))
        elif fn == "wait" and arg.isdigit():
            steps.append(Step("wait", int(arg)))
        else:
            return None
    return steps


def events_to_steps(events: list[tuple[float, str, int]], tap_ms: int = 150,
                    min_wait_ms: int = 20, fixed_delay_ms: Optional[int] = None) -> list[Step]:
    """Turn recorded (timestamp_s, key_name, value) events into macro steps.

    value is 1 for press, 0 for release. Quick press+release pairs become `key()`.
    Releases without a press (the click that started recording) are dropped, and keys
    still held at the end are released.
    """
    events = sorted(events, key=lambda e: e[0])
    held: set[str] = set()
    clean: list[tuple[float, str, int]] = []
    for t, name, value in events:
        if value == 1 and name not in held:
            held.add(name)
            clean.append((t, name, 1))
        elif value == 0 and name in held:
            held.discard(name)
            clean.append((t, name, 0))
    end_t = clean[-1][0] if clean else 0.0
    for name in sorted(held):
        clean.append((end_t, name, 0))

    # actions: (start_t, end_t, Step)
    actions: list[tuple[float, float, Step]] = []
    i = 0
    while i < len(clean):
        t, name, value = clean[i]
        if (value == 1 and i + 1 < len(clean) and clean[i + 1][1] == name
                and clean[i + 1][2] == 0 and (clean[i + 1][0] - t) * 1000 <= tap_ms):
            actions.append((t, clean[i + 1][0], Step("tap", name)))
            i += 2
            continue
        actions.append((t, t, Step("down" if value == 1 else "up", name)))
        i += 1

    steps: list[Step] = []
    for idx, (start, _end, step) in enumerate(actions):
        if idx:
            if fixed_delay_ms is not None:
                if fixed_delay_ms > 0:
                    steps.append(Step("wait", int(fixed_delay_ms)))
            else:
                gap_ms = (start - actions[idx - 1][1]) * 1000
                if gap_ms >= min_wait_ms:
                    steps.append(Step("wait", max(10, int(round(gap_ms / 10.0)) * 10)))
        steps.append(step)
    return steps


def wrap_loop(inner: str, mode: str, delay_ms: int = 0) -> str:
    """mode: "" | "hold" (repeat while key held) | "toggle" (press to start/stop)."""
    inner = inner.strip()
    if mode not in ("hold", "toggle") or not inner:
        return inner
    if delay_ms > 0:
        inner = f"{inner}.wait({int(delay_ms)})"
    return f"{mode}({inner})"


def unwrap_loop(text: str) -> tuple[str, str, int]:
    """Inverse of wrap_loop: returns (mode, inner, delay_ms)."""
    flat = "".join(text.split())
    m = re.fullmatch(r"(hold|toggle)\((.*)\)", flat)
    if not m:
        return "", text.strip(), 0
    inner = m.group(2)
    if inner.count("(") != inner.count(")"):
        return "", text.strip(), 0
    delay = 0
    d = re.search(r"\.wait\((\d+)\)$", inner)
    if d and inner != d.group(0)[1:]:
        delay = int(d.group(1))
        inner = inner[:d.start()]
    return m.group(1), inner, delay


def check_macro_syntax(text: str) -> Optional[str]:
    """Cheap sanity check. input-remapper does the real validation when it loads."""
    depth = 0
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            return "unbalanced parentheses"
    return "unbalanced parentheses" if depth else None


# --------------------------------------------------------------------------- macro library

class LibraryError(Exception):
    pass


class MacroLibrary:
    """Named, reusable outputs (macros, hotkeys) kept in <config>/macros.json."""

    def __init__(self, paths: Paths):
        self.path = paths.config_dir / "macros.json"

    def all(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("macros", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            return []
        return [m for m in items if isinstance(m, dict) and m.get("name") and m.get("output")]

    def names(self) -> list:
        return [m["name"] for m in self.all()]

    def get(self, name: str) -> Optional[str]:
        for m in self.all():
            if m["name"].lower() == name.strip().lower():
                return m["output"]
        return None

    def _write(self, items: list) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps({"version": 1, "macros": items}, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self, name: str, output: str, overwrite: bool = False) -> str:
        name, output = name.strip(), output.strip()
        if not name or not output:
            raise LibraryError("a macro needs a name and some content")
        items = self.all()
        existing = next((m for m in items if m["name"].lower() == name.lower()), None)
        if existing and not overwrite:
            raise LibraryError(f"“{existing['name']}” already exists")
        if existing:
            existing["output"] = output
        else:
            items.append({"name": name, "output": output})
        self._write(items)
        return name

    def rename(self, old: str, new: str) -> None:
        new = new.strip()
        items = self.all()
        item = next((m for m in items if m["name"] == old), None)
        if not item or not new:
            raise LibraryError("nothing to rename")
        if any(m is not item and m["name"].lower() == new.lower() for m in items):
            raise LibraryError(f"“{new}” already exists")
        item["name"] = new
        self._write(items)

    def delete(self, name: str) -> None:
        self._write([m for m in self.all() if m["name"] != name])


# --------------------------------------------------------------------------- stale hashes

def origin_hash(capabilities: dict, name: str) -> str:
    """Same recipe input-remapper uses to tag a mapping with its /dev/input node."""
    return hashlib.md5((str(capabilities) + name).encode()).hexdigest().lower()


def repair_origin_hashes(mappings: list[dict], live_nodes: list[dict]):
    """Repoint inputs whose origin_hash matches no live node of the device.

    live_nodes: [{"hash": str, "codes": {(type, code), ...}}]. A kernel/firmware update
    can change the reported capabilities, which changes the hash and silently disables
    every mapping recorded before. Returns (mappings, changes, unresolved).
    """
    live = {n["hash"] for n in live_nodes}
    trusted = {h for m in mappings for _t, _c, h in mapping_inputs(m) if h in live}
    changes, unresolved = [], []
    for m in mappings:
        for item in m.get("input_combination", []):
            h = item.get("origin_hash")
            if not h or h in live:
                continue
            key = (item.get("type"), item.get("code"))
            cands = [n["hash"] for n in live_nodes if key in n["codes"]]
            pick = None
            if len(cands) == 1:
                pick = cands[0]
            elif cands:
                pref = [c for c in cands if c in trusted]
                pick = pref[0] if len(pref) == 1 else None
            label = code_name(item["code"]) if item.get("type") == EV_KEY else str(key)
            if pick:
                item["origin_hash"] = pick
                changes.append(f"{label}: {h[:8]}… → {pick[:8]}…")
            else:
                unresolved.append(label)
    return mappings, changes, unresolved


# --------------------------------------------------------------------------- rules

@dataclass
class Rule:
    process: str
    device: str
    preset: str
    when: str = "running"               # see WHEN_CHOICES

    @property
    def is_default(self) -> bool:
        return self.process.strip().upper() == DEFAULT_RULE

    @property
    def needs_window(self) -> bool:
        return not self.is_default and self.when in ("visible", "focused")


# When a rule applies. "running" is what Saito's tool does; the others look at the program's window.
WHEN_CHOICES = (
    ("running", "is running (even if minimized or hidden in the tray)"),
    ("visible", "has a window on screen (not minimized, not hidden in the tray)"),
    ("focused", "has the window you are currently working in"),
)
WHEN_SHORT = {"running": "running", "visible": "window visible", "focused": "window focused"}


RULES_HEADER = ("# process fragment | device name | preset name (without .json) [| running, visible or focused]\n"
                "# first matching rule per device wins; DEFAULT is that device's fallback\n"
                "# the optional 4th field is when the rule applies: running (default), visible (window on\n"
                "# screen) or focused (window in front). Files without it work exactly as before.\n")


def parse_rules(text: str) -> list[Rule]:
    """`process|device|preset[|when]` per line; the first three fields are Saito's config.txt format."""
    rules = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|", 3)]
        if len(parts) >= 3 and all(parts[:3]):
            when = parts[3].lower() if len(parts) == 4 else "running"
            rules.append(Rule(*parts[:3], when if when in WHEN_SHORT else "running"))
    return rules


def dump_rules(rules: list[Rule]) -> str:
    return RULES_HEADER + "".join(
        f"{r.process}|{r.device}|{r.preset}" + (f"|{r.when}" if r.when != "running" else "") + "\n"
        for r in rules)


def load_rules(path: Path) -> list[Rule]:
    try:
        return parse_rules(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


def save_rules(path: Path, rules: list[Rule]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(dump_rules(rules), encoding="utf-8")
    os.replace(tmp, path)


def scan_processes(proc_root: Path = Path("/proc")) -> list[str]:
    """Full command lines of running processes (like `pgrep -f` matches against)."""
    out, me = [], str(os.getpid())
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit() or entry.name == me:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        line = " ".join(a for a in raw.decode("utf-8", "replace").split("\0") if a)
        if line:
            out.append(line)
    return out


def process_names(proc_root: Path = Path("/proc")) -> list[str]:
    names = set()
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name.isdigit():
            try:
                names.add((entry / "comm").read_text().strip())
            except OSError:
                pass
    return sorted(n for n in names if n)


def pid_cmdline(pid: int, proc_root: Path = Path("/proc")) -> str:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(a for a in raw.decode("utf-8", "replace").split("\0") if a)


def window_matches(window, fragment: str, cmdline_for: Callable[[int], str] = pid_cmdline) -> bool:
    """Does this window belong to the program `fragment` names? Checked against the window class
    (Wine/Proton games carry their .exe name there), its title, and its process's command line."""
    frag = fragment.lower()
    if any(frag in c.lower() for c in window.classes) or frag in window.title.lower():
        return True
    return bool(window.pid) and frag in cmdline_for(window.pid).lower()


def window_qualifies(window, when: str) -> bool:
    if when == "focused":
        return bool(window.active) and not window.minimized
    return not window.minimized and window.on_current_desktop


def decide(rules: list[Rule], cmdlines: list[str], windows: Optional[list] = None,
           cmdline_for: Callable[[int], str] = pid_cmdline) -> dict[str, str]:
    """Wanted preset per device: first matching rule wins, else that device's DEFAULT.

    `windows` is the current window list (see windows.py). When it is None, window detection
    isn't available and "visible"/"focused" rules fall back to plain "is running"."""
    lowered = [c.lower() for c in cmdlines]

    def matches(r: Rule) -> bool:
        if r.needs_window and windows is not None:
            return any(window_qualifies(w, r.when) and window_matches(w, r.process, cmdline_for)
                       for w in windows)
        return any(r.process.lower() in c for c in lowered)

    wanted: dict[str, str] = {}
    fallback: dict[str, str] = {}
    for r in rules:
        if r.is_default:
            fallback.setdefault(r.device, r.preset)
        elif r.device not in wanted and matches(r):
            wanted[r.device] = r.preset
    for device, preset in fallback.items():
        wanted.setdefault(device, preset)
    return wanted


# --------------------------------------------------------------------------- daemon control

Runner = Callable[[list], tuple]


def _run(cmd: list, timeout: float = 8.0) -> tuple:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return res.returncode, (res.stdout + res.stderr).strip()


class RemapperControl:
    """Thin wrapper around `input-remapper-control`.

    Remembers which preset it started per device (`running`) so a paused editing
    session can put things back afterwards.
    """

    kind = "input-remapper"

    def __init__(self, runner: Runner = _run, binary: Optional[str] = None):
        self.runner = runner
        self.binary = binary or shutil.which("input-remapper-control") or "input-remapper-control"
        self.running: dict = {}

    def available(self) -> bool:
        return shutil.which(self.binary) is not None or os.path.isfile(self.binary)

    def hello(self) -> bool:
        code, out = self.runner([self.binary, "--command", "hello"])
        return code == 0 and "hello" in out.lower()

    def start(self, device: str, preset: str) -> tuple:
        res = self.runner([self.binary, "--command", "start", "--device", device, "--preset", preset])
        if res[0] == 0:
            self.running[device] = preset
        return res

    def stop(self, device: str) -> tuple:
        res = self.runner([self.binary, "--command", "stop", "--device", device])
        if res[0] == 0:
            self.running.pop(device, None)
        return res

    def stop_all(self) -> tuple:
        res = self.runner([self.binary, "--command", "stop-all"])
        if res[0] == 0:
            self.running.clear()
        return res

    def refresh_running(self) -> None:
        """input-remapper's daemon can't be asked what is active; `running` only knows
        what InputDeck started in this session."""

    def autoload(self) -> tuple:
        """Load the presets configured for autoload in input-remapper itself."""
        return self.runner([self.binary, "--command", "autoload"])


# --------------------------------------------------------------------------- switcher

class Switcher:
    """Applies the right preset per device based on which programs are running."""

    def __init__(self, control: RemapperControl, store: PresetStore,
                 get_rules: Callable[[], list[Rule]],
                 scan: Callable[[], list[str]] = scan_processes,
                 log: Callable[[str], None] = print, interval: float = 3.0,
                 notify: Optional[Callable[[str], None]] = None,
                 windows: Optional[Callable] = None):
        self.control, self.store, self.get_rules = control, store, get_rules
        self.scan, self.log, self.interval, self.notify = scan, log, interval, notify
        self.windows = windows            # callable(interesting) -> list of windows, or None if unavailable
        self.use_windows = True           # the "window rules" setting; off = every rule acts like "running"
        self._window_warning = False
        self.applied: dict[str, str] = {}
        self._failed: dict[tuple, float] = {}
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def tick(self) -> None:
        if self._paused.is_set():
            return
        rules = self.get_rules()
        wanted = decide(rules, self.scan(), self._window_list(rules))
        for device, preset in wanted.items():
            if self.applied.get(device) == preset:
                continue
            if (device, preset) in self._failed and time.monotonic() - self._failed[(device, preset)] < 30:
                continue
            if not self.store.exists(device, preset):
                self._failed[(device, preset)] = time.monotonic()
                self.log(f"[skip] {device}: preset “{preset}” does not exist")
                continue
            ok, out = self.control.start(device, preset)
            if ok == 0:
                self.applied[device] = preset
                self._failed.pop((device, preset), None)
                skipped = [w for w in getattr(self.control, "last_warnings", []) if "skipped" in w]
                self.log(f"[switch] {device} → {preset}"
                         + (f"  ({len(skipped)} mapping(s) skipped, see Logs → Engine log)" if skipped else ""))
                if self.notify:
                    self.notify(f"{device}: {preset}")
            else:
                self._failed[(device, preset)] = time.monotonic()
                self.log(f"[error] {device} → {preset}: {out or 'input-remapper-control failed'}")

    def _window_list(self, rules: list[Rule]) -> Optional[list]:
        """Windows for the rules that need them; None when none do, the setting is off or detection fails."""
        fragments = [r.process.lower() for r in rules if r.needs_window]
        if not fragments or not self.use_windows:
            return None
        snapshot = None
        if self.windows is not None:
            def interesting(pid: int, classes: tuple, title: str) -> bool:
                text = " ".join(classes).lower() + " " + title.lower()
                cmd = pid_cmdline(pid).lower() if pid else ""
                return any(f in text or f in cmd for f in fragments)
            snapshot = self.windows(interesting)
        if snapshot is None and not self._window_warning:
            self._window_warning = True
            self.log("[info] window detection is not available here, so rules set to “window visible/focused” "
                     "act like “running” (needs xprop and an X11 or XWayland display)")
        elif snapshot is not None:
            self._window_warning = False
        return snapshot

    def reapply(self) -> None:
        self.applied.clear()
        self._failed.clear()

    def pause(self) -> None:
        self._paused.set()

    def resume(self, keep: Iterable[str] = ()) -> None:
        """Unpause. Forget what was applied (so the rules re-assert themselves) except
        for devices in `keep`, which were just set by hand and should stick until the
        rules' decision for them changes."""
        keep = set(keep)
        self.applied = {d: p for d, p in self.applied.items() if d in keep}
        self._failed.clear()
        self._paused.clear()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="inputdeck-switcher", daemon=True)
        self._thread.start()
        self.log("auto-switch started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self.log("auto-switch stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # keep the loop alive
                self.log(f"[error] {exc}")
            self._stop.wait(self.interval)


# --------------------------------------------------------------------------- log files

def rotate_log(path: Path, max_bytes: int = 262144, keep_lines: int = 2000) -> None:
    """Keep log files from growing forever: when over max_bytes, keep only the last lines."""
    try:
        if Path(path).stat().st_size <= max_bytes:
            return
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-keep_lines:]
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def append_log(path: Path, line: str) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except OSError:
        pass


def read_tail(path: Path, lines: int = 500) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(nothing logged yet)"
    return "\n".join(text.splitlines()[-lines:]) or "(nothing logged yet)"


# --------------------------------------------------------------------------- settings / health

DEFAULT_SETTINGS = {"backend": "auto", "interval": 3.0, "notify": False, "apply_after_save": True,
                    "autoswitch": True, "last_device": "", "close_to_tray": False,
                    "window_rules": True}


def load_settings(paths: Paths) -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        data.update(json.loads(paths.settings_file.read_text()))
    except (OSError, ValueError):
        pass
    return data


def save_settings(paths: Paths, settings: dict) -> None:
    try:
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        paths.settings_file.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass


def can_read_input_devices() -> bool:
    d = Path("/dev/input")
    try:
        nodes = [p for p in d.iterdir() if p.name.startswith("event")]
    except OSError:
        return False
    for p in nodes:
        try:
            with open(p, "rb"):
                return True
        except OSError:
            continue
    return False


# Package names per distro family (Arch/CachyOS/Garuda, Debian/Ubuntu/Mint, Fedora).
PY_PACKAGES = {"pacman": "python-pyqt6 python-evdev", "apt": "python3-pyqt6 python3-evdev",
               "dnf": "python3-qt6 python3-evdev"}
INSTALL_CMD = {"pacman": "sudo pacman -S --needed {}", "apt": "sudo apt install {}",
               "dnf": "sudo dnf install {}"}
REMAPPER_HINT = {
    "pacman": "install input-remapper (AUR / chaotic-aur / your distro repo, e.g. yay -S input-remapper-git)",
    "apt": "sudo apt install input-remapper   (needs version 2.x; otherwise use the .deb from its GitHub releases)",
    "dnf": "sudo dnf install input-remapper",
}


def package_manager() -> str:
    for family, exe in (("pacman", "pacman"), ("apt", "apt-get"), ("dnf", "dnf")):
        if shutil.which(exe):
            return family
    return ""


def python_deps_hint(only: str = "") -> str:
    pm = package_manager()
    if not pm:
        return "install PyQt6 and python-evdev with your package manager (or in a venv: pip install PyQt6 evdev)"
    pkgs = PY_PACKAGES[pm].split()
    if only == "evdev":
        pkgs = [p for p in pkgs if "evdev" in p]
    return INSTALL_CMD[pm].format(" ".join(pkgs))


def remapper_install_hint() -> str:
    return REMAPPER_HINT.get(package_manager(), "install input-remapper 2.x (see its README for your distro)")


XPROP_PACKAGES = {"pacman": "xorg-xprop", "apt": "x11-utils", "dnf": "xorg-x11-utils"}
OPTIONAL = "(optional) "


def xprop_hint() -> str:
    pm = package_manager()
    return INSTALL_CMD[pm].format(XPROP_PACKAGES[pm]) if pm else "install the `xprop` tool (package xorg-xprop / x11-utils)"


def setup_report(control: RemapperControl, paths: Paths) -> list[tuple]:
    """[(ok, what, hint)] for the first-run checklist. Entries starting with OPTIONAL don't count as failures."""
    from . import windows                          # imported here: windows.py imports this module
    report = _control_report(control, paths)
    report.append((windows.default_provider() is not None,
                   OPTIONAL + "window detection for “window visible/focused” rules: " + windows.describe(),
                   xprop_hint() if not shutil.which("xprop") else
                   "needs an X11 or XWayland display (DISPLAY is not set in this environment)"))
    return report


def _control_report(control: RemapperControl, paths: Paths) -> list[tuple]:
    if hasattr(control, "report"):                 # the built-in engine has its own checklist
        return list(control.report(paths))
    report = []
    try:
        import evdev  # noqa: F401
        report.append((True, "python-evdev installed", ""))
    except ImportError:
        report.append((False, "python-evdev missing", python_deps_hint("evdev")))
    report.append((control.available(), "input-remapper-control found", remapper_install_hint()))
    report.append((control.available() and control.hello(),
                   "input-remapper daemon reachable",
                   "sudo systemctl enable --now input-remapper.service"))
    report.append((can_read_input_devices(), "permission to read /dev/input/event*",
                   "sudo usermod -aG input $USER   (then log out and back in)"))
    old_format = paths.remapper_dir.name == "input-remapper" and paths.remapper_dir.is_dir()
    report.append((not old_format, "input-remapper 2.x preset folder (not the 1.x layout)",
                   f"{paths.remapper_dir} is from input-remapper 1.x, whose presets use another format; "
                   "install input-remapper 2.x"))
    report.append((paths.presets_dir.is_dir(), f"preset folder exists ({paths.presets_dir})",
                   "open input-remapper once and create a preset"))
    return report


def send_notification(text: str) -> None:
    exe = shutil.which("notify-send")
    if exe:
        _run([exe, "-a", "InputDeck", "InputDeck", text], 5)
