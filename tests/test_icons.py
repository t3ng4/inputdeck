import struct
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inputdeck import icons  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Rec:
    def __init__(self):
        self.ops = []

    def moveTo(self, x, y): self.ops.append(("M", x, y))
    def lineTo(self, x, y): self.ops.append(("L", x, y))
    def cubicTo(self, *a): self.ops.append(("C",) + a)
    def quadTo(self, *a): self.ops.append(("Q",) + a)
    def closeSubpath(self): self.ops.append(("Z",))


class IconDataTests(unittest.TestCase):
    def test_path_parser(self):
        r = Rec()
        icons.build_path("M5 19 L6 15 H16 V5 Z", r)
        self.assertEqual(r.ops, [("M", 5, 19), ("L", 6, 15), ("L", 16, 15), ("L", 16, 5), ("Z",)])
        r = Rec()
        icons.build_path("M1 2 3 4 C1 2 3 4 5 6 Q1 2 3 4", r)       # implicit lineto after M
        self.assertEqual(r.ops, [("M", 1, 2), ("L", 3, 4), ("C", 1, 2, 3, 4, 5, 6), ("Q", 1, 2, 3, 4)])
        with self.assertRaises(ValueError):
            icons.build_path("L 1", Rec())

    def test_every_icon_parses_and_stays_on_the_grid(self):
        for name, spec in icons.ICONS.items():
            for prim in spec["prims"]:
                self.assertIn(prim[-1], ("s", "f"), name)
                if prim[0] == "path":
                    r = Rec()
                    icons.build_path(prim[1], r)
                    self.assertTrue(r.ops, name)
                    for op in r.ops:
                        for v in op[1:]:
                            self.assertTrue(0 <= v <= 24, (name, op))

    def test_svgs_are_valid_xml(self):
        for name in icons.ICONS:
            ET.fromstring(icons.svg(name))


class AppIconFiles(unittest.TestCase):
    SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)

    @staticmethod
    def png_header(path):
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", path
        width, height = struct.unpack(">II", data[16:24])
        return width, height, data[25]                              # colour type 6 = RGBA

    def test_every_size_exists_with_the_right_dimensions_and_alpha(self):
        for size in self.SIZES:
            path = icons.ICON_DIR / f"inputdeck-{size}.png"
            self.assertTrue(path.is_file(), path)
            self.assertEqual(self.png_header(path), (size, size, 6), path)

    def test_corners_are_transparent_and_body_is_opaque(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        im = Image.open(icons.ICON_DIR / "inputdeck-512.png")
        self.assertEqual(im.getpixel((0, 0))[3], 0)
        self.assertEqual(im.getpixel((511, 511))[3], 0)
        self.assertEqual(im.getpixel((256, 256))[3], 255)


class QtRenderSmoke(unittest.TestCase):
    """Runs the painter code paths under the fake Qt (catches typos, not Qt API misuse)."""

    def test_all_icons_render(self):
        import qt_stub
        qt_stub.install()
        import importlib
        importlib.reload(icons)
        for name in icons.ICONS:
            icons._cache.clear()
            self.assertIsNotNone(icons.qt_icon(name))
        self.assertIsNotNone(icons.app_icon())


if __name__ == "__main__":
    unittest.main()
