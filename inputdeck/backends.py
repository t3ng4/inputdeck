"""Chooses which engine applies presets: input-remapper's daemon or InputDeck's built-in one."""
from __future__ import annotations

from . import core, engine

CHOICES = [
    ("auto", "Automatic (input-remapper if installed, otherwise built-in)"),
    ("input-remapper", "input-remapper daemon"),
    ("builtin", "Built-in engine (experimental, no input-remapper needed)"),
]


def make_control(settings: dict, paths: core.Paths):
    choice = settings.get("backend", "auto")
    if choice == "builtin":
        return engine.BuiltinControl(paths)
    remapper = core.RemapperControl()
    if choice == "input-remapper" or remapper.available():
        return remapper
    return engine.BuiltinControl(paths)


def label(control) -> str:
    return "built-in engine (experimental)" if getattr(control, "kind", "") == "builtin" else "input-remapper"
