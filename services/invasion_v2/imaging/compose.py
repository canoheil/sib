"""Offline announcement, join and result composition using existing framed art."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageOps
from .backgrounds import background, silhouette
from .templates import SIZE, CARD_BOX, BAND, label


def _base(enemy_type: str, *, large=False):
    img = background(enemy_type).convert("RGBA")
    box = (290, 62, 990, 695) if large else (95, 145, 510, 625)
    img.alpha_composite(silhouette(enemy_type, box))
    return img


def announcement_image(enemy_type: str, region: str, faction: str) -> Image.Image:
    img = _base(enemy_type)
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((618, 178, 1190, 502), 36, fill=(9, 11, 18, 195), outline=(185, 198, 218, 90), width=3)
    label(d, (1130, 265), "تهاجم شیاسیب", 56)
    label(d, (1130, 365), region[:36], 42)
    label(d, (1130, 438), faction[:34], 29, fill="#c7a65a")
    return img.convert("RGB")


def join_image(enemy_type: str, card_path: Path | None, username: str, damage: int, is_crit: bool, *, placeholder=False) -> Image.Image:
    img = _base(enemy_type)
    if card_path is None:
        if not placeholder:
            raise FileNotFoundError("No local framed Main Card art under assets/characters/{code}.jpg")
        card = Image.new("RGB", (384, 502), "#141c2f")
        label(ImageDraw.Draw(card), (335, 285), "کارت اصلی انتخاب نشده", 25)
    else:
        if not Path(card_path).is_file():
            raise FileNotFoundError(f"Main Card artwork missing: {card_path}")
        with Image.open(card_path) as original:
            card = ImageOps.fit(original.convert("RGB"), (384, 502), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", card.size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *card.size), radius=28, fill=255)
    img.paste(card, CARD_BOX[:2], mask)
    d = ImageDraw.Draw(img, "RGBA")
    d.rounded_rectangle((905, 80, 1230, 240), 26, fill=(9, 11, 18, 205))
    label(d, (1198, 155), username[:28], 30)
    d.rectangle(BAND, fill=(9, 11, 18, 216))
    fa_damage = f"{damage:,}".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    label(d, (1170, 650), f"{fa_damage} آسیب" if damage else "سایه بر میدان افتاد", 44)
    if is_crit:
        flash = Image.new("RGBA", SIZE, (225, 96, 65, 21))
        img.alpha_composite(flash)
        label(ImageDraw.Draw(img), (470, 650), "کریتیکال", 38, fill="#ffe0ab")
    return img.convert("RGB")


def result_image(enemy_type: str, status: str, region: str, faction: str) -> Image.Image:
    if status not in ("VICTORY", "DEFEAT"):
        raise ValueError("Result image requires VICTORY or DEFEAT")
    img = _base(enemy_type, large=True)
    tint = (31, 126, 64, 49) if status == "VICTORY" else (126, 24, 38, 72)
    img.alpha_composite(Image.new("RGBA", SIZE, tint))
    d = ImageDraw.Draw(img, "RGBA")
    d.rectangle((0, 0, 1280, 135), fill=(9, 11, 18, 205))
    d.rectangle(BAND, fill=(9, 11, 18, 230))
    label(d, (1190, 78), f"{region[:30]} · {faction[:22]}", 35)
    label(d, (1145, 610), "پیروزی" if status == "VICTORY" else "شکست", 65, fill="#ebd38c" if status == "VICTORY" else "#efa7a0")
    return img.convert("RGB")
