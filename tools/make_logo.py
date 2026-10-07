"""Renders the logo (app/ui/logo.py) into app/assets/logo.png and app/assets/logo.ico (used by the EXE build).
Run once after changing the drawing:  python tools/make_logo.py   (needs PyQt6 and Pillow)"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image                      # noqa: E402
from PyQt6.QtGui import QGuiApplication     # noqa: E402

from app.ui.logo import SIZES, render_logo  # noqa: E402


def main():
    _app = QGuiApplication([])
    out = ROOT / "app" / "assets"
    out.mkdir(parents=True, exist_ok=True)
    images = []
    for size in SIZES:
        path = out / f"_logo_{size}.png"
        render_logo(size).save(str(path))
        images.append(Image.open(path).convert("RGBA"))
    big = images[-1]
    big.save(out / "logo.png")
    big.save(out / "logo.ico", format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    for size in SIZES:
        (out / f"_logo_{size}.png").unlink()
    print("written:", out / "logo.png", out / "logo.ico")


if __name__ == "__main__":
    main()
