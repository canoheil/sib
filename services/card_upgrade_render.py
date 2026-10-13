"""Text + artwork for the Card Upgrade screen (presentation only).

Business rules live in ``services/card_upgrade.py``; this module only turns
an ``UpgradePreview`` (or an upgrade refusal) into Persian text and resolves
the screen's artwork. Attribute and item names/icons come from the project's
own catalogs (``card_progression.CARD_ATTRIBUTES``, ``items.ITEM_CATALOG``).
"""

from typing import Any, List, Optional

from sib.core.text import escape_markdown
from sib.services.card_progression import CARD_ATTRIBUTES, MAX_CARD_LEVEL, CardAttributes
from sib.services.card_upgrade import (
    InsufficientResourcesError,
    MissingResources,
    UpgradeCost,
    UpgradePreview,
    UpgradeResult,
)
from sib.services.items import item_emoji, item_label

# Normal Apples are the wallet currency (not an inventory item), so their
# display name is defined here once.
APPLE_EMOJI = "🍎"
APPLE_LABEL = "سیب معمولی"

# Asset key of the 1:1 artwork (``assets/items/card_upgrade.jpg``, replaceable
# at runtime with ``/setasset card_upgrade``).
ART_KEY = "card_upgrade"

_art_file_id_cache: dict[str, str] = {}


def _attribute_block(stats: CardAttributes) -> List[str]:
    lines = [
        f"{emoji} {label}: `{getattr(stats, key)}`" for key, emoji, label in CARD_ATTRIBUTES
    ]
    lines.append(f"📊 مجموع: `{stats.total}`")
    return lines


def apple_label() -> str:
    return f"{APPLE_EMOJI} {APPLE_LABEL}"


def material_label(item_key: str) -> str:
    return f"{item_emoji(item_key)} {item_label(item_key)}"


def cost_lines(cost: UpgradeCost) -> List[str]:
    return [
        f"{apple_label()} × `{cost.apples}`",
        f"{material_label(cost.item_key)} × `{cost.item_quantity}`",
    ]


def missing_lines(missing: MissingResources) -> List[str]:
    """Only the resources that are actually short, with the exact amount."""
    lines: List[str] = []
    if missing.apples > 0:
        lines.append(f"{apple_label()}: `{missing.apples}`")
    if missing.item_quantity > 0:
        lines.append(f"{material_label(missing.item_key)}: `{missing.item_quantity}`")
    return lines


def render_upgrade_screen(preview: UpgradePreview) -> str:
    """The Card Upgrade screen caption (kept well under Telegram's 1024)."""
    lines = [f"⬆️ *ارتقای کارت {escape_markdown(preview.character_name)}*", ""]

    if preview.is_max_level:
        lines += [
            "🏆 **این کارت به بیشترین سطح رسیده!**",
            f"سطح کارت: `{preview.level}` از `{MAX_CARD_LEVEL}`",
            "",
            *_attribute_block(preview.current),
            "",
            "ارتقای دیگری برای این کارت وجود ندارد.",
        ]
        return "\n".join(lines)

    assert preview.after is not None and preview.cost is not None
    lines += [
        f"🎴 سطح کارت: `{preview.level}` ▶️ `{preview.next_level}`",
        "",
        "**الان:**",
        *_attribute_block(preview.current),
        "",
        "**بعد از ارتقا:**",
        *_attribute_block(preview.after),
        "",
        "**هزینه:**",
        *cost_lines(preview.cost),
        "",
        "**موجودی تو:**",
        f"{apple_label()}: `{preview.apple_balance}`",
        f"{material_label(preview.cost.item_key)}: `{preview.item_balance}`",
    ]
    if preview.missing is not None and not preview.missing.is_empty:
        lines += ["", "⚠️ **منابع کم‌شده:**", *missing_lines(preview.missing)]
    return "\n".join(lines)


def missing_alert_text(error: InsufficientResourcesError) -> str:
    """Short popup listing exactly what is missing (alert limit: 200 chars)."""
    lines = ["منابع کافی نیست 🍏", "", "منابع کم‌شده:", *missing_lines(error.missing)]
    return "\n".join(line.replace("`", "") for line in lines)


def success_toast(result: UpgradeResult) -> str:
    return f"🎉 کارت به سطح {result.to_level} ارتقا یافت!"


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------
async def resolve_art(session) -> Optional[Any]:
    """Photo argument for the upgrade screen (runtime asset > cache > bundled file)."""
    from sib.services.assets import AssetService
    from sib.services.character_art import photo_arg

    file_id, fs_path = await AssetService.resolve_photo(session, ART_KEY)
    if file_id:
        return file_id
    cached = _art_file_id_cache.get(ART_KEY)
    if cached and fs_path is not None:
        return cached
    return photo_arg(None, fs_path)


def remember_art(message: Any) -> None:
    """Cache Telegram's file_id after the first upload (faster later sends)."""
    photos = getattr(message, "photo", None)
    if isinstance(photos, (list, tuple)) and photos:
        file_id = getattr(photos[-1], "file_id", None)
        if isinstance(file_id, str) and file_id:
            _art_file_id_cache[ART_KEY] = file_id
