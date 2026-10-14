"""Five distinct offline gradient backdrops and stylized corrupted apple forms."""
import math
from PIL import Image, ImageDraw, ImageFilter
from .templates import PALETTES, SIZE, SILHOUETTE_BOX


def background(enemy_type: str) -> Image.Image:
    if enemy_type not in PALETTES:
        raise ValueError(f"Unknown enemy type: {enemy_type}")
    dark, accent = PALETTES[enemy_type]
    img = Image.new("RGB", SIZE)
    pixels = img.load()
    w, h = SIZE
    for y in range(h):
        for x in range(w):
            t = min(1.0, y / h * .68 + max(0, 1 - abs(x - 380) / 820) * .32)
            vignette = .56 + .44 * max(0, 1 - math.hypot((x-w/2)/(w/2), (y-h/2)/(h/2)) / 1.4)
            pixels[x, y] = tuple(int((a * (1-t) + b * t) * vignette) for a, b in zip(dark, accent))
    d = ImageDraw.Draw(img, "RGBA")
    d.ellipse((40, -350, 690, 300), fill=(193, 207, 242, 10), outline=(190, 209, 238, 55), width=3)
    for i in range(12):
        x = (i * 277 + 133) % w
        d.line((x, 535, x + 130, 710), fill=(183, 196, 214, 11), width=2)
    return img


def soldier(): return background("soldier")
def shadow(): return background("shadow")
def commander(): return background("commander")
def brute(): return background("brute")
def king(): return background("king")


def silhouette(enemy_type: str, box=SILHOUETTE_BOX) -> Image.Image:
    """Apple-shaped threat, distinct scale/crown/armor per type; no external art."""
    if enemy_type not in PALETTES:
        raise ValueError(f"Unknown enemy type: {enemy_type}")
    x0, y0, x1, y1 = box
    w, h = x1-x0, y1-y0
    layer = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(layer)
    rim = (*PALETTES[enemy_type][1], 225)
    mass = (13, 15, 25, 250)
    pad = 34 if enemy_type == "brute" else 66 if enemy_type == "shadow" else 48
    d.ellipse((pad, 108, w-pad, h-22), fill=mass, outline=rim, width=7)
    d.ellipse((w//2-17, 140, w//2+10, 258), fill=(68, 20, 39, 180))
    d.polygon([(w//2-9, 135), (w//2+6, 35), (w//2+32, 36), (w//2+20, 145)], fill=(22, 24, 27, 255), outline=rim)
    d.ellipse((w//2+7, 50, w//2+128, 103), fill=(19, 35, 27, 255), outline=rim, width=3)
    if enemy_type in ("commander", "king"):
        d.polygon([(70, 160), (87, 48), (144, 107), (w//2, 32), (w-145, 108), (w-87, 48), (w-70, 160)], fill=(26, 23, 34, 255), outline=rim)
    if enemy_type == "brute":
        d.rounded_rectangle((8, 210, 96, 395), radius=24, fill=mass, outline=rim, width=5)
        d.rounded_rectangle((w-96, 210, w-8, 395), radius=24, fill=mass, outline=rim, width=5)
    if enemy_type == "shadow":
        layer = layer.filter(ImageFilter.GaussianBlur(4))
    d = ImageDraw.Draw(layer)
    d.polygon([(w//2-88, 268), (w//2-33, 253), (w//2-56, 269)], fill=(218, 94, 100, 255))
    d.polygon([(w//2+33, 253), (w//2+88, 268), (w//2+56, 269)], fill=(218, 94, 100, 255))
    img = Image.new("RGBA", SIZE)
    img.alpha_composite(layer, (x0, y0))
    return img
