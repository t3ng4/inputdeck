"""Rasterise icons.ICONS with matplotlib to eyeball them (dev tool, not shipped logic)."""
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import PathPatch, Circle, FancyBboxPatch
from matplotlib.path import Path as MPath
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from inputdeck import icons  # noqa: E402


class Rec:
    def __init__(self):
        self.v, self.c, self.cur = [], [], (0, 0)
    def moveTo(self, x, y): self.v.append((x, y)); self.c.append(MPath.MOVETO)
    def lineTo(self, x, y): self.v.append((x, y)); self.c.append(MPath.LINETO)
    def cubicTo(self, a, b, c, d, e, f): self.v += [(a, b), (c, d), (e, f)]; self.c += [MPath.CURVE4] * 3
    def quadTo(self, a, b, c, d): self.v += [(a, b), (c, d)]; self.c += [MPath.CURVE3] * 2
    def closeSubpath(self): self.v.append(self.v[0] if self.v else (0, 0)); self.c.append(MPath.CLOSEPOLY)


def draw(ax, name, color):
    spec = icons.ICONS[name]
    col = spec.get("color") or color
    for prim in spec["prims"]:
        kind, mode = prim[0], prim[-1]
        kw = dict(facecolor=col, edgecolor="none") if mode == "f" else dict(
            facecolor="none", edgecolor=col, linewidth=2 * 72 * 1.0 / 24 * ax.figure.get_figwidth() / 1,
            capstyle="round", joinstyle="round")
        if mode == "s":
            kw["linewidth"] = 2.0 * (ax.get_window_extent().width / 24.0) * 72 / ax.figure.dpi
        if kind == "path":
            # split on M so closed/open subpaths stay separate
            for chunk in re.findall(r"M[^M]*", prim[1]):
                r = Rec(); icons.build_path(chunk, r)
                ax.add_patch(PathPatch(MPath(r.v, r.c), **kw))
        elif kind == "circle":
            ax.add_patch(Circle((prim[1], prim[2]), prim[3], **kw))
        else:
            x, y, w, h, rx = prim[1:6]
            ax.add_patch(FancyBboxPatch((x + rx, y + rx), w - 2 * rx, h - 2 * rx,
                                        boxstyle=f"round,pad={rx},rounding_size={rx}", **kw))
    ax.set_xlim(0, 24); ax.set_ylim(24, 0); ax.set_aspect("equal"); ax.axis("off")


names = list(icons.ICONS)
cols = 7
rows = (len(names) + cols - 1) // cols
fig = plt.figure(figsize=(cols * 1.6, rows * 1.8), dpi=100)
for i, n in enumerate(names):
    ax = fig.add_subplot(rows, cols, i + 1)
    ax.set_facecolor("white")
    draw(ax, n, "#222222")
    ax.set_title(n, fontsize=8)
fig.tight_layout()
fig.savefig(sys.argv[1] if len(sys.argv) > 1 else "/tmp/icons.png")
print("saved")
