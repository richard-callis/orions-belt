"""
Generate the Orion's Belt .ico file for the desktop shortcut and tray icon.
Run automatically by setup.bat. Requires Pillow (already in requirements).
"""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "app" / "static" / "img" / "icon.ico"
OUT.parent.mkdir(parents=True, exist_ok=True)

ACCENT = (0, 167, 225)      # #00A7E1
BG     = (15, 15, 15)       # #0f0f0f


def draw_belt(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded square background
    pad = size // 8
    d.rounded_rectangle([pad, pad, size - pad, size - pad],
                        radius=size // 5, fill=BG)

    # Three dots — Orion's Belt stars
    dot_r = max(size // 10, 2)
    cx, cy = size // 2, size // 2
    gap = size // 5
    for x in [cx - gap, cx, cx + gap]:
        d.ellipse([x - dot_r, cy - dot_r, x + dot_r, cy + dot_r], fill=ACCENT)

    return img


sizes = [16, 24, 32, 48, 64, 128, 256]
frames = [draw_belt(s) for s in sizes]

frames[0].save(OUT, format="ICO", sizes=[(s, s) for s in sizes],
               append_images=frames[1:])

print(f"  Icon saved: {OUT}")
