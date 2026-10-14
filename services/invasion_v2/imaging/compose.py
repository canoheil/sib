"""Fast static-art composition. No scenery or enemy geometry is drawn here.

The compose_* API returns JPEG bytes. The three older image-returning adapters
remain for compatibility; production presentation and private admin preview use
the compose_* API directly. No gameplay-handler changes are required.

Phase 8E: phone-legible typography (1280x720 displays ~360px wide on mobile),
drop shadows on every Persian string, damage-number glow, and dramatic
victory/defeat status words with radial vignettes.
"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps, features

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError as exc:
    raise ImportError("Persian invasion imaging needs arabic-reshaper and python-bidi") from exc

from . import templates
from .templates import SIZE, label, load_layout, load_silhouette


__all__ = [
    "compose_announcement_image", "compose_join_image", "compose_result_image",
    "_draw_text_with_shadow", "_draw_radial_vignette",
]


def _jpeg(image: Image.Image) -> bytes:
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=88)
    return output.getvalue()


def _shaped(text):
    """The established shaping pipeline shared with templates.label: libraqm
    where available, otherwise local reshaper + bidi. Never fork this."""
    if features.check("raqm"):
        return str(text), {"direction": "rtl"}
    return get_display(arabic_reshaper.reshape(str(text))), {}


_MEASURE = ImageDraw.Draw(Image.new("L", (1, 1)))


def _fit_size(text, nominal, *, max_width, min_size=16):
    """Whole-point shrink until the shaped text fits (same loop as label)."""
    visual, kwargs = _shaped(text)
    size = nominal
    while size > min_size and _MEASURE.textlength(visual, font=templates.font(size), **kwargs) > max_width:
        size -= 1
    return size


def _draw_text_with_shadow(img, xy, text, font, fill,
                           stroke_fill=None, stroke_width=0,
                           shadow_offset=(3, 3),
                           shadow_blur=6,
                           shadow_opacity=0.7,
                           anchor="mm"):
    """Blurred black drop shadow first, then the (optionally stroked) text.

    Every Persian string on every invasion image goes through this helper so
    the shared reshaper/bidi shaping and a readable shadow are guaranteed.
    The shadow is blurred on a tight crop, not the whole canvas.
    """
    visual, kwargs = _shaped(text)
    if stroke_width and stroke_fill is None:
        stroke_fill = fill
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text(
        (xy[0] + shadow_offset[0], xy[1] + shadow_offset[1]), visual, font=font,
        fill=(0, 0, 0, 255), anchor=anchor, stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, 255), **kwargs)
    box = shadow.getchannel("A").getbbox()
    if box is not None:
        pad = max(shadow_blur * 3, 4)
        box = (max(0, box[0] - pad), max(0, box[1] - pad),
               min(img.width, box[2] + pad), min(img.height, box[3] + pad))
        blurred = shadow.crop(box).filter(ImageFilter.GaussianBlur(shadow_blur))
        blurred.putalpha(blurred.getchannel("A").point(
            lambda value: round(value * shadow_opacity)))
        img.alpha_composite(blurred, (box[0], box[1]))
    ImageDraw.Draw(img).text(xy, visual, font=font, fill=fill, anchor=anchor,
                             stroke_width=stroke_width, stroke_fill=stroke_fill, **kwargs)


def _glow(image, xy, text, font, color, *, opacity, blur, anchor="mm", dilate=2):
    """Soft glow behind text: a dilated, blurred copy of the glyphs."""
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    visual, kwargs = _shaped(text)
    ImageDraw.Draw(layer).text(xy, visual, font=font, fill=color, anchor=anchor, **kwargs)
    box = layer.getchannel("A").getbbox()
    if box is None:
        return
    pad = blur * 2 + dilate * 2 + 2
    box = (max(0, box[0] - pad), max(0, box[1] - pad),
           min(image.width, box[2] + pad), min(image.height, box[3] + pad))
    cropped = layer.crop(box)
    for _ in range(dilate):
        cropped = cropped.filter(ImageFilter.MaxFilter(3))
    cropped = cropped.filter(ImageFilter.GaussianBlur(blur))
    cropped.putalpha(cropped.getchannel("A").point(lambda value: round(value * opacity)))
    image.alpha_composite(cropped, (box[0], box[1]))


def _paste_enemy(image, enemy_type, position, *, opacity=1.0, scale=1.0):
    enemy = load_silhouette(enemy_type)
    if scale != 1.0:
        enemy = enemy.resize((round(enemy.width * scale), round(enemy.height * scale)),
                             Image.Resampling.LANCZOS)
    if opacity != 1.0:
        enemy.putalpha(enemy.getchannel("A").point(lambda value: round(value * opacity)))
    image.alpha_composite(enemy, position)


def _announcement(enemy_type, region_name, enemy_label_fa, *, legacy=False):
    image = load_layout("announcement").convert("RGBA")
    _paste_enemy(image, enemy_type, (80, 80))
    if legacy:
        draw = ImageDraw.Draw(image)
        label(draw, (1216, 100), "تهاجم شیاسیب", 44, fill="#FCECD8",
              stroke_width=2, stroke_fill="#3A1A08", max_width=350)
        label(draw, (1216, 200), region_name, 44, fill="#FCECD8",
              stroke_width=2, stroke_fill="#3A1A08", max_width=350)
        label(draw, (1216, 280), enemy_label_fa, 28,
              fill="#D9C29A", stroke_width=0, max_width=350)
        return image
    region_size = _fit_size(region_name, 88, max_width=380)
    _draw_text_with_shadow(image, (1216, 110), region_name, templates.font(region_size),
                           fill="#FCECD8", stroke_fill="#3A1A08", stroke_width=2, anchor="ra")
    enemy_size = _fit_size(enemy_label_fa, 52, max_width=350)
    _draw_text_with_shadow(image, (1216, 190), enemy_label_fa, templates.font(enemy_size),
                           fill="#D9C29A", stroke_fill="#2A1408", stroke_width=1, anchor="ra")
    return image


def compose_announcement_image(enemy_type, region_name, enemy_label_fa) -> bytes:
    return _jpeg(_announcement(enemy_type, region_name, enemy_label_fa))


@lru_cache(maxsize=32)
def _card_decoration(size):
    """Reusable UI masks only, not procedural artwork. 4px outer dark border."""
    width, height = size
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width-1, height-1), radius=16, fill=255)
    border = Image.new("RGBA", (width+8, height+8), "#1A0E06")
    outer = Image.new("L", border.size)
    ImageDraw.Draw(outer).rounded_rectangle((0, 0, width+7, height+7), radius=20, fill=255)
    border.putalpha(outer)
    # Blur padding keeps the shadow smooth rather than cutting it at the card edge.
    alpha = Image.new("L", (width+104, height+104))
    alpha.paste(outer, (48, 48))
    alpha = alpha.filter(ImageFilter.GaussianBlur(16)).point(lambda value: round(value * .55))
    shadow = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    return mask, border, shadow


def _paste_card(image, card_image_bytes):
    with Image.open(BytesIO(card_image_bytes)) as source:
        card = ImageOps.contain(ImageOps.exif_transpose(source).convert("RGBA"),
                                (420, 580), Image.Resampling.LANCZOS)
    x, y = (SIZE[0]-card.width)//2, 340-card.height//2
    mask, border, shadow = _card_decoration(card.size)
    image.alpha_composite(shadow, (x-4-48+8, y-4-48+8))
    image.alpha_composite(border, (x-4, y-4))
    image.paste(card, (x, y), mask)


@lru_cache(maxsize=1)
def _critical_light():
    """Reusable compositing mask: radial gold light, at most 25% opacity."""
    alpha = ImageOps.invert(Image.radial_gradient("L")).resize(SIZE)
    alpha = alpha.point(lambda value: round(value * .25))
    light = Image.new("RGBA", SIZE, (255, 194, 46, 0))
    light.putalpha(alpha)
    return light


def _digits(value):
    return f"{value:,}".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _join(enemy_type, card_image_bytes, username, damage, is_crit, enemy_hp_remaining,
          *, legacy=False):
    image = load_layout("join").convert("RGBA")
    _paste_enemy(image, enemy_type, (60, 80), opacity=.40)
    if is_crit:
        image.alpha_composite(_critical_light())
    _paste_card(image, card_image_bytes)
    if legacy:
        draw = ImageDraw.Draw(image)
        label(draw, (1216, 64), username, 32, fill="white", stroke_width=2, max_width=310)
        damage_text = _digits(damage)
        damage_text = f"{damage_text} اسیب" if damage else "سایه بر میدان افتاد"
        if is_crit:
            glow = Image.new("RGBA", (780, 160))
            label(ImageDraw.Draw(glow), (390, 122), damage_text, 64,
                  anchor="ms", fill="#FFD24A", stroke_width=3,
                  stroke_fill="#FFD24A", max_width=740)
            image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(9)), (250, 560))
            draw = ImageDraw.Draw(image)
            label(draw, (70, 40), "کریتیکال", 28, anchor="la", fill="#FFD24A", stroke_width=2)
        label(draw, (640, 682), damage_text, 64, anchor="ms",
              fill="#FFD24A" if is_crit else "#FCECD8", stroke_width=2, max_width=720)
        return image
    user_size = _fit_size(username, 60, max_width=350)
    _draw_text_with_shadow(image, (1216, 72), username, templates.font(user_size),
                           fill="white", stroke_fill="#2A1408", stroke_width=2, anchor="ra")
    if is_crit:
        _draw_text_with_shadow(image, (70, 52), "ضربه کاری", templates.font(28),
                               fill="#FFD24A", stroke_fill="#3A1A08", stroke_width=1, anchor="la")
    # Post-attack HP is stored on the row: 0 means this blow was the kill.
    is_kill = enemy_hp_remaining is not None and enemy_hp_remaining <= 0
    damage_text = _digits(damage)
    damage_fill = "#B8E986" if is_kill else "#FFD24A" if is_crit else "#FCECD8"
    glow_opacity = 0.60 if (is_crit or is_kill) else 0.40
    glow_blur = 16 if (is_crit or is_kill) else 12
    _glow(image, (640, 648), damage_text, templates.font(160), damage_fill,
          opacity=glow_opacity, blur=glow_blur, anchor="ms")
    _draw_text_with_shadow(image, (640, 648), damage_text, templates.font(160),
                           fill=damage_fill, stroke_fill="#2A1408", stroke_width=2, anchor="ms")
    if enemy_hp_remaining is not None:
        _draw_text_with_shadow(image, (1216, 662), _digits(enemy_hp_remaining),
                               templates.font(44), fill="#B8A98F", stroke_fill="#1A0E06",
                               stroke_width=1, anchor="rs")
    return image


def compose_join_image(enemy_type, card_image_bytes, username, damage, is_crit,
                       enemy_hp_remaining) -> bytes:
    return _jpeg(_join(enemy_type, card_image_bytes, username, damage, is_crit,
                       enemy_hp_remaining))


def _result(status, region_name, enemy_type, reward_lines):
    if status not in ("victory", "defeat"):
        raise ValueError("Result image requires victory or defeat")
    image = load_layout(status).convert("RGBA")
    _paste_enemy(image, enemy_type, (360, -32), opacity=.35, scale=1.4)
    region_size = _fit_size(region_name, 88, max_width=1120)
    _draw_text_with_shadow(image, (640, 52), region_name, templates.font(region_size),
                           fill="#FCECD8", stroke_fill="#3A1A08", stroke_width=2, anchor="ma")
    word = "پیروزی" if status == "victory" else "شکست"
    _draw_text_with_shadow(image, (640, 350), word, templates.font(200),
                           fill="#FCECD8" if status == "victory" else "#efa7a0",
                           stroke_width=4, anchor="mm")
    if status == "victory":
        lines = list(reward_lines)
        spacing = min(64, 192 // max(1, len(lines)))
        for index, line in enumerate(lines):
            center_y = 680 - (len(lines) - 1 - index) * spacing
            line_size = min(54, max(1, spacing - 10))
            _draw_text_with_shadow(image, (640, center_y), line, templates.font(line_size),
                                   fill="#FCECD8", stroke_width=1, anchor="ms")
    return image


def compose_result_image(status, region_name, enemy_type, reward_lines) -> bytes:
    return _jpeg(_result(status, region_name, enemy_type, reward_lines))


# Compatibility adapters: existing callers and user-facing labels stay intact.
def announcement_image(enemy_type: str, region: str, faction: str) -> Image.Image:
    return _announcement(enemy_type, region[:36], faction[:34], legacy=True).convert("RGB")


def load_card_image_bytes(card_path: Path | None, *, placeholder=False) -> bytes:
    """Read local art; only private preview may explicitly request a placeholder."""
    if card_path is None:
        if not placeholder:
            raise FileNotFoundError("No local framed Main Card art under assets/characters/{code}.jpg")
        card = Image.new("RGBA", (384, 502), "#141c2f")
        _draw_text_with_shadow(card, (335, 285), "کارت اصلی انتخاب نشده",
                               templates.font(25), fill="#f3eee1", anchor="ra")
        data = _jpeg(card)
    else:
        if not Path(card_path).is_file():
            raise FileNotFoundError(f"Main Card artwork missing: {card_path}")
        data = Path(card_path).read_bytes()
    return data


def join_image(enemy_type: str, card_path: Path | None, username: str, damage: int,
               is_crit: bool, *, placeholder=False) -> Image.Image:
    data = load_card_image_bytes(card_path, placeholder=placeholder)
    return _join(enemy_type, data, username[:28], damage, is_crit, None,
                 legacy=True).convert("RGB")


def result_image(enemy_type: str, status: str, region: str, faction: str) -> Image.Image:
    if status not in ("VICTORY", "DEFEAT"):
        raise ValueError("Result image requires VICTORY or DEFEAT")
    return _result(status.lower(), f"{region[:30]} · {faction[:22]}", enemy_type, ()).convert("RGB")
