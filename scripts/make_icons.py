#!/usr/bin/env python3
"""Draw the Mousehare app icon and produce platform icon files."""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".build" / "icons"


def draw(size: int) -> Image.Image:
    scale = size / 256
    image = Image.new("RGBA", (size, size), (18, 17, 15, 255))
    d = ImageDraw.Draw(image)
    amber = (216, 121, 50, 255)
    bronze = (99, 64, 31, 255)
    cream = (240, 222, 197, 255)
    pad = int(16 * scale)
    d.rounded_rectangle((pad, pad, size - pad, size - pad), radius=int(48 * scale),
                        outline=bronze, width=max(1, int(3 * scale)))
    cx, cy = size // 2, size // 2
    # A mouse/device silhouette inside a restrained TVA-like target ring.
    d.ellipse((int(62 * scale), int(37 * scale), int(194 * scale), int(219 * scale)),
              outline=amber, width=max(2, int(8 * scale)))
    d.rounded_rectangle((int(86 * scale), int(57 * scale), int(170 * scale), int(194 * scale)),
                        radius=int(42 * scale), fill=(36, 32, 25, 255), outline=cream,
                        width=max(2, int(5 * scale)))
    d.line((cx, int(61 * scale), cx, int(106 * scale)), fill=amber,
           width=max(2, int(6 * scale)))
    d.line((int(38 * scale), cy, int(74 * scale), cy), fill=bronze,
           width=max(1, int(3 * scale)))
    d.line((int(182 * scale), cy, int(218 * scale), cy), fill=bronze,
           width=max(1, int(3 * scale)))
    d.ellipse((cx - int(7 * scale), cy - int(7 * scale), cx + int(7 * scale),
               cy + int(7 * scale)), fill=amber)
    return image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("macos", "windows", "all"), default="all")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    image = draw(1024)
    image.save(OUT / "mouseshare.png")
    if args.platform in ("windows", "all"):
        image.resize((256, 256), Image.Resampling.LANCZOS).save(OUT / "mouseshare.ico")
    if args.platform in ("macos", "all"):
        # Pillow writes the supported ICNS size representations from the
        # 1024px master, without depending on Apple's iconutil version.
        image.save(OUT / "mouseshare.icns", format="ICNS")


if __name__ == "__main__":
    main()
