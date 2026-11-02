"""InputDeck's built-in remapping engine (experimental): works without input-remapper.

How it works (the same idea as input-remapper's daemon, in plain Python):

* For a device group with a preset applied, every /dev/input node that has a mapped key is
  *grabbed* (so the desktop no longer sees the real device) and cloned as a virtual device.
* Every event is forwarded unchanged to the clone, except keys that are the trigger of a
  mapping: those are swallowed and replaced by the mapping's output.
* Outputs are written to virtual keyboard / mouse devices created through /dev/uinput.

Supported subset of the input-remapper 2.x preset format: key/button inputs and
combinations; outputs that are a key (KEY_*, BTN_*), a combination (hold_keys) or a macro
built from key, key_down, key_up, wait, repeat, hold and toggle. Analog (axis) mappings and
other macro functions are skipped with a warning.

Safety: nodes without a mapped key are never grabbed; killing the process releases every
grab (the kernel does that); and Ctrl+Alt+Shift+Esc stops all remapping immediately.

The engine runs as its own process (`python3 -m inputdeck --engine`), controlled over a
unix socket, so a GUI crash or restart never leaves a device grabbed.
"""
from __future__ import annotations

import functools
import json
import os
import re
import selectors
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import core

try:
    import evdev
    from evdev import ecodes
except ImportError:
    evdev = None
    ecodes = None

EV_SYN, EV_KEY, EV_REL = 0, 1, 2
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0, 1, 6, 8
MOUSE_CODES = range(0x110, 0x118)                     # BTN_LEFT .. BTN_TASK
BTN_LEFT_CODE = 0x110
PANIC_CHORD = frozenset({29, 56, 42, 1})              # Left Ctrl + Left Alt + Left Shift + Esc
IDLE_EXIT_SECONDS = 300.0
DEFAULT_KEY_SLEEP_MS = 10


class EngineError(Exception):
    pass


class MacroError(ValueError):
    pass


# --------------------------------------------------------------------------- symbols

_KEYSYM_ALIASES = {
    "Control_L": "KEY_LEFTCTRL", "Control_R": "KEY_RIGHTCTRL", "Shift_L": "KEY_LEFTSHIFT",
    "Shift_R": "KEY_RIGHTSHIFT", "Alt_L": "KEY_LEFTALT", "Alt_R": "KEY_RIGHTALT",
    "Super_L": "KEY_LEFTMETA", "Super_R": "KEY_RIGHTMETA", "Return": "KEY_ENTER",
    "space": "KEY_SPACE", "Escape": "KEY_ESC", "Tab": "KEY_TAB", "BackSpace": "KEY_BACKSPACE",
    "Delete": "KEY_DELETE", "Up": "KEY_UP", "Down": "KEY_DOWN", "Left": "KEY_LEFT",
    "Right": "KEY_RIGHT", "Home": "KEY_HOME", "End": "KEY_END", "Page_Up": "KEY_PAGEUP",
    "Page_Down": "KEY_PAGEDOWN", "Insert": "KEY_INSERT",
}


@functools.lru_cache(maxsize=512)
def resolve_symbol(name: str) -> Optional[int]:
    """Key name -> evdev code. KEY_*/BTN_* names are exact; a few X keysyms and single
    letters/digits are mapped by QWERTY position (see symbol_is_guess)."""
    if name.startswith(("KEY_", "BTN_")):
        return core.name_code(name)
    alias = _KEYSYM_ALIASES.get(name)
    if alias:
        return core.name_code(alias)
    if re.fullmatch(r"[A-Za-z0-9]", name):
        return core.name_code("KEY_" + name.upper())
    return None


def symbol_is_guess(name: str) -> bool:
    return not name.startswith(("KEY_", "BTN_")) and name not in _KEYSYM_ALIASES


# --------------------------------------------------------------------------- macro language

@dataclass
class Call:
    name: str
    args: list


_TOKEN = re.compile(r"\s*(?:(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)|(?P<num>-?\d+(?:\.\d+)?)|(?P<p>[().,]))")


def _tokens(text: str) -> list:
    text, pos, out = text.strip(), 0, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise MacroError(f"unexpected character {text[pos]!r}")
        pos = m.end()
        if m.group("name"):
            out.append(("name", m.group("name")))
        elif m.group("num"):
            raw = m.group("num")
            out.append(("num", float(raw) if "." in raw else int(raw)))
        else:
            out.append(("p", m.group("p")))
    return out


class _Parser:
    def __init__(self, toks: list):
        self.t, self.i = toks, 0

    def peek(self, k: int = 0):
        return self.t[self.i + k] if self.i + k < len(self.t) else (None, None)

    def expect(self, punct: str) -> None:
        if self.peek() != ("p", punct):
            raise MacroError(f"expected {punct!r}")
        self.i += 1

    def chain(self) -> list:
        calls = [self.call()]
        while self.peek() == ("p", "."):
            self.i += 1
            calls.append(self.call())
        return calls

    def call(self) -> Call:
        kind, val = self.peek()
        if kind != "name":
            raise MacroError("expected a function name")
        self.i += 1
        self.expect("(")
        args = []
        if self.peek() != ("p", ")"):
            while True:
                args.append(self.arg())
                if self.peek() == ("p", ","):
                    self.i += 1
                    continue
                break
        self.expect(")")
        return Call(val, args)

    def arg(self):
        kind, val = self.peek()
        if kind == "num":
            self.i += 1
            return val
        if kind == "name":
            if self.peek(1) == ("p", "("):
                return self.chain()
            self.i += 1
            return val
        raise MacroError("expected an argument")


def parse_macro_ast(text: str) -> list:
    parser = _Parser(_tokens(text))
    chain = parser.chain()
    if parser.i != len(parser.t):
        raise MacroError("unexpected text after the macro")
    return chain


def _check_symbol(arg, func: str) -> None:
    if not isinstance(arg, str) or resolve_symbol(arg) is None:
        raise MacroError(f"{func}(): unknown key {arg!r}")


def validate_chain(chain: list) -> None:
    for call in chain:
        n, a = call.name, call.args
        if n in ("key", "key_down", "key_up"):
            if len(a) != 1:
                raise MacroError(f"{n}() takes exactly one key")
            _check_symbol(a[0], n)
        elif n == "wait":
            if len(a) != 1 or not isinstance(a[0], (int, float)):
                raise MacroError("wait() takes a number of milliseconds")
        elif n in ("hold", "toggle"):
            if len(a) != 1 or not isinstance(a[0], list):
                raise MacroError(f"{n}() takes one macro")
            validate_chain(a[0])
        elif n == "repeat":
            if len(a) != 2 or not isinstance(a[0], (int, float)) or not isinstance(a[1], list):
                raise MacroError("repeat() takes a count and a macro")
            validate_chain(a[1])
        elif n == "hold_keys":
            if not a:
                raise MacroError("hold_keys() needs at least one key")
            for k in a:
                _check_symbol(k, n)
        else:
            raise MacroError(f"{n}() isn't supported by the built-in engine")


@dataclass
class Plan:
    kind: str                                   # "disable" | "chord" | "macro"
    codes: list = field(default_factory=list)   # chord keys
    chain: list = field(default_factory=list)
    mode: str = "once"                          # once | hold | toggle


def plan_output(text: str, notes: Optional[list] = None) -> Plan:
    text = text.strip()
    if text == "disable":
        return Plan("disable")

    def codes_of(names: list) -> list:
        codes = []
        for n in names:
            code = resolve_symbol(n)
            if code is None:
                raise MacroError(f"unknown key {n!r}")
            if notes is not None and symbol_is_guess(n):
                notes.append(f"“{n}” was read as {core.code_name(code)} (QWERTY position); "
                             "KEY_ names are exact")
            codes.append(code)
        return codes

    if "(" not in text:
        return Plan("chord", codes=codes_of([s.strip() for s in text.split("+") if s.strip()]))
    chain = parse_macro_ast(text)
    validate_chain(chain)
    if len(chain) == 1 and chain[0].name == "hold_keys":
        return Plan("chord", codes=codes_of(chain[0].args))
    if len(chain) == 1 and chain[0].name in ("hold", "toggle"):
        return Plan("macro", chain=chain[0].args[0], mode=chain[0].name)
    return Plan("macro", chain=chain)


def chain_codes(chain: list) -> set:
    codes: set = set()
    for call in chain:
        n, a = call.name, call.args
        if n in ("key", "key_down", "key_up"):
            codes.add(resolve_symbol(a[0]))
        elif n == "hold_keys":
            codes.update(resolve_symbol(k) for k in a)
        elif n in ("hold", "toggle"):
            codes |= chain_codes(a[0])
        elif n == "repeat":
            codes |= chain_codes(a[1])
    return codes


def plan_codes(plan: Plan) -> set:
    return set(plan.codes) | chain_codes(plan.chain)


# --------------------------------------------------------------------------- output

def _keyboard_caps() -> dict:
    return {EV_KEY: sorted(c for c in ecodes.keys if isinstance(c, int) and 0 < c < 0x100)}


def _mouse_caps() -> dict:
    return {EV_KEY: list(MOUSE_CODES), EV_REL: [REL_X, REL_Y, REL_WHEEL, REL_HWHEEL]}


def _default_uinput(caps: dict, name: str):
    return evdev.UInput(caps, name=name)


def _default_clone(dev):
    return evdev.UInput.from_device(dev, name=f"InputDeck forward: {dev.name}"[:80])


class Output:
    """Virtual keyboard + mouse, created on first use."""

    def __init__(self, make_uinput: Callable, label: str):
        self._make, self._label = make_uinput, label
        self._kb = self._mouse = None
        self._lock = threading.Lock()
        self.down: set = set()

    def prepare(self, codes: set) -> None:
        """Create the virtual devices that will be needed *now*: the desktop only listens to a
        uinput device after it has appeared, so creating one on first use loses the first key."""
        if any(c in MOUSE_CODES for c in codes):
            self._device(BTN_LEFT_CODE)
        if any(c not in MOUSE_CODES for c in codes):
            self._device(30)

    def _device(self, code: int):
        if code in MOUSE_CODES:
            if self._mouse is None:
                self._mouse = self._make(_mouse_caps(), f"InputDeck mouse ({self._label})"[:80])
            return self._mouse
        if self._kb is None:
            self._kb = self._make(_keyboard_caps(), f"InputDeck keyboard ({self._label})"[:80])
        return self._kb

    def _send(self, code: int, value: int) -> None:
        dev = self._device(code)
        dev.write(EV_KEY, code, value)
        dev.syn()
        (self.down.add if value else self.down.discard)(code)

    def press(self, code: int) -> None:
        with self._lock:
            self._send(code, 1)

    def release(self, code: int) -> None:
        with self._lock:
            self._send(code, 0)

    def release_all(self) -> None:
        with self._lock:
            for code in sorted(self.down):
                self._send(code, 0)

    def close(self) -> None:
        self.release_all()
        for dev in (self._kb, self._mouse):
            if dev is not None:
                try:
                    dev.close()
                except OSError:
                    pass


# --------------------------------------------------------------------------- macro runner

class MacroRun(threading.Thread):
    def __init__(self, plan: Plan, out: Output, sleep_ms: float, log: Callable):
        super().__init__(name="inputdeck-macro-run", daemon=True)
        self.plan, self.out, self.sleep_ms, self.log = plan, out, sleep_ms, log
        self.stop_ev = threading.Event()
        self.released = threading.Event()       # the trigger key was let go
        self.held: set = set()

    def _sleep(self, ms: float) -> None:
        self.stop_ev.wait(max(ms, 0) / 1000.0)

    def _press(self, code: int) -> None:
        self.out.press(code)
        self.held.add(code)

    def _release(self, code: int) -> None:
        self.out.release(code)
        self.held.discard(code)

    def _looping(self, mode: str) -> bool:
        return not self.stop_ev.is_set() and (mode == "toggle" or not self.released.is_set())

    def run(self) -> None:
        try:
            if self.plan.mode == "once":
                self._exec(self.plan.chain)
            else:
                while self._looping(self.plan.mode):
                    self._exec(self.plan.chain)
                    self._sleep(1)               # a body without waits must not spin a core
        except Exception as exc:                 # never let a macro take the engine down
            self.log(f"[error] macro failed: {exc}")
        finally:
            for code in sorted(self.held):       # no key may stay stuck down
                self.out.release(code)

    def _exec(self, chain: list) -> None:
        for call in chain:
            if self.stop_ev.is_set():
                return
            n, a = call.name, call.args
            if n == "key":
                code = resolve_symbol(a[0])
                self._press(code)
                self._sleep(self.sleep_ms)
                self._release(code)
                self._sleep(self.sleep_ms)
            elif n == "key_down":
                self._press(resolve_symbol(a[0]))
            elif n == "key_up":
                self._release(resolve_symbol(a[0]))
            elif n == "wait":
                self._sleep(float(a[0]))
            elif n == "repeat":
                for _ in range(int(a[0])):
                    if self.stop_ev.is_set():
                        return
                    self._exec(a[1])
            elif n in ("hold", "toggle"):
                while self._looping(n):
                    self._exec(a[0])
                    self._sleep(1)
            elif n == "hold_keys":
                codes = [resolve_symbol(k) for k in a]
                for c in codes:
                    self._press(c)
                while not self.stop_ev.is_set() and not self.released.is_set():
                    self.stop_ev.wait(0.01)
                for c in reversed(codes):
                    self._release(c)


# --------------------------------------------------------------------------- injector

@dataclass
class Mapping:
    combo: list
    plan: Plan
    sleep_ms: float
    label: str


def compile_preset(data: list) -> tuple:
    """Preset JSON -> ([Mapping], [warning]). Unsupported mappings are skipped, never fatal."""
    mappings, warnings = [], []
    for m in data:
        inputs = core.mapping_inputs(m)
        label = core.describe_input(m)
        if not inputs or any(t != core.EV_KEY for t, _c, _h in inputs):
            warnings.append(f"{label}: axis inputs aren't supported by the built-in engine — skipped")
            continue
        symbol = m.get("output_symbol")
        if not symbol:
            warnings.append(f"{label}: analog outputs aren't supported by the built-in engine — skipped")
            continue
        notes: list = []
        try:
            plan = plan_output(str(symbol), notes)
        except MacroError as exc:
            warnings.append(f"{label}: {exc} — skipped")
            continue
        warnings += [f"{label}: {n}" for n in notes]
        mappings.append(Mapping([c for _t, c, _h in inputs], plan,
                                float(m.get("macro_key_sleep_ms", DEFAULT_KEY_SLEEP_MS)), label))
    return mappings, warnings


class Injector:
    """Everything for one device group: grabbed nodes, their clones, the mappings' state."""

    def __init__(self, name: str, nodes: list, mappings: list, make_uinput: Callable,
                 make_clone: Callable, log: Callable, settle: float = 0.0):
        self.name, self.nodes, self.mappings, self.log = name, nodes, mappings, log
        self.make_clone, self.settle = make_clone, settle
        self.out = Output(make_uinput, name)
        self.clones: dict = {}          # id(node) -> clone (evdev devices are not hashable)
        self.grabbed: list = []
        self.phys: set = set()          # keys physically down
        self.forwarded: dict = {}       # key -> clone it was forwarded through (still down there)
        self.active: dict = {}          # trigger key -> mapping currently held
        self.swallow: set = set()       # combo keys already released for the system
        self.runs: dict = {}
        self.preset_file = ""

    def start(self) -> None:
        try:
            for node in self.nodes:
                node.grab()
                self.grabbed.append(node)
                self.clones[id(node)] = self.make_clone(node)
            needed: set = set()
            for m in self.mappings:
                needed |= plan_codes(m.plan)
            self.out.prepare(needed)
            if self.settle:
                time.sleep(self.settle)                # let the desktop notice the new devices
        except BaseException:                          # whatever went wrong: give the device back
            self.stop()
            raise

    def stop(self) -> None:
        for run in self.runs.values():
            run.stop_ev.set()
        for run in self.runs.values():
            if run.is_alive():
                run.join(1.0)
        self.out.close()
        for code, clone in list(self.forwarded.items()):
            try:
                clone.write(EV_KEY, code, 0)
                clone.syn()
            except OSError:
                pass
        self.forwarded.clear()
        for clone in self.clones.values():
            try:
                clone.close()
            except OSError:
                pass
        for node in self.grabbed:
            try:
                node.ungrab()
            except OSError:
                pass
        for node in self.nodes:
            try:
                node.close()
            except OSError:
                pass
        self.clones, self.grabbed = {}, []

    # ----- event path
    def handle(self, node, e) -> None:
        clone = self.clones[id(node)]
        if e.type != EV_KEY or e.code == 0:
            clone.write_event(e)
            return
        code, value = e.code, e.value
        if value == 1:
            self.phys.add(code)
            m = self._find(code)
            if m is not None:
                self._trigger(m, code)
                return
            self.forwarded[code] = clone
            clone.write_event(e)
        elif value == 0:
            self.phys.discard(code)
            if code in self.active:
                self._release(code)
            elif code in self.swallow:
                self.swallow.discard(code)
            else:
                self.forwarded.pop(code, None)
                clone.write_event(e)
        # value 2 = kernel autorepeat: dropped. The desktop repeats held keys itself, and the
        # clone has its own repeat, so forwarding these would double the repeat rate.

    def _find(self, code: int) -> Optional[Mapping]:
        best = None
        for m in self.mappings:
            if m.combo[-1] == code and all(k in self.phys for k in m.combo[:-1]):
                if best is None or len(m.combo) > len(best.combo):
                    best = m
        return best

    def _trigger(self, m: Mapping, code: int) -> None:
        for k in m.combo[:-1]:                      # the system must not see "Ctrl" of Ctrl+X
            clone = self.forwarded.pop(k, None)
            if clone is not None:
                clone.write(EV_KEY, k, 0)
                clone.syn()
            self.swallow.add(k)
        self.active[code] = m
        if m.plan.kind == "chord":
            for c in m.plan.codes:
                self.out.press(c)
        elif m.plan.kind == "macro":
            self._start_macro(m)

    def _release(self, code: int) -> None:
        m = self.active.pop(code)
        if m.plan.kind == "chord":
            for c in reversed(m.plan.codes):
                self.out.release(c)
        elif m.plan.kind == "macro" and m.plan.mode != "toggle":
            run = self.runs.get(id(m))
            if run is not None:
                run.released.set()

    def _start_macro(self, m: Mapping) -> None:
        run = self.runs.get(id(m))
        alive = run is not None and run.is_alive()
        if m.plan.mode == "toggle" and alive:
            run.stop_ev.set()
            return
        if alive:
            return                                   # still running from the previous press
        run = MacroRun(m.plan, self.out, m.sleep_ms, self.log)
        self.runs[id(m)] = run
        run.start()


# --------------------------------------------------------------------------- engine process

def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "inputdeck-engine.sock" if runtime else Path(f"/tmp/inputdeck-{os.getuid()}.sock")


class Engine:
    def __init__(self, sock_path: Path, log: Callable = print, finder: Optional[Callable] = None,
                 opener: Optional[Callable] = None, make_uinput: Optional[Callable] = None,
                 make_clone: Optional[Callable] = None, idle_exit: float = IDLE_EXIT_SECONDS,
                 settle: float = 0.3):
        from . import recorder
        self.sock_path, self.log = Path(sock_path), log
        self.finder = finder or recorder.list_input_devices
        self.opener = opener or (lambda path: evdev.InputDevice(path))
        self.make_uinput = make_uinput or _default_uinput
        self.make_clone = make_clone or _default_clone
        self.idle_exit, self.settle = idle_exit, settle
        self.sel = selectors.DefaultSelector()
        self.injectors: dict = {}
        self.phys_all: set = set()
        self.quit = False
        self._server: Optional[socket.socket] = None
        self._idle_since: Optional[float] = time.monotonic()
        self._last_request = time.monotonic()

    # ----- device control
    def start_device(self, name: str, preset_file: str) -> dict:
        self.stop_device(name)
        groups, _denied = self.finder()
        group = next((g for g in groups if g.name == name), None)
        if group is None:
            raise EngineError(f"device “{name}” not found or not readable")
        try:
            text = Path(preset_file).read_text(encoding="utf-8").strip()
            data = json.loads(text) if text else []
        except (OSError, ValueError) as exc:
            raise EngineError(f"cannot read the preset: {exc}")
        if not isinstance(data, list):
            raise EngineError("not an input-remapper 2.x preset (expected a list)")
        mappings, warnings = compile_preset(data)
        for w in warnings:
            self.log(f"[warn] {name}: {w}")
        if not mappings:
            return {"ok": True, "msg": "no supported mappings in this preset; device left untouched",
                    "warnings": warnings}
        wanted = {c for m in mappings for c in m.combo}
        nodes = []
        try:
            for path in group.paths:
                dev = self.opener(path)
                keys = set(dev.capabilities(absinfo=False).get(EV_KEY, []))
                if keys & wanted:
                    nodes.append(dev)
                else:
                    dev.close()
        except OSError as exc:
            for dev in nodes:
                dev.close()
            raise EngineError(f"cannot open the device: {exc.strerror or exc}")
        if not nodes:
            raise EngineError("none of the mapped keys exist on this device")
        inj = Injector(name, nodes, mappings, self.make_uinput, self.make_clone, self.log, self.settle)
        inj.preset_file = preset_file
        try:
            inj.start()
        except OSError as exc:
            raise EngineError(f"could not grab the device: {exc.strerror or exc} "
                              "(is input-remapper or another remapper still running?)")
        except Exception as exc:                       # the injector already released everything
            self.log(f"[error] start {name}: {exc!r}")
            raise EngineError(f"could not start remapping ({exc!r}); the device was left untouched")
        for node in nodes:
            self.sel.register(node, selectors.EVENT_READ, inj)
        self.injectors[name] = inj
        self.log(f"[start] {name}: {len(mappings)} mapping(s) on {len(nodes)} node(s)")
        return {"ok": True, "msg": f"{len(mappings)} mapping(s) active", "warnings": warnings}

    def stop_device(self, name: str) -> None:
        inj = self.injectors.pop(name, None)
        if inj is None:
            return
        for node in inj.nodes:
            try:
                self.sel.unregister(node)
            except (KeyError, ValueError):
                pass
        inj.stop()
        self.log(f"[stop] {name}")

    def stop_all(self) -> None:
        for name in list(self.injectors):
            self.stop_device(name)

    # ----- commands
    def handle_command(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        try:
            if cmd == "hello":
                return {"ok": True, "msg": "hello"}
            if cmd == "start":
                return self.start_device(str(msg["device"]), str(msg["preset_file"]))
            if cmd == "stop":
                self.stop_device(str(msg["device"]))
                return {"ok": True, "msg": ""}
            if cmd == "stop-all":
                self.stop_all()
                return {"ok": True, "msg": ""}
            if cmd == "status":
                return {"ok": True, "msg": "", "running": {n: i.preset_file for n, i in self.injectors.items()}}
            if cmd == "quit":
                self.quit = True
                return {"ok": True, "msg": "bye"}
        except EngineError as exc:
            return {"ok": False, "msg": str(exc)}
        except KeyError as exc:
            return {"ok": False, "msg": f"missing field {exc}"}
        except Exception as exc:
            self.log(f"[error] {cmd}: {exc!r}")
            return {"ok": False, "msg": f"internal error: {exc}"}
        return {"ok": False, "msg": f"unknown command {cmd!r}"}

    # ----- serving
    def bind(self) -> None:
        if self.sock_path.exists():
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.connect(str(self.sock_path))
                raise EngineError("another InputDeck engine is already running")
            except OSError:
                self.sock_path.unlink(missing_ok=True)       # stale socket
            finally:
                probe.close()
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        server = socket.socket(socket.AF_UNIX)
        old_umask = os.umask(0o177)
        try:
            server.bind(str(self.sock_path))
        finally:
            os.umask(old_umask)
        server.listen(4)
        server.setblocking(False)
        self.sel.register(server, selectors.EVENT_READ, None)
        self._server = server

    def pump(self, timeout: float = 0.2) -> None:
        for key, _mask in self.sel.select(timeout):
            if key.data is None:
                self._accept()
            else:
                self._read_node(key.fileobj, key.data)
        now = time.monotonic()
        if self.injectors:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = now
        elif self.idle_exit and now - max(self._idle_since, self._last_request) > self.idle_exit:
            self.log("no devices remapped and nobody asked for a while: exiting")
            self.quit = True

    def _accept(self) -> None:
        try:
            conn, _addr = self._server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2.0)
            data = b""
            try:
                while b"\n" not in data and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                msg = json.loads(data.decode("utf-8") or "{}")
                self._last_request = time.monotonic()          # any client talking to us = still wanted
                if msg.get("cmd") not in ("status", "hello"):  # polled often: keep the log readable
                    self.log(f"[request] {msg.get('cmd')} from {self._peer(conn)}")
                resp = self.handle_command(msg)
            except (OSError, ValueError) as exc:
                resp = {"ok": False, "msg": f"bad request: {exc}"}
            try:
                conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
            except OSError:
                pass

    @staticmethod
    def _peer(conn) -> str:
        """Who is on the other end of the socket (pid + command line), for the log."""
        try:
            pid, _uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                 struct.calcsize("3i")))
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            return f"pid {pid} ({cmd.strip()[:70]})"
        except (OSError, struct.error, AttributeError):
            return "unknown client"

    def _read_node(self, dev, inj: Injector) -> None:
        try:
            events = list(dev.read())
        except BlockingIOError:
            return
        except OSError:
            self.log(f"[warn] {inj.name}: device went away")
            self.stop_device(inj.name)
            return
        for e in events:
            if e.type == EV_KEY and e.value in (0, 1):
                (self.phys_all.add if e.value else self.phys_all.discard)(e.code)
                if PANIC_CHORD <= self.phys_all:
                    self.log("[panic] Ctrl+Alt+Shift+Esc — all remapping stopped")
                    self.stop_all()
                    self.phys_all.clear()
                    return
            try:
                inj.handle(dev, e)
            except Exception as exc:                 # fail safe: give the device back
                self.log(f"[error] {inj.name}: {exc!r} — stopping remapping for it")
                self.stop_device(inj.name)
                return

    def run(self) -> None:
        self.bind()
        self.log(f"engine listening on {self.sock_path}")
        try:
            while not self.quit:
                self.pump(0.2)
        finally:
            self.stop_all()
            if self._server is not None:
                try:
                    self.sel.unregister(self._server)
                except (KeyError, ValueError):
                    pass
                self._server.close()
            self.sock_path.unlink(missing_ok=True)


def run_engine(paths: core.Paths) -> int:
    import signal

    def log(msg: str) -> None:
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    engine = Engine(socket_path(), log)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(engine, "quit", True))
    try:
        engine.run()
    except EngineError as exc:
        log(str(exc))
        return 1
    return 0


# --------------------------------------------------------------------------- client

def uinput_writable() -> bool:
    return os.access("/dev/uinput", os.W_OK)


UINPUT_HINT = ("./install.sh --setup-access   (adds you to the 'input' group and a udev rule for "
               "/dev/uinput; then log out and in)")


class BuiltinControl:
    """Same interface as core.RemapperControl, but talks to InputDeck's own engine process."""

    kind = "builtin"

    def __init__(self, paths: core.Paths, sock_path: Optional[Path] = None,
                 spawn: Optional[Callable] = None):
        self.paths = paths
        self.sock = Path(sock_path) if sock_path else socket_path()
        self._spawn = spawn or self._spawn_engine
        self.running: dict = {}
        self.last_warnings: list = []

    # ----- plumbing
    def _spawn_engine(self) -> None:
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        core.rotate_log(self.paths.data_dir / "engine.log")
        log = open(self.paths.data_dir / "engine.log", "a")
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        subprocess.Popen([sys.executable, "-m", "inputdeck", "--engine"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, env=env)

    def _send(self, msg: dict, timeout: float = 10.0) -> dict:
        s = socket.socket(socket.AF_UNIX)
        try:
            s.settimeout(timeout)
            s.connect(str(self.sock))
            s.sendall(json.dumps(msg).encode("utf-8") + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            return json.loads(data.decode("utf-8"))
        finally:
            s.close()

    def _request(self, msg: dict, spawn: bool = True) -> dict:
        try:
            return self._send(msg)
        except (OSError, ValueError):
            if not spawn:
                raise
        self._spawn()
        deadline = time.monotonic() + 5.0
        last: Exception = OSError("engine did not start")
        while time.monotonic() < deadline:
            try:
                return self._send(msg)
            except (OSError, ValueError) as exc:
                last = exc
                time.sleep(0.1)
        raise OSError(f"the engine did not start ({last}); see {self.paths.data_dir / 'engine.log'}")

    def _call(self, msg: dict, spawn: bool = True) -> tuple:
        try:
            resp = self._request(msg, spawn)
        except OSError as exc:
            return 1, str(exc)
        self.last_warnings = list(resp.get("warnings") or [])
        text = resp.get("msg", "")
        if self.last_warnings:
            text += ("\n" if text else "") + "\n".join(self.last_warnings)
        return (0 if resp.get("ok") else 1), text

    # ----- the RemapperControl interface
    def available(self) -> bool:
        return evdev is not None and uinput_writable()

    def hello(self) -> bool:
        return self._call({"cmd": "hello"})[0] == 0

    def start(self, device: str, preset: str) -> tuple:
        preset_file = self.paths.presets_dir / device / f"{preset}.json"
        res = self._call({"cmd": "start", "device": device, "preset_file": str(preset_file)})
        if res[0] == 0:
            self.running[device] = preset
        return res

    def stop(self, device: str) -> tuple:
        res = self._call({"cmd": "stop", "device": device}, spawn=False) if self._reachable() else (0, "")
        self.running.pop(device, None)
        return res

    def stop_all(self) -> tuple:
        res = self._call({"cmd": "stop-all"}, spawn=False) if self._reachable() else (0, "")
        self.running.clear()
        return res

    def autoload(self) -> tuple:
        return 0, ""

    def refresh_running(self) -> None:
        """Ask the engine what it is really doing (it outlives the GUI). Never starts it."""
        try:
            resp = self._send({"cmd": "status"}, timeout=1.0)
        except (OSError, ValueError):
            self.running = {}
            return
        self.running = {name: Path(f).stem for name, f in (resp.get("running") or {}).items()}

    # ----- extras
    def _reachable(self) -> bool:
        try:
            self._send({"cmd": "hello"})
            return True
        except (OSError, ValueError):
            return False

    def shutdown_engine(self) -> None:
        if self._reachable():
            self._call({"cmd": "quit"}, spawn=False)
            self.running.clear()

    def report(self, paths: core.Paths) -> list:
        ok_evdev = evdev is not None
        return [
            (ok_evdev, "python-evdev installed", core.python_deps_hint("evdev")),
            (core.can_read_input_devices(), "permission to read /dev/input/event*",
             "sudo usermod -aG input $USER   (then log out and back in)"),
            (uinput_writable(), "permission to create virtual devices (/dev/uinput)", UINPUT_HINT),
            (True, f"presets are kept in {paths.presets_dir} (created on demand)", ""),
            (True, "engine: " + ("running" if self._reachable() else "not running (starts automatically)"), ""),
        ]
