"""Apple Clash dynamic combat composition using pre-generated AI backgrounds and Pillow."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError as exc:
    raise ImportError("Persian war imaging requires arabic-reshaper and python-bidi") from exc

from sib.core.logging import logger

ROOT = Path(__file__).resolve().parents[2]
FONT_CANDIDATES = (
    ROOT / ".agents/skills/persian-writing/assets/fonts/Vazirmatn-Bold.ttf",
    ROOT / ".agents/skills/persian-writing/assets/fonts/Vazirmatn-Regular.ttf",
)
FONT_PATH = next((path for path in FONT_CANDIDATES if path.is_file()), None)

SIZE = (1280, 720)
ASSETS_DIR = ROOT / "assets" / "apple_clash"


@lru_cache(maxsize=32)
def get_font(size: int):
    if FONT_PATH and FONT_PATH.is_file():
        return ImageFont.truetype(str(FONT_PATH), size)
    return ImageFont.load_default()


def _shaped(text: str) -> Tuple[str, dict]:
    """Shape Persian text with libraqm where available, otherwise arabic_reshaper + bidi."""
    if features.check("raqm"):
        return str(text), {"direction": "rtl"}
    return get_display(arabic_reshaper.reshape(str(text))), {}


def _digits(value: int) -> str:
    """Format an integer with Persian numerals and comma thousands separators."""
    return f"{value:,}".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _draw_text_with_shadow(
    img: Image.Image,
    xy: Tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
    stroke_fill: Optional[str] = None,
    stroke_width: int = 0,
    shadow_offset: Tuple[int, int] = (0, 4),
    shadow_blur: int = 8,
    shadow_opacity: float = 0.85,
    anchor: str = "ms",
):
    """Draw blurred drop shadow layer first, followed by stroked text."""
    visual, kwargs = _shaped(text)
    if stroke_width and stroke_fill is None:
        stroke_fill = fill

    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text(
        (xy[0] + shadow_offset[0], xy[1] + shadow_offset[1]),
        visual,
        font=font,
        fill=(0, 0, 0, 255),
        anchor=anchor,
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, 255),
        **kwargs,
    )
    box = shadow.getchannel("A").getbbox()
    if box is not None:
        pad = max(shadow_blur * 3, 4)
        box = (
            max(0, box[0] - pad),
            max(0, box[1] - pad),
            min(img.width, box[2] + pad),
            min(img.height, box[3] + pad),
        )
        blurred = shadow.crop(box).filter(ImageFilter.GaussianBlur(shadow_blur))
        blurred.putalpha(
            blurred.getchannel("A").point(lambda value: round(value * shadow_opacity))
        )
        img.alpha_composite(blurred, (box[0], box[1]))

    ImageDraw.Draw(img).text(
        xy,
        visual,
        font=font,
        fill=fill,
        anchor=anchor,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
        **kwargs,
    )


def _glow(
    image: Image.Image,
    xy: Tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    color: str,
    *,
    opacity: float = 0.55,
    blur: int = 14,
    anchor: str = "ms",
    dilate: int = 2,
):
    """Render a soft radiant glow behind the text glyphs."""
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    visual, kwargs = _shaped(text)
    ImageDraw.Draw(layer).text(xy, visual, font=font, fill=color, anchor=anchor, **kwargs)
    box = layer.getchannel("A").getbbox()
    if box is None:
        return
    pad = blur * 2 + dilate * 2 + 2
    box = (
        max(0, box[0] - pad),
        max(0, box[1] - pad),
        min(image.width, box[2] + pad),
        min(image.height, box[3] + pad),
    )
    cropped = layer.crop(box)
    for _ in range(dilate):
        cropped = cropped.filter(ImageFilter.MaxFilter(3))
    cropped = cropped.filter(ImageFilter.GaussianBlur(blur))
    cropped.putalpha(cropped.getchannel("A").point(lambda value: round(value * opacity)))
    image.alpha_composite(cropped, (box[0], box[1]))


@lru_cache(maxsize=16)
def _card_decoration(size: Tuple[int, int]):
    """Build rounded corner mask, border, and blurred drop shadow for the card."""
    width, height = size
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=16, fill=255
    )

    border = Image.new("RGBA", (width + 8, height + 8), "#1A0E06")
    outer = Image.new("L", border.size)
    ImageDraw.Draw(outer).rounded_rectangle(
        (0, 0, width + 7, height + 7), radius=20, fill=255
    )
    border.putalpha(outer)

    alpha = Image.new("L", (width + 104, height + 104))
    alpha.paste(outer, (48, 48))
    alpha = alpha.filter(ImageFilter.GaussianBlur(16)).point(
        lambda value: round(value * 0.55)
    )
    shadow = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    return mask, border, shadow


def _paste_card(image: Image.Image, card_image_bytes: bytes):
    """Resize, frame, and paste the player's card into the center of the canvas."""
    try:
        with Image.open(BytesIO(card_image_bytes)) as source:
            card = ImageOps.contain(
                ImageOps.exif_transpose(source).convert("RGBA"),
                (390, 530),
                Image.Resampling.LANCZOS,
            )
    except Exception:
        # Fallback card canvas if bytes are invalid or missing
        card = Image.new("RGBA", (360, 480), "#1A2238")

    x, y = (SIZE[0] - card.width) // 2, 310 - (card.height // 2)
    mask, border, shadow = _card_decoration(card.size)
    image.alpha_composite(shadow, (x - 4 - 48 + 8, y - 4 - 48 + 8))
    image.alpha_composite(border, (x - 4, y - 4))
    image.paste(card, (x, y), mask)


def _buff_fire_overlay() -> Image.Image:
    """Fiery orange ambient lighting layer for Sib-Buff attacks."""
    alpha = ImageOps.invert(Image.radial_gradient("L")).resize(SIZE)
    alpha = alpha.point(lambda value: round(value * 0.22))
    light = Image.new("RGBA", SIZE, (255, 87, 34, 0))
    light.putalpha(alpha)
    return light


def compose_war_attack_image(
    war_id: int,
    user_id: int,
    card_image_bytes: bytes,
    damage: int,
    is_buff: bool,
    perspective: str,  # 'attacker' or 'defender'
) -> bytes:
    """Compose a full 1280x720 JPEG battle dispatch with pre-generated background, card, and damage text."""
    bg_filename = (
        "war_bg_attacker.jpg" if perspective == "attacker" else "war_bg_defender.jpg"
    )
    bg_path = ASSETS_DIR / bg_filename

    if bg_path.is_file():
        image = Image.open(bg_path).convert("RGBA")
    else:
        # Fallback canvas if asset path is missing (e.g. isolated test environments)
        image = Image.new("RGBA", SIZE, (25, 20, 20, 255))

    # Apply fiery ambient overlay if Sib-Buff is used
    if is_buff:
        image.alpha_composite(_buff_fire_overlay())

    # Paste player card
    _paste_card(image, card_image_bytes)

    # Damage typography & glow
    damage_text = _digits(damage)
    damage_font = get_font(150)
    text_pos = (640, 648)

    if is_buff:
        damage_color = "#FF7043"  # fiery orange
        stroke_color = "#2E0A0A"
        glow_color = "#FF5722"
        glow_opacity = 0.70
    else:
        damage_color = "#FCECD8"  # warm cream
        stroke_color = "#2A1408"
        glow_color = "#FFD24A"
        glow_opacity = 0.45

    _glow(
        image,
        text_pos,
        damage_text,
        damage_font,
        glow_color,
        opacity=glow_opacity,
        blur=16,
        anchor="ms",
    )
    _draw_text_with_shadow(
        image,
        text_pos,
        damage_text,
        damage_font,
        fill=damage_color,
        stroke_fill=stroke_color,
        stroke_width=3,
        anchor="ms",
    )

    # Encode to JPEG bytes
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=88)
    return output.getvalue()
