#!/usr/bin/env python3
"""Draw the YOLO app icon into an .icns file.

Keeps the repo free of binary art: the icon is drawn at install time, so it
renders at the display's own resolution and needs no asset checked in.

Two lines of YO / LO in a rounded square, on a warm gradient -- four letters
across one 16pt tile would be unreadable, and this app's icon lives in the
menubar and the Dock where small sizes matter most.

Usage: make_icon.py <output.icns>
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGradient,
    NSGraphicsContext,
    NSMakeRect,
    NSString,
)

SIZES = [16, 32, 64, 128, 256, 512, 1024]
TOP, BOTTOM = "YO", "LO"

# Warm orange-to-red: "YOLO" should not look like a system utility.
START = (1.00, 0.58, 0.16)
END = (0.91, 0.24, 0.28)


def draw(size: int) -> bytes:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0
    )
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)

    inset = size * 0.055
    radius = size * 0.225
    tile = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(inset, inset, size - inset * 2, size - inset * 2), radius, radius
    )
    NSGradient.alloc().initWithStartingColor_endingColor_(
        NSColor.colorWithSRGBRed_green_blue_alpha_(*START, 1.0),
        NSColor.colorWithSRGBRed_green_blue_alpha_(*END, 1.0),
    ).drawInBezierPath_angle_(tile, -90.0)

    # Heavy weight and tight tracking so the letters still read at 16pt.
    font_size = size * 0.30
    attrs = {
        NSFontAttributeName: NSFont.systemFontOfSize_weight_(font_size, 0.62),
        NSForegroundColorAttributeName: NSColor.whiteColor(),
    }
    line_gap = font_size * 0.94
    for i, text in enumerate((TOP, BOTTOM)):
        s = NSString.stringWithString_(text)
        w, h = s.sizeWithAttributes_(attrs)
        x = (size - w) / 2
        y = size / 2 - line_gap * (i + 1) + line_gap * 0.98 + (line_gap - h) / 2
        s.drawAtPoint_withAttributes_((x, y), attrs)

    NSGraphicsContext.restoreGraphicsState()
    return rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})


def main() -> int:
    out = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for s in SIZES:
            name = f"icon_{s}x{s}.png" if s <= 512 else "icon_512x512@2x.png"
            (iconset / name).write_bytes(bytes(draw(s)))
            if s * 2 in SIZES and s <= 256:
                (iconset / f"icon_{s}x{s}@2x.png").write_bytes(bytes(draw(s * 2)))
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(out)],
            check=True,
        )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
