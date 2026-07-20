"""Generate the integration brand icon (an original mesh-of-lights motif).

Home Assistant (>= 2026.3) loads a custom integration's icon from the
``custom_components/<domain>/brand/`` directory: ``icon.png`` (256x256) and
``icon@2x.png`` (512x512). We deliberately do NOT use the Bluetooth wordmark or
figure mark (a trademark); instead the icon is an original motif — a central
glowing light node linked to satellite nodes — that reads as "mesh lighting".

Run with a Python that has Pillow::

    python scripts/make_icon.py

Outputs are written next to the integration, under ``brand/``.
"""

from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFilter

# Supersample everything, then downscale with LANCZOS for crisp anti-aliasing.
SS = 4
OUT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "bluetooth_mesh", "brand",
)

# Palette: a deep indigo→violet field (tech / mesh) with warm light nodes.
BG_TOP = (37, 33, 92)      # deep indigo
BG_BOTTOM = (86, 46, 138)  # violet
NODE = (255, 240, 214)     # warm white (light)
NODE_DIM = (214, 197, 255) # cool lilac for satellites
EDGE = (255, 255, 255)     # links (drawn semi-transparent)


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _gradient(size: int) -> Image.Image:
    grad = Image.new("RGB", (size, size), BG_TOP)
    px = grad.load()
    for y in range(size):
        t = y / (size - 1)
        r = round(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = round(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = round(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        for x in range(size):
            px[x, y] = (r, g, b)
    return grad


def _glow(size: int, cx: float, cy: float, radius: float, color) -> Image.Image:
    """A soft radial glow as its own RGBA layer (blurred filled circle)."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        fill=color + (255,),
    )
    return layer.filter(ImageFilter.GaussianBlur(radius * 0.55))


def build(size_out: int) -> Image.Image:
    S = size_out * SS
    # Rounded-rect gradient background.
    bg = _gradient(S)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(bg, (0, 0), _rounded_mask(S, int(S * 0.22)))

    cx = cy = S / 2
    ring = S * 0.30          # radius of the satellite ring
    node_r = S * 0.052       # satellite node radius
    center_r = S * 0.082     # central node radius

    # Satellite positions (5 nodes, evenly spaced, offset so one sits up top).
    n = 5
    sats = [
        (
            cx + ring * math.cos(-math.pi / 2 + i * 2 * math.pi / n),
            cy + ring * math.sin(-math.pi / 2 + i * 2 * math.pi / n),
        )
        for i in range(n)
    ]

    # Edges: centre→satellite and around the ring (mesh topology).
    edges = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ed = ImageDraw.Draw(edges)
    lw = max(1, int(S * 0.011))
    for sx, sy in sats:
        ed.line([cx, cy, sx, sy], fill=EDGE + (90,), width=lw)
    for i in range(n):
        ax, ay = sats[i]
        bx, by = sats[(i + 1) % n]
        ed.line([ax, ay, bx, by], fill=EDGE + (60,), width=lw)
    img = Image.alpha_composite(img, edges)

    # Central glow, then nodes.
    img = Image.alpha_composite(img, _glow(S, cx, cy, center_r * 2.6, NODE))
    d = ImageDraw.Draw(img)
    for sx, sy in sats:
        d.ellipse(
            [sx - node_r, sy - node_r, sx + node_r, sy + node_r],
            fill=NODE_DIM + (255,),
        )
    d.ellipse(
        [cx - center_r, cy - center_r, cx + center_r, cy + center_r],
        fill=NODE + (255,),
    )

    return img.resize((size_out, size_out), Image.LANCZOS)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    build(256).save(os.path.join(OUT, "icon.png"))
    build(512).save(os.path.join(OUT, "icon@2x.png"))
    print("wrote icon.png (256) and icon@2x.png (512) to", OUT)


if __name__ == "__main__":
    main()
