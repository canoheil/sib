#!/usr/bin/env python3
"""Compose character art into the canonical golden card frame.

ONE frame for ALL character cards (now and forever):

    python3 scripts/frame_card.py <art.jpg> <out.jpg>

or frame every file in assets/characters_raw/ into assets/characters/:

    python3 scripts/frame_card.py --all

The frame lives at assets/frames/card_frame_gold.jpg. Art is center-cropped
(cover fit) into the frame opening; the ornate golden border stays visible
on all four sides.
"""

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FRAME_PATH = ROOT / "assets" / "frames" / "card_frame_gold.jpg"
RAW_DIR = ROOT / "assets" / "characters_raw"
OUT_DIR = ROOT / "assets" / "characters"

# Central region of the frame occupied by the artwork (fractions).
ART_W = 0.74
ART_H = 0.72


def frame_card(art_path: Path, out_path: Path) -> None:
    frame = Image.open(FRAME_PATH).convert("RGB")
    art = Image.open(art_path).convert("RGB")
    W, H = frame.size
    aw, ah = int(W * ART_W), int(H * ART_H)

    # Cover-fit: scale so the art fills the opening, then center-crop.
    scale = max(aw / art.width, ah / art.height)
    art = art.resize(
        (max(1, int(art.width * scale)), max(1, int(art.height * scale))),
        Image.LANCZOS,
    )
    x = (art.width - aw) // 2
    y = (art.height - ah) // 2
    art = art.crop((x, y, x + aw, y + ah))

    frame.paste(art, ((W - aw) // 2, (H - ah) // 2))
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
