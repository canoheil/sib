"""Actual repository font and 16:9 composition geometry."""
from pathlib import Path
from PIL import ImageFont

ROOT = Path(__file__).resolve().parents[3]
FONT_CANDIDATES = (
    ROOT / ".agents/skills/persian-writing/assets/fonts/Vazirmatn-Bold.ttf",
    ROOT / ".agents/skills/persian-writing/assets/fonts/Vazirmatn-Regular.ttf",
)
FONT_PATH = next((path for path in FONT_CANDIDATES if path.is_file()), None)
if FONT_PATH is None:
    raise FileNotFoundError("Persian font missing; searched: " + ", ".join(map(str, FONT_CANDIDATES)))

SIZE = (1280, 720)
PALETTES = {
    "soldier": ((9, 11, 18), (66, 41, 59)),
    "shadow": ((6, 15, 31), (40, 60, 99)),
    "commander": ((15, 17, 33), (88, 48, 76)),
    "brute": ((19, 20, 28), (89, 47, 36)),
    "king": ((8, 10, 27), (87, 58, 113)),
}
SILHOUETTE_BOX = (95, 145, 510, 625)
CARD_BOX = (500, 88, 884, 590)
BAND = (0, 565, 1280, 720)


def font(size: int):
    return ImageFont.truetype(str(FONT_PATH), size)


def label(draw, xy, text: str, size: int, *, fill="#f3eee1", anchor="ra"):
    """Shape RTL text with libraqm (required for legible Persian)."""
    from PIL import features
    if not features.check("raqm"):
        raise RuntimeError("Pillow must have libraqm to shape Persian invasion text")
    draw.text(xy, str(text), font=font(size), fill=fill, anchor=anchor, direction="rtl", stroke_width=1, stroke_fill="#090b12")
