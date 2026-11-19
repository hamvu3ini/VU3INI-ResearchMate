"""Logo + favicon generator.

  python scripts/make_logo.py                    # (re)build favicon from frontend/assets/logo.png,
                                                 # or draw a crisp fallback logo if logo.png is missing/corrupt
  python scripts/make_logo.py path\\to\\image.png  # use your own image as the new logo
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

ASSETS = Path(__file__).resolve().parent.parent / "frontend" / "assets"


def draw_fallback(size: int = 256) -> Image.Image:
    s = size / 128
    im = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    d.ellipse([24 * s, 14 * s, 104 * s, 94 * s], fill=(58, 151, 216), outline=(43, 108, 176), width=int(3 * s))
    d.pieslice([40 * s, 70 * s, 88 * s, 118 * s], 180, 360, fill=(211, 47, 47))
    d.ellipse([48 * s, 34 * s, 80 * s, 66 * s], fill=(242, 180, 140))
    d.chord([44 * s, 22 * s, 86 * s, 54 * s], 180, 360, fill=(58, 58, 58))
    try:
        font = ImageFont.truetype("arialbd.ttf", int(20 * s))
    except OSError:
        font = ImageFont.load_default()
    d.text((64 * s, 110 * s), "VU3INI", fill=(17, 17, 17), font=font, anchor="mm")
    return im


def build(src: Path | None = None) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    logo_path = ASSETS / "logo.png"
    try:
        im = Image.open(src or logo_path).convert("RGBA")
    except (FileNotFoundError, UnidentifiedImageError, OSError):
        im = draw_fallback()
    # square-pad, then downscale to a crisp 256 px logo and 64 px favicon
    side = max(im.size)
    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    logo = canvas.resize((256, 256), Image.LANCZOS)
    logo.save(logo_path, optimize=True)
    canvas.resize((64, 64), Image.LANCZOS).save(ASSETS / "favicon.png")
    print(f"logo.png and favicon.png written to {ASSETS}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
