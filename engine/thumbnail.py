"""Original card thumbnails, drawn by the engine (no publisher photos, so no image-rights exposure).

Each story gets a PNG in two shapes: wide 1200x630 (link previews, feed cards) and square 1080x1080
(Instagram, grid cards). The look comes from the story's top category (palette + motif); the motif's
layout is seeded by the story id, so no two stories look alike. Text is the card title (from the digest
when there is one) or the headline.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import random
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import settings as S

VERSION = 1
SIZES = {"wide": (1200, 630), "square": (1080, 1080)}
FONTS = S.ROOT / "assets" / "fonts"

# palette: background top-left, background bottom-right, accent, pill text
PALETTES = {
    "world":         ("#0B1B33", "#123A63", "#5AB0FF", "#06223F"),
    "politics":      ("#1E0E33", "#3A1A5C", "#C79BFF", "#24103D"),
    "business":      ("#062A22", "#0D4A3B", "#3EE0A5", "#05291F"),
    "tech":          ("#0D1220", "#1C2747", "#6EA8FF", "#0B1630"),
    "science":       ("#042A33", "#0A4B5A", "#38D9F0", "#032B33"),
    "health":        ("#33101A", "#5A1F2E", "#FF8FA3", "#3A1220"),
    "environment":   ("#0C2A12", "#1C4D22", "#9BE88E", "#0E2E13"),
    "sports":        ("#331705", "#5C2C0B", "#FFA552", "#3A1A06"),
    "entertainment": ("#2E0A2A", "#56164E", "#FF8AD8", "#330B2F"),
    "justice":       ("#1A1A1A", "#33302A", "#F5C451", "#26221A"),
    "education":     ("#16163D", "#2A2A6B", "#A9B4FF", "#16163D"),
}
GROUP = {
    "World": "world", "Politics": "politics", "Policy": "politics", "Leadership": "politics",
    "Business": "business", "Economy": "business", "Markets": "business", "Funding": "business",
    "Fintech": "business", "Ecommerce": "business", "Startups": "tech", "Technology": "tech", "AI": "tech",
    "Big Tech": "tech", "Cybersecurity": "tech", "Deeptech": "tech", "Social Media": "tech", "Mobility": "tech",
    "Science": "science", "Health": "health", "Environment": "environment", "Sports": "sports",
    "Entertainment": "entertainment", "Culture": "entertainment", "Lifestyle": "entertainment",
    "Travel": "entertainment", "Crime & Justice": "justice", "Education": "education",
}


def _hex(c: str, a: int = 255) -> tuple:
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), a)


@lru_cache(maxsize=16)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Inter-{weight}.otf"), size)


def _gradient(w: int, h: int, c1: str, c2: str) -> Image.Image:
    """Diagonal two-colour gradient, built from Pillow's 256-step ramp (fast)."""
    ramp = Image.linear_gradient("L").rotate(-35, expand=True, resample=Image.BICUBIC)
    side = ramp.size[0]
    ramp = ramp.crop((side * 0.32, side * 0.32, side * 0.68, side * 0.68)).resize((w, h), Image.BILINEAR)
    return Image.composite(Image.new("RGBA", (w, h), _hex(c2)), Image.new("RGBA", (w, h), _hex(c1)), ramp)


# ------------------------------------------------------------------ motifs (draw on a transparent layer)
def _motif(group: str, d: ImageDraw.ImageDraw, box: tuple, rnd: random.Random, accent: str) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx, cy = x0 + w * rnd.uniform(0.45, 0.6), y0 + h * rnd.uniform(0.4, 0.6)
    A = lambda a: _hex(accent, a)  # noqa: E731
    if group == "world":                                         # globe: rings + meridians
        r = min(w, h) * rnd.uniform(0.42, 0.5)
        for i in range(6):
            rr = r * (1 - i * 0.14)
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=A(70 - i * 8), width=3)
        for k in range(5):
            ew = r * abs(math.cos(k * math.pi / 5 + rnd.uniform(0, 0.4)))
            d.ellipse([cx - ew, cy - r, cx + ew, cy + r], outline=A(55), width=2)
        for _ in range(9):
            a, rr = rnd.uniform(0, 2 * math.pi), r * rnd.uniform(0.2, 0.95)
            px, py = cx + rr * math.cos(a), cy + rr * math.sin(a)
            d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=A(200))
    elif group == "politics":                                    # columns of a house
        n = rnd.randint(5, 7)
        cw = w * 0.7 / n
        base_y = y1 - h * 0.12
        for i in range(n):
            hh = h * rnd.uniform(0.45, 0.75)
            x = x0 + w * 0.15 + i * cw
            d.rounded_rectangle([x, base_y - hh, x + cw * 0.55, base_y], radius=8, fill=A(40 + i * 12))
        d.polygon([(x0 + w * 0.1, y0 + h * 0.22), (x0 + w * 0.5, y0 + h * 0.04), (x0 + w * 0.9, y0 + h * 0.22)], fill=A(60))
    elif group == "business":                                    # rising bars + trend line
        n = rnd.randint(7, 10)
        bw = w * 0.8 / n
        vals = [rnd.uniform(0.15, 0.35)]
        for _ in range(n - 1):
            vals.append(min(0.95, max(0.1, vals[-1] + rnd.uniform(-0.08, 0.16))))
        base_y = y1 - h * 0.1
        pts = []
        for i, v in enumerate(vals):
            x = x0 + w * 0.1 + i * bw
            d.rounded_rectangle([x, base_y - v * h * 0.8, x + bw * 0.6, base_y], radius=6, fill=A(45 + i * 10))
            pts.append((x + bw * 0.3, base_y - v * h * 0.8 - 18))
        d.line(pts, fill=A(230), width=6, joint="curve")
        d.ellipse([pts[-1][0] - 10, pts[-1][1] - 10, pts[-1][0] + 10, pts[-1][1] + 10], fill=A(255))
    elif group == "tech":                                        # node network
        nodes = [(x0 + w * rnd.uniform(0.08, 0.95), y0 + h * rnd.uniform(0.08, 0.92)) for _ in range(16)]
        for i, a in enumerate(nodes):
            near = sorted(nodes, key=lambda b: (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)[1:3]
            for b in near:
                d.line([a, b], fill=A(70), width=2)
        for i, (px, py) in enumerate(nodes):
            r = rnd.choice([5, 7, 10, 14])
            d.ellipse([px - r, py - r, px + r, py + r], fill=A(120 + (i % 4) * 30))
        for gx in range(int(x0), int(x1), 36):
            for gy in range(int(y0), int(y1), 36):
                d.point((gx, gy), fill=A(60))
    elif group == "science":                                     # orbits
        for i in range(4):
            rx, ry = w * (0.18 + i * 0.1), h * (0.08 + i * 0.07)
            tilt = rnd.uniform(-0.5, 0.5)
            pts = [(cx + rx * math.cos(t) * math.cos(tilt) - ry * math.sin(t) * math.sin(tilt),
                    cy + rx * math.cos(t) * math.sin(tilt) + ry * math.sin(t) * math.cos(tilt))
                   for t in [k * 2 * math.pi / 90 for k in range(91)]]
            d.line(pts, fill=A(90 - i * 12), width=3)
            px, py = pts[rnd.randint(0, 89)]
            d.ellipse([px - 9, py - 9, px + 9, py + 9], fill=A(230))
        d.ellipse([cx - 26, cy - 26, cx + 26, cy + 26], fill=A(200))
    elif group == "health":                                      # pulse line + crosses
        base_y = cy
        pts, x = [], x0
        while x < x1:
            if rnd.random() < 0.12:
                pts += [(x, base_y), (x + 14, base_y - h * 0.3), (x + 28, base_y + h * 0.18), (x + 42, base_y)]
                x += 42
            else:
                pts.append((x, base_y))
                x += 24
        d.line(pts, fill=A(220), width=6, joint="curve")
        for _ in range(6):
            px, py, s = x0 + w * rnd.uniform(0.1, 0.9), y0 + h * rnd.uniform(0.1, 0.9), rnd.choice([14, 20, 28])
            d.rectangle([px - s, py - s / 3, px + s, py + s / 3], fill=A(70))
            d.rectangle([px - s / 3, py - s, px + s / 3, py + s], fill=A(70))
    elif group == "environment":                                 # waves
        for i in range(7):
            amp, ph, yy = h * rnd.uniform(0.03, 0.07), rnd.uniform(0, 6), y0 + h * (0.2 + i * 0.1)
            pts = [(x, yy + amp * math.sin(x / (w * 0.12) + ph)) for x in range(int(x0), int(x1) + 1, 8)]
            d.line(pts, fill=A(60 + i * 20), width=5)
    elif group == "sports":                                      # track lanes + ball
        lane_y = y0 + h * 0.95
        for i in range(6):
            r = min(w, h) * (0.32 + i * 0.1)
            d.arc([cx - r * 1.3, lane_y - r, cx + r * 1.3, lane_y + r], start=195, end=345, fill=A(70 + i * 15), width=6)
        bx, by = cx + w * rnd.uniform(-0.15, 0.15), y0 + h * rnd.uniform(0.55, 0.7)
        d.ellipse([bx - 34, by - 34, bx + 34, by + 34], fill=A(235))
    elif group == "entertainment":                               # spotlight rays
        ox = x0 + w * rnd.uniform(0.3, 0.7)
        for i in range(9):
            a = math.radians(rnd.uniform(55, 125))
            spread = math.radians(rnd.uniform(3, 7))
            L = h * 1.3
            d.polygon([(ox, y0 - 10), (ox + L * math.cos(a - spread), y0 + L * math.sin(a - spread)),
                       (ox + L * math.cos(a + spread), y0 + L * math.sin(a + spread))], fill=A(28 + i * 6))
        for _ in range(14):
            px, py = x0 + w * rnd.uniform(0, 1), y0 + h * rnd.uniform(0.5, 1)
            d.ellipse([px - 4, py - 4, px + 4, py + 4], fill=A(200))
    elif group == "justice":                                     # diagonal stripes
        for i in range(-10, 22):
            x = x0 + i * 42 + rnd.uniform(-3, 3)
            d.line([(x, y1), (x + h * 0.8, y0)], fill=A(35 if i % 3 else 90), width=10)
    else:                                                        # education and default: stacked blocks
        for i in range(6):
            bw, bh = w * rnd.uniform(0.4, 0.7), h * 0.085
            x = x0 + w * 0.2 + rnd.uniform(-20, 20)
            y = y1 - h * 0.12 - (i + 1) * bh * 1.15
            d.rounded_rectangle([x, y, x + bw, y + bh], radius=10, fill=A(60 + i * 22))


# ------------------------------------------------------------------ text
def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if font.getlength(test) <= width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit(text: str, width: int, max_lines: int, sizes: range) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    for size in sizes:
        f = _font("ExtraBold", size)
        lines = _wrap(text, f, width)
        if len(lines) <= max_lines:
            return f, lines
    f = _font("ExtraBold", sizes[-1])
    lines = _wrap(text, f, width)[:max_lines]
    while lines and f.getlength(lines[-1] + "…") > width:
        lines[-1] = lines[-1].rsplit(" ", 1)[0]
    if lines:
        lines[-1] = lines[-1].rstrip(",;:—-") + "…"
    return f, lines


def _pill(d: ImageDraw.ImageDraw, xy: tuple, text: str, bg: str, fg: str, size: int = 26) -> int:
    f = _font("Bold", size)
    x, y = xy
    tw = f.getlength(text)
    pad_x, h = size * 0.7, size * 1.7
    d.rounded_rectangle([x, y, x + tw + pad_x * 2, y + h], radius=h / 2, fill=_hex(bg))
    d.text((x + pad_x, y + h / 2), text, font=f, fill=_hex(fg), anchor="lm")
    return int(x + tw + pad_x * 2)


# ------------------------------------------------------------------ render
_cache: dict[str, bytes] = {}
CACHE_MAX = 400


def render_cached(story: dict, size: str = "wide") -> bytes:
    key = etag(story, size)
    png = _cache.get(key)
    if png is None:
        png = render(story, size)
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = png
    return png


def render(story: dict, size: str = "wide") -> bytes:
    if size not in SIZES:
        raise ValueError(f"size must be one of {sorted(SIZES)}")
    W, H = SIZES[size]
    tags = story.get("tags") or {}
    cats = tags.get("category") or ["World"]
    group = GROUP.get(cats[0], "world")
    bg1, bg2, accent, pill_fg = PALETTES[group]
    seed = int(hashlib.sha1(f"{story['id']}:{VERSION}".encode()).hexdigest()[:12], 16)
    rnd = random.Random(seed)

    img = _gradient(W, H, bg1, bg2)
    art = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    box = (W * 0.6, 24, W - 24, H - 110) if size == "wide" else (40, 40, W - 40, H * 0.46)
    _motif(group, ImageDraw.Draw(art), box, rnd, accent)
    art = art.filter(ImageFilter.GaussianBlur(0.6))
    # fade the art toward the text so the headline always reads cleanly
    mask = Image.new("L", (W, H), 0)
    mp = ImageDraw.Draw(mask)
    if size == "wide":
        for x in range(W):
            mp.line([(x, 0), (x, H)], fill=int(255 * max(0.0, min(1.0, (x - W * 0.57) / (W * 0.12)))))
    else:
        for y in range(H):
            mp.line([(0, y), (W, y)], fill=int(255 * max(0.0, min(1.0, (H * 0.6 - y) / (H * 0.3)))))
    img.paste(art, (0, 0), Image.composite(art.getchannel("A"), Image.new("L", (W, H), 0), mask))
    d = ImageDraw.Draw(img)

    title = (story.get("card_title") or story.get("headline") or "").strip()
    sources = story.get("sources") or []
    n = story.get("source_count") or len(sources) or 1
    region = (tags.get("region") or [None])[0]
    meta = " · ".join(x for x in [region, f"{n} outlets" if n > 1 else None] if x)

    if size == "wide":
        m = 64
        x_after = _pill(d, (m, m), cats[0].upper(), accent, pill_fg, 24)
        if n >= S.settings.trending_source_count:
            _pill(d, (x_after + 14, m), "TRENDING", "#FFFFFF", bg1, 24)
        f, lines = _fit(title, int(W * 0.56) - m, 4, range(62, 35, -2))
        lh = int(f.size * 1.12)
        top = m + 70 + max(0, (H - m - 70 - 110 - lh * len(lines)) // 2)
        for i, line in enumerate(lines):
            d.text((m, top + i * lh), line, font=f, fill=(255, 255, 255, 255))
        foot_y = H - m - 8
    else:
        m = 72
        top_text = int(H * 0.52)
        x_after = _pill(d, (m, top_text - 70), cats[0].upper(), accent, pill_fg, 28)
        if n >= S.settings.trending_source_count:
            _pill(d, (x_after + 14, top_text - 70), "TRENDING", "#FFFFFF", bg1, 28)
        f, lines = _fit(title, W - 2 * m, 5, range(76, 43, -2))
        lh = int(f.size * 1.12)
        for i, line in enumerate(lines):
            d.text((m, top_text + 10 + i * lh), line, font=f, fill=(255, 255, 255, 255))
        foot_y = H - m + 8

    src_line = " · ".join(sources[:3]) + (f" +{len(sources) - 3}" if len(sources) > 3 else "")
    fm = _font("SemiBold", 24 if size == "wide" else 28)
    d.text((m, foot_y), src_line, font=fm, fill=(255, 255, 255, 190), anchor="ls")
    if meta:
        d.text((m, foot_y - fm.size - 12), meta, font=_font("Regular", fm.size - 2), fill=_hex(accent, 230), anchor="ls")
    brand = os.getenv("BRAND_NAME", "")
    if brand:
        d.text((W - m, foot_y), brand, font=_font("Bold", fm.size), fill=(255, 255, 255, 220), anchor="rs")

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def etag(story: dict, size: str) -> str:
    key = f"{story['id']}|{size}|{VERSION}|{story.get('card_title') or story.get('headline')}|" \
          f"{story.get('source_count')}|{(story.get('tags') or {}).get('category')}|{os.getenv('BRAND_NAME', '')}"
    return '"' + hashlib.sha1(key.encode()).hexdigest()[:20] + '"'
