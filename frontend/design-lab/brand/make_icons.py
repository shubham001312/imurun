"""Raster brand assets for the IMURUN shell (frontend/design-lab), from logo.svg.

    python frontend/design-lab/brand/make_icons.py            # logo.ico + logo-512.png
    python frontend/design-lab/brand/make_icons.py --svg other.svg --out some/folder
    python frontend/design-lab/brand/make_icons.py --check     # aperture.js == brand/vanilla/aperture.js?

Source of truth: the ImuTechz website. ``logo.svg`` is the website's
``app/icon.svg`` byte for byte, which is the Aperture's resting frame
(Mark.jsx ``bladePaths(REST)`` / ``pupilPath(REST)`` at the small, 7.6 px
weight). The live mark in the app is drawn by ``aperture.js`` from the same
math; the frontend checks compare its resting frame with this file.

Raster files (need Playwright with Microsoft Edge or its Chromium, and
Pillow). Each size is rendered separately by a real browser engine (so
16/24 px stay crisp), on a transparent ground:

    logo-512.png   512 px (apple-touch-icon, GTK/Qt window icon)
    logo.ico       16, 24, 32, 48, 64, 128, 256 px, every entry a 32-bit BMP
                   (DIB) rather than PNG-compressed: Win32 LoadImage, the
                   WinForms Icon class pywebview uses, and old shell code
                   paths load DIB entries everywhere, while PNG entries below
                   256 px have crashed icon loaders before.

This file is not served and not loaded by the app.
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
KIT_APERTURE = HERE.parents[1] / "brand" / "vanilla" / "aperture.js"   # the brand source
APP_APERTURE = HERE / "aperture.js"                                   # the served copy


def check_aperture() -> int:
    """The served aperture.js must be the brand kit's, byte for byte."""
    if not KIT_APERTURE.is_file():
        print(f"brand kit not found at {KIT_APERTURE}")
        return 1
    if KIT_APERTURE.read_bytes() != APP_APERTURE.read_bytes():
        print(f"{APP_APERTURE} differs from {KIT_APERTURE}: edit the kit, then copy it over")
        return 1
    print("aperture.js matches the brand kit")
    return 0
SIZES = (16, 24, 32, 48, 64, 128, 256)
def render(svg: str, sizes: tuple[int, ...]) -> dict[int, bytes]:
    from playwright.sync_api import sync_playwright

    out: dict[int, bytes] = {}
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge")
        except Exception:  # noqa: BLE001 - no Edge: the bundled Chromium
            browser = p.chromium.launch()
        page = browser.new_page(device_scale_factor=1, color_scheme="light")
        for size in sizes:
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(
                "<!doctype html><html><body style='margin:0;background:transparent'>"
                f"<div style='width:{size}px;height:{size}px'>{svg}</div></body></html>"
            )
            page.eval_on_selector("svg", f"s => {{ s.setAttribute('width', {size}); s.setAttribute('height', {size}); }}")
            out[size] = page.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size})
        browser.close()
    return out


def bmp_entry(image) -> bytes:
    """One ICO image as a 32-bit BI_RGB DIB: header, bottom-up BGRA, AND mask."""
    w, h = image.size
    rgba = image.convert("RGBA")
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b, a = rgba.getpixel((x, y))
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    mask_stride = ((w + 31) // 32) * 4
    mask_rows = []
    for y in range(h - 1, -1, -1):
        bits = bytearray(mask_stride)
        for x in range(w):
            if rgba.getpixel((x, y))[3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask_rows.append(bytes(bits))
    return header + b"".join(rows) + b"".join(mask_rows)


def write_ico(path: Path, images: dict) -> None:
    entries = [(size, bmp_entry(images[size])) for size in SIZES]
    offset = 6 + 16 * len(entries)
    head = struct.pack("<HHH", 0, 1, len(entries))
    table = b""
    for size, data in entries:
        dim = 0 if size >= 256 else size
        table += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.write_bytes(head + table + b"".join(data for _, data in entries))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IMURUN brand assets from logo.svg")
    parser.add_argument("--svg", type=Path, default=HERE / "logo.svg")
    parser.add_argument("--out", type=Path, default=HERE)
    parser.add_argument("--check", action="store_true",
                        help="only check that aperture.js equals brand/vanilla/aperture.js (exit 1 if not)")
    args = parser.parse_args(argv)
    if args.check:
        return check_aperture()
    svg = args.svg.read_text(encoding="utf-8")
    if "<svg" not in svg:
        print(f"{args.svg} is not an SVG", file=sys.stderr)
        return 2
    from PIL import Image

    pngs = render(svg, SIZES + (512,))
    (args.out / "logo-512.png").write_bytes(pngs[512])
    images = {s: Image.open(io.BytesIO(pngs[s])).convert("RGBA") for s in SIZES}
    write_ico(args.out / "logo.ico", images)
    print(f"wrote {args.out / 'logo-512.png'} and {args.out / 'logo.ico'} ({', '.join(map(str, SIZES))} px, BMP entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
