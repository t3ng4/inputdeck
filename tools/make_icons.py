"""Builds the app icon set from a master PNG (dev tool; needs Pillow + numpy).

    python3 tools/make_icons.py [master.png]

* Turns the opaque black corners of a rounded-square icon into real transparency
  (anti-aliased, no dark halo).
* Writes inputdeck/data/icons/inputdeck-<size>.png for the sizes below.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "inputdeck" / "data" / "icons"
SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)
MASTER_SIZE = 1024


def cut_corners(im: Image.Image) -> Image.Image:
    im = im.convert("RGBA")
    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    h, w, _ = rgb.shape
    near_black = rgb.max(axis=2) < 14
    outside = np.zeros((h, w), bool)                      # black region connected to a corner
    for y, x in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)):
        outside[y, x] = near_black[y, x]
    while True:                                           # grow within the black pixels
        grown = outside.copy()
        grown[1:, :] |= outside[:-1, :]
        grown[:-1, :] |= outside[1:, :]
        grown[:, 1:] |= outside[:, :-1]
        grown[:, :-1] |= outside[:, 1:]
        grown &= near_black
        if (grown == outside).all():
            break
        outside = grown

    # the tile colour, sampled well inside the image
    body = rgb[h // 2 - 5:h // 2 + 5, 20:30].reshape(-1, 3).mean(axis=0)
    # band of pixels touching the outside region: partially covered by the tile
    band = np.asarray(Image.fromarray(outside.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(7))) > 0
    band &= ~outside
    cover = np.clip((rgb @ body) / float(body @ body), 0.0, 1.0)      # black -> 0, tile colour -> 1
    alpha = np.full((h, w), 255, np.float32)
    alpha[outside] = 0
    alpha[band] = (cover[band] * 255).clip(0, 255)
    out = np.asarray(im).astype(np.float32).copy()
    safe = np.where(alpha > 0, alpha, 1.0)[..., None] / 255.0
    edge = band & (alpha > 0) & (alpha < 255)
    out[..., :3][edge] = np.clip(rgb[edge] / safe[edge], 0, 255)      # un-blend from black
    out[..., 3] = alpha
    return Image.fromarray(out.round().astype(np.uint8), "RGBA")


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tools" / "icon-master.png"
    master = cut_corners(Image.open(src))
    master = master.resize((MASTER_SIZE, MASTER_SIZE), Image.LANCZOS)
    master.save(ROOT / "tools" / "icon-master.png", optimize=True)
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        master.resize((size, size), Image.LANCZOS).save(OUT / f"inputdeck-{size}.png", optimize=True)
    print("wrote", ", ".join(str(s) for s in SIZES))


if __name__ == "__main__":
    main()
