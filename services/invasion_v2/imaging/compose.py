"""Fast static-art composition. No scenery or enemy geometry is drawn here.

The compose_* API returns JPEG bytes. The three older image-returning adapters
remain for compatibility; production presentation and private admin preview use
the compose_* API directly. No gameplay-handler changes are required.

Phase 8E: phone-legible typography (1280x720 displays ~360px wide on mobile),
drop shadows on every Persian string, damage-number glow, and dramatic
victory/defeat status words with radial vignettes.

Follow-up fix: the victory word is spelled "پیروزی", and the result region name
and reward lines are drawn straight onto the art - no dark plate behind them.
"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps, features

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError as exc:
    raise ImportError("Persian invasion imaging needs arabic-reshaper and python-bidi") from exc

from sib.core.logging import logger

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


def _fit_size(text, nominal, *, max_width, min_size=16, warn_key=None):
    """Whole-point shrink until the shaped text fits (same loop as label)."""
    visual, kwargs = _shaped(text)
    size = nominal
    while size > min_size and _MEASURE.textlength(visual, font=templates.font(size), **kwargs) > max_width:
        size -= 1
    if warn_key is not None and size < nominal:
        logger.warning("%s: font shrunk %s -> %s", warn_key, nominal, size)
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


def _draw_radial_vignette(img, center_xy, radius_x, radius_y, inner_alpha, outer_alpha=0.0,
                          color=(0, 0, 0)):
    """Smooth radial fade from inner_alpha at the center to outer_alpha at the
    ellipse edge, pasted as an RGBA layer (numpy gradient for smoothness)."""
    cx, cy = center_xy
    width, height = img.size
    ys, xs = np.ogrid[:height, :width]
    distance = np.sqrt(((xs - cx) / radius_x) ** 2 + ((ys - cy) / radius_y) ** 2)
    blend = np.clip(distance, 0.0, 1.0)
    alpha = (inner_alpha + (outer_alpha - inner_alpha) * blend) * 255
    layer = Image.new("RGBA", img.size, (int(color[0]), int(color[1]), int(color[2]), 0))
    layer.putalpha(Image.fromarray(alpha.astype("uint8"), mode="L"))
    img.alpha_composite(layer)


# Phase 8E status words: dramatic, not pastel. Vignette -> glow -> text.
_STATUS_WORDS = {
    "victory": (
        "پیروزی", "#C8A02E", "#3A2408", 8,      # deep gold, warm dark-brown stroke
        "#E8C766", 0.60, 20,                    # glow: color, opacity, blur radius
        (640, 325), 450, 180, 0.55, (60, 40, 10)  # warm halo vignette
    ),
    "defeat": (
        "شکست", "#7A0E0E", "#1A0202", 10,       # deep blood red, near-black stroke
        "#8B0000", 0.70, 24,                    # glow: color, opacity, blur radius
        (640, 325), 450, 200, 0.75, (0, 0, 0)   # dark radial vignette
    ),
}


def _text_ink_box(size, xy, text, font, anchor="mm"):
    """Bbox of the drawn ink for a shaped string at an anchored position."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    visual, kwargs = _shaped(text)
    ImageDraw.Draw(layer).text(xy, visual, font=font, fill=(255, 255, 255, 255),
                               anchor=anchor, **kwargs)
    return layer.getchannel("A").getbbox()


# Result text sits directly on the painted art: no plate, pill or box behind
# it. Legibility comes from a heavier stroke plus a wider, softer drop shadow
# (a rounded rectangle drawn with an alpha fill is opaque once the RGBA canvas
# is flattened to JPEG, which is what produced the old solid black boxes).
_RESULT_SHADOW = {"shadow_offset": (0, 4), "shadow_blur": 10, "shadow_opacity": 0.85}


def _decorative_line(image, center):
    """Thin 160x3 line in #6E3511 with a soft glow, centered on center."""
    x, y = center
    layer = Image.new("RGBA", (200, 44), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rectangle((20, 20, 180, 23), fill=(164, 92, 44, 230))
    layer = layer.filter(ImageFilter.GaussianBlur(4))
    image.alpha_composite(layer, (x - 100, y - 22))
    ImageDraw.Draw(image).rectangle((x - 80, y - 1, x + 80, y + 2), fill=(110, 53, 17))


def _draw_apple_icon(image, center):
    """Small Pillow-drawn decorative apple: red circle (~20px radius) + leaf."""
    x, y = center
    leaf = Image.new("RGBA", (40, 26), (0, 0, 0, 0))
    ImageDraw.Draw(leaf).ellipse((4, 5, 36, 21), fill=(84, 132, 48, 255))
    leaf = leaf.rotate(-35, expand=True, resample=Image.Resampling.BICUBIC)
    image.alpha_composite(leaf, (round(x + 4 - leaf.width // 2), round(y - 36)))
    draw = ImageDraw.Draw(image)
    draw.line((x + 8, y - 15, x + 14, y - 26), fill=(92, 56, 22, 255), width=4)
    radius = 20
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(176, 32, 26, 255))
    draw.ellipse((x - 13, y - 13, x - 3, y - 4), fill=(224, 96, 76, 210))


# Announcement panel: the dark plate on the right third (x ~860..1280).
_PANEL_X = 1070
_REGION_Y = 216  # ~30% of panel height


def _announcement(enemy_type, region_name, enemy_label_fa, *, legacy=False):
    image = load_layout("announcement").convert("RGBA")
    _paste_enemy(image, enemy_type, (80, 80))
    if legacy:
        draw = ImageDraw.Draw(image)
        label(draw, (1216, 100), "یورش شیاسیب", 44, fill="#FCECD8",
              stroke_width=2, stroke_fill="#3A1A08", max_width=350)
        label(draw, (1216, 200), region_name, 44, fill="#FCECD8",
              stroke_width=2, stroke_fill="#3A1A08", max_width=350)
        label(draw, (1216, 280), enemy_label_fa, 28,
              fill="#D9C29A", stroke_width=0, max_width=350)
        return image
    # Rebalanced panel: region name centered at ~30% height, decorative line,
    # enemy label below the line with a small apple icon to its right.
    region_size = _fit_size(region_name, 88, max_width=380)
    region_font = templates.font(region_size)
    _draw_text_with_shadow(image, (_PANEL_X, _REGION_Y), region_name, region_font,
                           fill="#FCECD8", stroke_fill="#3A1A08", stroke_width=2, anchor="mm")
    region_box = _text_ink_box(image.size, (_PANEL_X, _REGION_Y), region_name, region_font)
    line_y = region_box[3] + 36
    _decorative_line(image, (_PANEL_X, line_y))
    enemy_size = _fit_size(enemy_label_fa, 52, max_width=340)
    enemy_font = templates.font(enemy_size)
    enemy_center_y = line_y + 64
    _draw_text_with_shadow(image, (_PANEL_X, enemy_center_y), enemy_label_fa, enemy_font,
                           fill="#D9C29A", stroke_fill="#2A1408", stroke_width=1, anchor="mm")
    enemy_box = _text_ink_box(image.size, (_PANEL_X, enemy_center_y), enemy_label_fa, enemy_font)
    icon_x = min(SIZE[0] - 28, enemy_box[2] + 34)
    _draw_apple_icon(image, (icon_x, enemy_center_y))
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
        damage_text = f"{damage_text} آسیب" if damage else "سایه بر میدان افتاد"
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
        _draw_text_with_shadow(image, (70, 52), "ضربهٔ کاری", templates.font(28),
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
    # Region name: always readable - 88px at y=70, plate-free over the art.
    region_size = _fit_size(region_name, 88, max_width=int(SIZE[0] * 0.9), min_size=56,
                            warn_key="inv2 result region name too long")
    region_font = templates.font(region_size)
    _draw_text_with_shadow(image, (640, 70), region_name, region_font,
                           fill="#FCECD8", stroke_fill="#2A1408", stroke_width=4,
                           anchor="ma", **_RESULT_SHADOW)
    # Dramatic status word: vignette behind, glow, then the stroked word.
    (word, fill, stroke, stroke_width, glow_color, glow_opacity, glow_blur,
     center, radius_x, radius_y, inner_alpha, vignette_color) = _STATUS_WORDS[status]
    _draw_radial_vignette(image, center, radius_x, radius_y, inner_alpha,
                          color=vignette_color)
    word_font = templates.font(200)
    _glow(image, center, word, word_font, glow_color, opacity=glow_opacity,
          blur=glow_blur, anchor="mm")
    _draw_text_with_shadow(image, center, word, word_font, fill=fill,
                           stroke_fill=stroke, stroke_width=stroke_width, anchor="mm")
    if status == "victory":
        # Reward lines: 54px, bottom-center, plate-free over the art.
        lines = list(reward_lines)
        spacing = 86
        for index, line in enumerate(lines):
            center_y = 672 - (len(lines) - 1 - index) * spacing
            line_size = _fit_size(line, 54, max_width=1080)
            line_font = templates.font(line_size)
            _draw_text_with_shadow(image, (640, center_y), line, line_font,
                                   fill="#FCECD8", stroke_fill="#2A1408", stroke_width=2,
                                   anchor="ms", **_RESULT_SHADOW)
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
