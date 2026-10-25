#!/usr/bin/env python3
"""RETIRED — do not use this script for new cards.

Since the roster moved to delivered art, the image in ``assets/characters/``
**is** the card: the artist bakes the frame, the name plate and the crown into
the picture, and the game only resolves the path
(``services/character_art.artwork_path``). Running this script on a delivered
card re-crops it and draws a second frame on top of the first one. Kept only
for the four legacy cards it originally produced; no runtime code imports it
and ``tests/test_character_art.py`` fails if anything in ``services`` /
``handlers`` / ``keyboards`` / ``games`` starts using it again.

Compose character art into the canonical golden card frame.

ONE frame for ALL character cards (now and forever):

    python3 scripts/frame_card.py <art.jpg> <out.jpg>

or frame every file in assets/characters_raw/ into assets/characters/:

    python3 scripts/frame_card.py --all

The frame lives at assets/frames/card_frame_gold.jpg. The opening of the
frame is discovered from the frame asset itself (the matte region
connected to the center), the art is cover-fitted into that opening with
a minimal center crop (never stretched) and pasted through the exact
opening mask with a feathered seam. The golden border stays fully
visible on all four sides: ONE frame, no double frame, no empty margins.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
FRAME_PATH = ROOT / "assets" / "frames" / "card_frame_gold.jpg"
RAW_DIR = ROOT / "assets" / "characters_raw"
OUT_DIR = ROOT / "assets" / "characters"

MATTE_TOLERANCE = 40  # sum-of-|ΔRGB| accepted as "frame's matte paper"
SEAM_FEATHER = 1.0  # blur radius of the paste seam (px, inside the opening)


def opening_mask(frame: Image.Image) -> Image.Image:
    """Exact opening shape of the frame as an L-mode mask.

    The opening is the region of the frame's matte paper that is
    connected to the frame's center — this follows the true opening
    geometry (rounded/decorative corners) instead of a guessed rectangle.
    """
    matte = frame.convert("RGB").getpixel((frame.width // 2, frame.height // 2))
    solid = Image.new("RGB", frame.size, matte)
    r, g, b = ImageChops.difference(frame.convert("RGB"), solid).split()
    dist = ImageChops.add(ImageChops.add(r, g), b)  # saturating sum |ΔRGB|
    binary = dist.point(lambda v: 255 if v < MATTE_TOLERANCE else 0)
    binary = binary.convert("L")
    ImageDraw.floodfill(binary, (frame.width // 2, frame.height // 2), 128, thresh=0)
    return binary.point(lambda v: 255 if v == 128 else 0)


def cover_fit(art: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale `art` to COVER `size` and center-crop the overflow.

    Minimal crop, aspect preserved, the art never leaves gaps.
    """
    tw, th = size
    scale = max(tw / art.width, th / art.height)
    scaled = art.resize(
        (max(tw, round(art.width * scale)), max(th, round(art.height * scale))),
        Image.LANCZOS,
    )
    x = (scaled.width - tw) // 2
    y = (scaled.height - th) // 2
    return scaled.crop((x, y, x + tw, y + th))


def frame_card(art_path: Path, out_path: Path) -> None:
    frame = Image.open(FRAME_PATH).convert("RGB")
    mask = opening_mask(frame)
    ox0, oy0, ox1, oy1 = mask.getbbox()
    art = Image.open(art_path).convert("RGB")

    fitted = cover_fit(art, (ox1 - ox0, oy1 - oy0))
    seam = mask.crop((ox0, oy0, ox1, oy1)).filter(ImageFilter.GaussianBlur(SEAM_FEATHER))
    frame.paste(fitted, (ox0, oy0), seam)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    frame.save(out_path, quality=92)
    print(f"framed: {out_path}")


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--all":
        for art in sorted(RAW_DIR.glob("*.jpg")):
            frame_card(art, OUT_DIR / art.name)
        return
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    frame_card(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
