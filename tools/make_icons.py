"""Draw the three programs' icons into resources/icons/*.ico (build.rs puts them in the .exe files).

    python tools/make_icons.py [--preview OUT.png]

  ATSHaptics.ico   the window: a bright blue tile with a vibrating controller, the one to open
  ats_haptics.ico  the command line: a dark console window with a >_ prompt
  replay.ico       the debug-log replay: the same console window with an orange replay arrow

The colours are the window's own (ui/widgets.slint Theme): grip blue, trigger orange, card dark.
Every size is drawn on its own (8x supersampled, box-filtered down) rather than shrunk from the
256 px one, and the small sizes leave detail out so they stay sharp. Needs Pillow only.
"""

import argparse
import io
import math
import struct
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "resources" / "icons"
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)

GRIP = (90, 162, 255)       # Theme.grip
TRIGGER = (255, 154, 77)    # Theme.trigger
CARD = (22, 26, 33)         # Theme.card
FIELD = (35, 40, 51)        # Theme.field
TEXT = (231, 233, 238)      # Theme.text
WHITE = (255, 255, 255)


class Canvas:
    """A size x size icon drawn at `ss` times the size, in units where the icon is 256 wide."""

    def __init__(self, size: int):
        self.size = size
        self.ss = 4 if size >= 128 else 8
        self.px = size * self.ss
        self.image = Image.new("RGBA", (self.px, self.px), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)

    def u(self, v: float) -> float:
        """Units to supersampled pixels."""
        return v * self.px / 256

    def stroke(self, units: float, min_px: float) -> float:
        """A line width in units, but never thinner than `min_px` pixels of the final icon."""
        return max(self.u(units), min_px * self.ss)

    def pixel(self, n: float) -> float:
        """Final-icon pixels to supersampled pixels, for edges that should sit on the pixel grid."""
        return n * self.ss

    def tile(self) -> tuple[float, float, float, float]:
        """The rounded square every icon sits on, its edges snapped to whole pixels."""
        margin = {16: 1, 20: 1, 24: 1, 32: 2, 40: 2, 48: 3, 64: 4}.get(self.size, round(self.size / 16))
        return (self.pixel(margin), self.pixel(margin),
                self.pixel(self.size - margin) - 1, self.pixel(self.size - margin) - 1)

    def finish(self) -> Image.Image:
        return self.image.reduce(self.ss)


def gradient(width: int, height: int, top, bottom) -> Image.Image:
    column = Image.new("RGBA", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    return column.resize((width, height))


def capsule(draw: ImageDraw.ImageDraw, a, b, radius: float, fill) -> None:
    draw.line([a, b], fill=fill, width=round(2 * radius))
    for x, y in (a, b):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=fill)


def arc(draw: ImageDraw.ImageDraw, centre, radius: float, start: float, end: float, width: float, fill) -> None:
    """A clockwise arc (PIL angles: degrees clockwise from 3 o'clock) with round ends."""
    cx, cy = centre
    outer = radius + width / 2
    draw.arc([cx - outer, cy - outer, cx + outer, cy + outer], start, end, fill=fill, width=round(width))
    for angle in (start, end):
        x, y = cx + radius * math.cos(math.radians(angle)), cy + radius * math.sin(math.radians(angle))
        draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=fill)


# --- the window: ATSHaptics.exe ---------------------------------------------------------------

def window_icon(size: int) -> Image.Image:
    c = Canvas(size)
    x0, y0, x1, y1 = c.tile()
    radius = (x1 - x0) * 0.22
    mask = Image.new("L", (c.px, c.px), 0)
    ImageDraw.Draw(mask).rounded_rectangle([x0, y0, x1, y1], radius, fill=255)
    tile = gradient(c.px, c.px, (112, 182, 255), (38, 104, 224))
    c.image.paste(tile, (0, 0), mask)
    # a darker rim so the tile holds its edge on a light background
    c.draw.rounded_rectangle([x0, y0, x1, y1], radius, outline=(28, 78, 178, 150), width=max(1, round(c.ss * 0.75)))

    small = size <= 20
    scale = 1.18 if small else 0.92  # without the waves the controller can fill the tile
    cx, cy = c.u(128), c.u(140 if small else 138)

    def p(x: float, y: float) -> tuple[float, float]:
        """Controller units (centred on 0,0) to pixels."""
        return cx + c.u(x * scale), cy + c.u(y * scale)

    # the controller: a wide body and two grips sloping down and out
    body = Image.new("L", (c.px, c.px), 0)
    d = ImageDraw.Draw(body)
    (bx0, by0), (bx1, by1) = p(-62, -38), p(62, 18)
    d.rounded_rectangle([bx0, by0, bx1, by1], c.u(30 * scale), fill=255)
    for side in (-1, 1):
        capsule(d, p(side * 40, 0), p(side * 50, 38), c.u(22 * scale), 255)
    shade = Image.new("RGBA", (c.px, c.px), (0, 0, 0, 0))
    ImageDraw.Draw(shade).bitmap((0, c.u(5)), body, fill=(10, 40, 110, 90))  # a soft drop below
    c.image.alpha_composite(shade)
    c.image.paste(Image.new("RGBA", (c.px, c.px), WHITE + (255,)), (0, 0), body)

    hole = (52, 120, 228, 255)
    if size >= 32:
        # d-pad on the left, four buttons on the right
        (dx, dy), arm, w = p(-34, -12), c.u(14 * scale), c.u(5.5 * scale)
        c.draw.rectangle([dx - arm, dy - w, dx + arm, dy + w], fill=hole)
        c.draw.rectangle([dx - w, dy - arm, dx + w, dy + arm], fill=hole)
        bx, by = p(34, -12)
        r, off = c.u(5.5 * scale), c.u(11 * scale)
        for ox, oy in ((0, -off), (0, off), (-off, 0), (off, 0)):
            c.draw.ellipse([bx + ox - r, by + oy - r, bx + ox + r, by + oy + r], fill=hole)
    else:
        # one dot a side is all a few pixels hold, and enough to read as a controller
        r = c.pixel(0.8 if size <= 20 else 0.95)
        for side in (-1, 1):
            x, y = p(side * 32, -12)
            c.draw.ellipse([x - r, y - r, x + r, y + r], fill=hole)

    if not small:
        # vibration: orange arcs on both sides, one ring from 24 px, two from 40
        rings = (86,) if size < 40 else (80, 99)
        width = c.stroke(11, 1.6)
        for ring in rings:
            for centre_angle in (0, 180):
                arc(c.draw, (cx, cy - c.u(8)), c.u(ring), centre_angle - 26, centre_angle + 26, width,
                    TRIGGER + (255,))
    return c.finish()


# --- the console programs: ats_haptics.exe and replay.exe ----------------------------------------

def console_frame(c: Canvas) -> tuple[float, float, float, float]:
    """A dark window with a caption strip; returns the content area."""
    x0, y0, x1, y1 = c.tile()
    radius = (x1 - x0) * 0.13
    rim = max(1, round(c.ss * (1.0 if c.size <= 24 else 0.75)))
    c.draw.rounded_rectangle([x0, y0, x1, y1], radius, fill=(70, 80, 99, 255))  # the rim
    c.draw.rounded_rectangle([x0 + rim, y0 + rim, x1 - rim, y1 - rim], radius - rim, fill=CARD + (255,))
    bar = c.pixel(max(2, round(c.size * 0.16)))
    strip = Image.new("L", (c.px, c.px), 0)
    ImageDraw.Draw(strip).rounded_rectangle([x0 + rim, y0 + rim, x1 - rim, y1 - rim], radius - rim, fill=255)
    ImageDraw.Draw(strip).rectangle([0, y0 + rim + bar, c.px, c.px], fill=0)
    c.image.paste(Image.new("RGBA", (c.px, c.px), FIELD + (255,)), (0, 0), strip)
    if c.size >= 40:
        r = c.u(6)
        for i in range(3):
            x, y = x0 + c.u(26 + 20 * i), y0 + rim + bar / 2
            c.draw.ellipse([x - r, y - r, x + r, y + r], fill=(95, 104, 122, 255))
    return x0 + rim, y0 + rim + bar, x1 - rim, y1 - rim


def command_line_icon(size: int) -> Image.Image:
    c = Canvas(size)
    x0, y0, x1, y1 = console_frame(c)
    middle = (y0 + y1) / 2
    width = c.stroke(22, 1.8)
    # the ">" chevron in grip blue, the "_" cursor in text white
    left, tip, half = x0 + c.u(38), x0 + c.u(92), c.u(44)
    c.draw.line([(left, middle - half), (tip, middle), (left, middle + half)], fill=GRIP + (255,),
                width=round(width), joint="curve")
    for x, y in ((left, middle - half), (tip, middle), (left, middle + half)):
        c.draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=GRIP + (255,))
    base = middle + half
    c.draw.rectangle([x0 + c.u(116), base - width / 2, x0 + c.u(184), base + width / 2], fill=TEXT + (255,))
    return c.finish()


def replay_icon(size: int) -> Image.Image:
    c = Canvas(size)
    x0, y0, x1, y1 = console_frame(c)
    centre = ((x0 + x1) / 2, (y0 + y1) / 2)
    radius = min(x1 - x0, y1 - y0) * (0.30 if size > 20 else 0.33)
    width = c.stroke(20, 1.7)
    colour = TRIGGER + (255,)
    # a circle open at the top left, its head at the top pointing left: play it again
    start, end = 292, 205
    arc(c.draw, centre, radius, start, end + 360, width, colour)
    theta = math.radians(start)
    px, py = centre[0] + radius * math.cos(theta), centre[1] + radius * math.sin(theta)
    tangent = (math.sin(theta), -math.cos(theta))  # anticlockwise on screen
    normal = (math.cos(theta), math.sin(theta))
    head, spread = width * 1.35, width * 1.25
    c.draw.polygon([
        (px + tangent[0] * head, py + tangent[1] * head),
        (px + normal[0] * spread - tangent[0] * width * 0.25, py + normal[1] * spread - tangent[1] * width * 0.25),
        (px - normal[0] * spread - tangent[0] * width * 0.25, py - normal[1] * spread - tangent[1] * width * 0.25),
    ], fill=colour)
    if size >= 32:
        # a play mark inside
        s = radius * 0.42
        cx, cy = centre[0] + s * 0.18, centre[1]
        c.draw.polygon([(cx - s * 0.6, cy - s * 0.75), (cx + s * 0.75, cy), (cx - s * 0.6, cy + s * 0.75)],
                       fill=TEXT + (255,))
    return c.finish()


# --- the .ico container ---------------------------------------------------------------------------

def dib(image: Image.Image) -> bytes:
    """An icon frame as a 32-bit DIB with its AND mask, which every Windows reads."""
    w, h = image.size
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rgba = image.tobytes()
    rows = []
    for y in range(h - 1, -1, -1):  # bottom-up
        row = rgba[y * w * 4:(y + 1) * w * 4]
        rows.append(b"".join(row[i + 2:i + 3] + row[i + 1:i + 2] + row[i:i + 1] + row[i + 3:i + 4]
                             for i in range(0, len(row), 4)))
    mask_stride = ((w + 31) // 32) * 4
    mask = []
    for y in range(h - 1, -1, -1):
        bits = bytearray(mask_stride)
        for x in range(w):
            if rgba[(y * w + x) * 4 + 3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask.append(bytes(bits))
    return header + b"".join(rows) + b"".join(mask)


def write_ico(path: Path, frames: list[Image.Image]) -> None:
    """Small frames as DIBs, the 256 px one as PNG (as Windows' own icons are)."""
    blobs = []
    for frame in frames:
        if frame.width >= 256:
            buffer = io.BytesIO()
            frame.save(buffer, "PNG", optimize=True)
            blobs.append(buffer.getvalue())
        else:
            blobs.append(dib(frame))
    out = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    offset = 6 + 16 * len(frames)
    for frame, blob in zip(frames, blobs):
        w = frame.width if frame.width < 256 else 0
        out += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for blob in blobs:
        out += blob
    path.write_bytes(bytes(out))


ICONS = {
    "ATSHaptics": window_icon,
    "ats_haptics": command_line_icon,
    "replay": replay_icon,
}


def preview(path: Path, drawn: dict[str, list[Image.Image]]) -> None:
    """Every frame at 1x and 4x on a light and a dark strip, to check by eye."""
    rows = []
    for background in ((243, 243, 243), (32, 32, 32)):
        for frames in drawn.values():
            row_w = sum(f.width * (1 if f.width >= 128 else 4) + 12 for f in frames) + sum(f.width + 12 for f in frames[:-1])
            row = Image.new("RGBA", (row_w + 12, 256 + 24), background + (255,))
            x = 12
            for f in frames[:-1]:
                row.alpha_composite(f, (x, 12))
                x += f.width + 12
            for f in frames:
                big = f if f.width >= 128 else f.resize((f.width * 4, f.height * 4), Image.NEAREST)
                row.alpha_composite(big, (x, 12))
                x += big.width + 12
            rows.append(row)
    sheet = Image.new("RGBA", (max(r.width for r in rows), sum(r.height for r in rows)), (128, 128, 128, 255))
    y = 0
    for row in rows:
        sheet.alpha_composite(row, (0, y))
        y += row.height
    sheet.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preview", type=Path, help="also write a PNG sheet of every frame")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    drawn = {}
    for name, draw in ICONS.items():
        frames = [draw(size) for size in SIZES]
        write_ico(OUT / f"{name}.ico", frames)
        drawn[name] = frames
        print(f"{OUT / name}.ico")
    if args.preview:
        preview(args.preview, drawn)


if __name__ == "__main__":
    main()
