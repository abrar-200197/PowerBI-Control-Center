"""
Build an original scroll-scrub dive frame pack for post-login landing.

Legal intent:
- Does NOT download or re-encode Mercury's Mux/streaming videos.
- Uses our existing local still as start art (already in-repo).
- Composites an original Power BI Control Center UI mock for deep frames.
- Ken Burns crops are our own generated imagery for scroll scrubbing.

Output: static/video/dive/frame_XXX.jpg + manifest.json
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "static" / "img" / "mercury-hero.jpg"
OUT = ROOT / "static" / "video" / "dive"
# 16:9 stage, 60 frames (~6s at 10fps scrub mapping — dense enough for scroll)
W, H = 1600, 900
N = 60
MAX_ZOOM = 5.0
FOCUS = (0.50, 0.57)  # laptop screen center in source still


def ease(t: float) -> float:
    # smootherstep
    return t * t * t * (t * (t * 6 - 15) + 10)


def cover_crop(im: Image.Image, zoom: float, focus=FOCUS) -> Image.Image:
    iw, ih = im.size
    view_aspect = W / H
    if (iw / ih) > view_aspect:
        base_h, base_w = ih, ih * view_aspect
    else:
        base_w, base_h = iw, iw / view_aspect
    sw, sh = base_w / zoom, base_h / zoom
    sx = max(0, min(iw - sw, focus[0] * iw - sw / 2))
    sy = max(0, min(ih - sh, focus[1] * ih - sh / 2))
    crop = im.crop((int(sx), int(sy), int(sx + sw), int(sy + sh)))
    return crop.resize((W, H), Image.Resampling.LANCZOS)


def draw_control_center_ui(size=(W, H)) -> Image.Image:
    """Original PBI Control Center chrome mock (not Mercury product UI)."""
    w, h = size
    img = Image.new("RGB", (w, h), "#EDF2F7")
    d = ImageDraw.Draw(img)
    # Sidebar
    sw = int(w * 0.18)
    d.rectangle([0, 0, sw, h], fill="#1A365D")
    d.rectangle([0, 0, sw, 56], fill="#153E75")
    d.text((16, 18), "POWER BI", fill="#E2E8F0")
    d.text((16, 34), "CONTROL CENTER", fill="#90CDF4")
    for i, label in enumerate(
        ["Home", "Report Catalog", "Semantic Models", "Report Lineage", "Impact Explorer"]
    ):
        y = 80 + i * 40
        if i == 0:
            d.rectangle([8, y - 8, sw - 8, y + 24], fill="#2B6CB0")
        d.text((20, y), label, fill="#E2E8F0")
    # Header
    d.rectangle([sw, 0, w, 64], fill="#FFFFFF")
    d.line([sw, 64, w, 64], fill="#E2E8F0", width=1)
    d.text((sw + 24, 14), "Home", fill="#1A202C")
    d.text((sw + 24, 36), "Workspace health at a glance", fill="#718096")
    # KPI cards
    cards = [
        ("WORKSPACES", "39", "#2B6CB0"),
        ("REPORTS", "1878", "#2F855A"),
        ("INACTIVE", "441", "#C05621"),
        ("ORPHANED", "10", "#6B46C1"),
    ]
    gap, cw = 16, (w - sw - 24 - 3 * 16) // 4
    y0 = 84
    for i, (lab, val, accent) in enumerate(cards):
        x = sw + 12 + i * (cw + gap)
        d.rounded_rectangle([x, y0, x + cw, y0 + 88], radius=10, fill="#FFFFFF", outline="#E2E8F0")
        d.ellipse([x + 14, y0 + 28, x + 42, y0 + 56], fill=accent)
        d.text((x + 52, y0 + 18), lab, fill="#718096")
        d.text((x + 52, y0 + 40), val, fill="#1A202C")
    # Table
    ty = y0 + 110
    d.rounded_rectangle([sw + 12, ty, w - 12, h - 16], radius=10, fill="#FFFFFF", outline="#E2E8F0")
    d.text((sw + 28, ty + 14), "Your workspaces", fill="#2D3748")
    rows = [
        "AGR Merchandizing & Visual",
        "BI - Marketing Specialists",
        "BI Development Supply Chain",
        "Enterprise SupplyChain",
        "Digital Marketing",
    ]
    for i, name in enumerate(rows):
        y = ty + 48 + i * 36
        d.line([sw + 20, y - 6, w - 20, y - 6], fill="#EDF2F7")
        d.text((sw + 28, y), name, fill="#2D3748")
        d.text((w - 120, y), "Open", fill="#2B6CB0")
    return img


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"Missing source still: {SRC}")
    OUT.mkdir(parents=True, exist_ok=True)
    base = Image.open(SRC).convert("RGB")
    # Mild unsharp once for cleaner crops
    base = base.filter(ImageFilter.UnsharpMask(radius=1.0, percent=110, threshold=2))
    ui = draw_control_center_ui()
    names = []
    for i in range(N):
        t = i / (N - 1)
        e = ease(t)
        zoom = 1.0 + e * (MAX_ZOOM - 1.0)
        frame = cover_crop(base, zoom)
        # Late dive: dissolve original Control Center UI into view (our app, not Mercury)
        if t >= 0.52:
            a = min(1.0, (t - 0.52) / 0.40)
            a = a * a * (3 - 2 * a)
            frame = Image.blend(frame, ui, a)
        name = f"frame_{i:03d}.jpg"
        path = OUT / name
        frame.save(path, "JPEG", quality=86, optimize=True, progressive=True)
        names.append(name)
        if i % 10 == 0:
            print(f"frame {i}/{N-1} zoom={zoom:.2f}")
    # Poster = first frame
    Image.open(OUT / names[0]).save(OUT / "poster.jpg", "JPEG", quality=88, optimize=True)
    manifest = {
        "version": 1,
        "frameCount": N,
        "width": W,
        "height": H,
        "durationHintSec": 6.0,
        "focus": list(FOCUS),
        "maxZoom": MAX_ZOOM,
        "frames": names,
        "poster": "poster.jpg",
        "licenseNote": (
            "Original Control Center UI composite + generated Ken Burns frame pack. "
            "Not a redistribution of Mercury Mux/stream video assets."
        ),
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    total = sum(p.stat().st_size for p in OUT.glob("*.jpg"))
    print(f"OK {N} frames -> {OUT} ({total/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
