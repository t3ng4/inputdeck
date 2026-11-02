"""InputDeck's own icons, so nothing depends on the desktop's icon theme.

The small UI icons are plain vector data on a 24x24 grid (stroke style, round caps), drawn
with QPainter at runtime and tinted with the current palette, so they look right in light and
dark themes. Drawing with QPainter avoids needing the optional QtSvg module, which some
distros package separately. The application icon itself is a set of PNGs (data/icons).

Primitives: ("path", d, mode) | ("circle", cx, cy, r, mode) | ("rect", x, y, w, h, rx, mode)
mode: "s" = stroke, "f" = fill. Path data supports absolute M L H V C Q Z only.
"""
from __future__ import annotations

import re
from pathlib import Path

ICON_DIR = Path(__file__).resolve().parent / "data" / "icons"

ICONS: dict = {
    "record": {"color": "#e5484d", "prims": [("circle", 12, 12, 7, "f")]},
    "stop": {"prims": [("rect", 6, 6, 12, 12, 2, "f")]},
    "add": {"prims": [("path", "M12 5 V19 M5 12 H19", "s")]},
    "remove": {"prims": [("path", "M6 6 L18 18 M18 6 L6 18", "s")]},
    "edit": {"prims": [("path", "M5 19 L6 15 L16 5 L19 8 L9 18 Z", "s"),
                       ("path", "M14 7 L17 10", "s")]},
    "delete": {"prims": [("path", "M5 7 H19", "s"), ("path", "M10 7 V4 H14 V7", "s"),
                         ("path", "M7 7 L8 20 H16 L17 7", "s")]},
    "apply": {"prims": [("path", "M8 5 L19 12 L8 19 Z", "f")]},
    "refresh": {"prims": [("path", "M19 12 C19 15.9 15.9 19 12 19 C8.1 19 5 15.9 5 12 "
                                   "C5 8.1 8.1 5 12 5 C14 5 15.8 5.8 17 7", "s"),
                          ("path", "M17 3 V7 H13", "s")]},
    "new": {"prims": [("path", "M7 3 H14 L18 7 V21 H7 Z", "s"), ("path", "M14 3 V7 H18", "s"),
                      ("path", "M12.5 11 V17 M9.5 14 H15.5", "s")]},
    "duplicate": {"prims": [("rect", 8, 8, 12, 12, 2, "s"), ("path", "M16 8 V5 H5 V16 H8", "s")]},
    "up": {"prims": [("path", "M6 15 L12 9 L18 15", "s")]},
    "down": {"prims": [("path", "M6 9 L12 15 L18 9", "s")]},
    "hotkey": {"prims": [("rect", 3, 5, 18, 14, 3, "s"), ("path", "M8 14.5 H16", "s"),
                         ("circle", 8, 10, 1, "f"), ("circle", 12, 10, 1, "f"),
                         ("circle", 16, 10, 1, "f")]},
    "macro": {"prims": [("circle", 12, 12, 8, "s"), ("circle", 12, 12, 3, "f")]},
    "keyboard": {"prims": [("rect", 2.5, 6, 19, 12, 2.5, "s"),
                           ("circle", 6.5, 10, 1, "f"), ("circle", 10, 10, 1, "f"),
                           ("circle", 14, 10, 1, "f"), ("circle", 17.5, 10, 1, "f"),
                           ("path", "M8 14.5 H16", "s")]},
    "mouse": {"prims": [("rect", 6.5, 3, 11, 18, 5.5, "s"), ("path", "M12 3 V9.5 M6.5 9.5 H17.5", "s")]},
    "gamepad": {"prims": [("rect", 2.5, 7.5, 19, 10, 5, "s"), ("path", "M8 10.5 V14.5 M6 12.5 H10", "s"),
                          ("circle", 15.5, 11.5, 1, "f"), ("circle", 18, 14, 1, "f")]},
    "media": {"prims": [("path", "M5 15 V12 C5 8.1 8.1 5 12 5 C15.9 5 19 8.1 19 12 V15", "s"),
                        ("rect", 3.5, 14, 4, 7, 1.5, "f"), ("rect", 16.5, 14, 4, 7, 1.5, "f")]},
    "active": {"color": "#2ecc71", "prims": [("circle", 12, 12, 6, "f")]},
    "other": {"prims": [("rect", 5, 5, 14, 14, 3, "s"), ("circle", 12, 12, 1.5, "f")]},
    "switch": {"prims": [("path", "M4 8 H18 M14 4 L18 8 L14 12", "s"),
                         ("path", "M20 16 H6 M10 12 L6 16 L10 20", "s")]},
}

# ------------------------------------------------------------------ SVG export


def svg(name: str, color: str = "#222222", size: int = 24) -> str:
    spec = ICONS[name]
    color = spec.get("color") or color
    body = []
    for prim in spec["prims"]:
        kind, mode = prim[0], prim[-1]
        paint = (f'fill="{color}" stroke="none"' if mode == "f" else
                 f'fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" '
                 f'stroke-linejoin="round"')
        if kind == "path":
            body.append(f'<path d="{prim[1]}" {paint}/>')
        elif kind == "circle":
            body.append(f'<circle cx="{prim[1]}" cy="{prim[2]}" r="{prim[3]}" {paint}/>')
        else:
            body.append(f'<rect x="{prim[1]}" y="{prim[2]}" width="{prim[3]}" height="{prim[4]}" '
                        f'rx="{prim[5]}" {paint}/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 24 24">{"".join(body)}</svg>\n')


# ------------------------------------------------------------------ path parsing


def build_path(d: str, path) -> None:
    """Feed SVG-style path data (absolute M L H V C Q Z) into `path`, which needs
    moveTo/lineTo/cubicTo/quadTo/closeSubpath (a QPainterPath, or a fake in tests)."""
    tokens = re.findall(r"[MLHVCQZ]|-?\d*\.?\d+", d)
    arity = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "Q": 4, "Z": 0}
    i, cmd, cx, cy = 0, None, 0.0, 0.0
    while i < len(tokens):
        if tokens[i] in arity:
            cmd = tokens[i]
            i += 1
            if cmd == "Z":
                path.closeSubpath()
                continue
        if cmd is None or cmd == "Z":
            raise ValueError(f"bad path data: {d!r}")
        n = arity[cmd]
        args = [float(t) for t in tokens[i:i + n]]
        if len(args) != n:
            raise ValueError(f"bad path data: {d!r}")
        i += n
        if cmd == "M":
            cx, cy = args
            path.moveTo(cx, cy)
            cmd = "L"                       # extra pairs after M are implicit lineto
        elif cmd == "L":
            cx, cy = args
            path.lineTo(cx, cy)
        elif cmd == "H":
            cx = args[0]
            path.lineTo(cx, cy)
        elif cmd == "V":
            cy = args[0]
            path.lineTo(cx, cy)
        elif cmd == "C":
            path.cubicTo(*args)
            cx, cy = args[4], args[5]
        elif cmd == "Q":
            path.quadTo(*args)
            cx, cy = args[2], args[3]


# ------------------------------------------------------------------ Qt rendering

_SIZES = (16, 24, 32, 48, 64)
_cache: dict = {}


def _paint_icon(p, name: str, color, scale: float) -> None:
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import QBrush, QColor, QPainterPath, QPen

    spec = ICONS[name]
    col = QColor(spec["color"]) if spec.get("color") else QColor(color)
    if spec.get("color") and color.alpha() < 255:
        col.setAlpha(color.alpha())
    p.scale(scale, scale)
    pen = QPen(col, 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    for prim in spec["prims"]:
        kind, mode = prim[0], prim[-1]
        if mode == "s":
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(col))
        if kind == "path":
            path = QPainterPath()
            build_path(prim[1], path)
            p.drawPath(path)
        elif kind == "circle":
            p.drawEllipse(QPointF(prim[1], prim[2]), prim[3], prim[3])
        else:
            p.drawRoundedRect(QRectF(prim[1], prim[2], prim[3], prim[4]), prim[5], prim[5])


def qt_icon(name: str):
    """A QIcon tinted with the current palette's button-text colour (cached per colour)."""
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    base = QApplication.palette().color(QPalette.ColorRole.ButtonText)
    key = (name, base.rgba())
    if key in _cache:
        return _cache[key]
    icon = QIcon()
    for mode, alpha in ((QIcon.Mode.Normal, 255), (QIcon.Mode.Disabled, 100)):
        color = QColor(base)
        color.setAlpha(alpha)
        for size in _SIZES:
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            _paint_icon(p, name, color, size / 24.0)
            p.end()
            icon.addPixmap(pm, mode)
    _cache[key] = icon
    return icon


def app_icon():
    """The application / window / tray icon: the PNG set shipped in inputdeck/data/icons."""
    from PyQt6.QtGui import QIcon

    if "app" not in _cache:
        icon = QIcon()
        for path in sorted(ICON_DIR.glob("inputdeck-*.png")):
            icon.addFile(str(path))
        _cache["app"] = icon
    return _cache["app"]
