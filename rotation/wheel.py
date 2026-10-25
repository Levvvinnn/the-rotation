"""Draw a spinning wheel GIF where slice size = how overdue each person is."""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLORS = ["#FF6B6B", "#4ECDC4", "#FFD93D", "#6C5CE7", "#FF9F43", "#1DD1A1", "#54A0FF", "#F368E0"]
POINTER_ANGLE = 270.0  # top of the wheel, in Pillow's angles (0 = 3 o'clock, clockwise)

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font(size: int):
    for p in _FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size=size)


def slices(weights: list[float]) -> list[tuple[float, float]]:
    """Start and end angles (Pillow degrees, clockwise from 3 o'clock) for each weight."""
    total = sum(weights)
    out, a = [], 0.0
    for w in weights:
        span = 360.0 * w / total
        out.append((a, a + span))
        a += span
    return out


def final_rotation(spans: list[tuple[float, float]], winner: int, rng: random.Random, turns: int = 5) -> float:
    """Counterclockwise rotation (degrees) that leaves the winner's slice under the pointer."""
    a, b = spans[winner]
    mid = (a + b) / 2
    jitter = (b - a) * 0.35 * rng.uniform(-1, 1)  # don't always land dead center
    return 360.0 * turns + ((mid + jitter - POINTER_ANGLE) % 360.0)


def _wheel_image(names: list[str], spans, size: int) -> Image.Image:
    scale = 2  # draw big, shrink later for smooth edges
    S = size * scale
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    box = (6 * scale, 6 * scale, S - 6 * scale, S - 6 * scale)
    font = _font(int(S * 0.055))
    for i, ((a, b), name) in enumerate(zip(spans, names)):
        d.pieslice(box, a, b, fill=COLORS[i % len(COLORS)], outline="white", width=3 * scale)
    c, r = S / 2, (S - 12 * scale) / 2
    for (a, b), name in zip(spans, names):
        if b - a < 14:
            continue  # slice too thin for a label
        mid = math.radians((a + b) / 2)
        x, y = c + r * 0.62 * math.cos(mid), c + r * 0.62 * math.sin(mid)
        label = name if len(name) <= 10 else name[:9] + "…"
        d.text((x, y), label, font=font, fill="white", anchor="mm", stroke_width=3 * scale, stroke_fill="#222222")
    return img.resize((size, size), Image.LANCZOS)


def _overlay(size: int) -> Image.Image:
    over = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    c = size / 2
    d.ellipse((c - size * 0.07, c - size * 0.07, c + size * 0.07, c + size * 0.07),
              fill="#1E1F22", outline="#F2F3F5", width=4)
    d.polygon([(c - size * 0.05, 0), (c + size * 0.05, 0), (c, size * 0.11)], fill="#F2F3F5", outline="#111214", width=2)
    return over


def render(names: list[str], weights: list[float], winner: int, out_path: str | Path,
           size: int = 420, frames: int = 48, seed: int | None = None) -> Path:
    """Write a spin GIF that ends on `winner`. Returns the path."""
    if len(names) != len(weights) or not names or not 0 <= winner < len(names):
        raise ValueError("Choose a winner from matching names and weights.")
    if any(not math.isfinite(w) or w <= 0 for w in weights) or frames < 2 or size < 100:
        raise ValueError("Use positive finite weights, at least two frames, and a size of 100 or more.")
    rng = random.Random(seed)
    spans = slices(weights)
    total_rot = final_rotation(spans, winner, rng)
    wheel = _wheel_image(names, spans, size)
    over = _overlay(size)
    bg = (26, 26, 30, 255)  # #1A1A1E, sampled from the Discord reference

    pics, durations = [], []
    for i in range(frames):
        t = i / (frames - 1)
        ease = 1 - (1 - t) ** 3  # fast start, slow finish
        frame = Image.new("RGBA", (size, size), bg)
        frame.alpha_composite(wheel.rotate(total_rot * ease, resample=Image.BICUBIC))
        frame.alpha_composite(over)
        pics.append(frame.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=128))
        durations.append(40 if t < 0.7 else 60 if t < 0.9 else 90)
    durations[-1] = 60000  # rest on the result instead of looping straight back into a spin

    out_path = Path(out_path)
    pics[0].save(out_path, save_all=True, append_images=pics[1:], duration=durations, optimize=True, disposal=1)
    return out_path


def winner_color(winner: int) -> str:
    return COLORS[winner % len(COLORS)]
