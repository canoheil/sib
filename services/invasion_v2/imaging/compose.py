"""Fast static-art composition. No scenery or enemy geometry is drawn here.

The compose_* API returns JPEG bytes. The three older image-returning adapters
remain for compatibility; production presentation and private admin preview use
the compose_* API directly. No gameplay-handler changes are required.
"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .templates import SIZE, label, load_layout, load_silhouette


__all__ = ["compose_announcement_image", "compose_join_image", "compose_result_image"]


def _jpeg(image: Image.Image) -> bytes:
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=88)
    return output.getvalue()


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
    draw = ImageDraw.Draw(image)
    if legacy:
        label(draw, (1216, 100), "تهاجم شیاسیب", 44, fill="#FCECD8",
              stroke_width=2, stroke_fill="#3A1A08", max_width=350)
    label(draw, (1216, 200 if legacy else 110), region_name, 44, fill="#FCECD8",
          stroke_width=2, stroke_fill="#3A1A08", max_width=350)
    label(draw, (1216, 280 if legacy else 190), enemy_label_fa, 28,
          fill="#D9C29A", stroke_width=0, max_width=350)
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
    draw = ImageDraw.Draw(image)
    label(draw, (1216, 64), username, 32, fill="white", stroke_width=2, max_width=310)
    damage_text = _digits(damage)
    if legacy:
        damage_text = f"{damage_text} آسیب" if damage else "سایه بر میدان افتاد"
    damage_size = 64 if legacy else 96
    if is_crit:
        glow = Image.new("RGBA", (780, 160))
        label(ImageDraw.Draw(glow), (390, 122), damage_text, damage_size,
              anchor="ms", fill="#FFD24A", stroke_width=3,
              stroke_fill="#FFD24A", max_width=740)
        image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(9)), (250, 560))
        draw = ImageDraw.Draw(image)
        label(draw, (70, 40), "کریتیکال" if legacy else "ضربه کاری", 28,
              anchor="la", fill="#FFD24A", stroke_width=2)
    label(draw, (640, 682), damage_text, damage_size, anchor="ms",
          fill="#FFD24A" if is_crit else "#FCECD8", stroke_width=2, max_width=720)
    if enemy_hp_remaining is not None:
        label(draw, (1216, 662), _digits(enemy_hp_remaining), 28,
              fill="#B8A98F", anchor="rs", max_width=240)
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
    draw = ImageDraw.Draw(image)
    label(draw, (640, 52), region_name, 44, anchor="ma", fill="#FCECD8",
          stroke_width=2, max_width=1120)
    label(draw, (640, 350), "پیروزی" if status == "victory" else "شکست", 120,
          anchor="mm", fill="#FCECD8" if status == "victory" else "#efa7a0",
          stroke_width=4)
    if status == "victory":
        lines = list(reward_lines)
        spacing = min(48, 192 // max(1, len(lines)))
        for index, line in enumerate(lines):
            label(draw, (640, 680-(len(lines)-1-index)*spacing), line,
                  min(32, max(1, spacing-10)), anchor="ms", fill="#FCECD8",
                  stroke_width=1, max_width=1120)
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
        card = Image.new("RGB", (384, 502), "#141c2f")
        label(ImageDraw.Draw(card), (335, 285), "کارت اصلی انتخاب نشده", 25)
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
