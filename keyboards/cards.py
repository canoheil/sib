"""Inline keyboards of the card screens (view + upgrade)."""

from aiogram.types import InlineKeyboardMarkup

from sib.keyboards.common import danger_button, nav_back_row, success_button

UPGRADE_BUTTON_TEXT = "⬆️ ارتقای کارت"
BACK_TO_CARDS_TEXT = "↩️ بازگشت به کارت‌های من"
BACK_TO_CARD_TEXT = "↩️ بازگشت"
MAIN_MENU_TEXT = "🏠 منوی اصلی"

# Callback data (short, prefixed, validated by the handlers).
CARD_VIEW = "card_panel:{card_id}"
CARD_MAIN_SET = "card_main_set:{card_id}:{expected_main_card_id}"
CARD_UPGRADE = "card_upgrade:{card_id}"
CARD_UPGRADE_DO = "card_upgrade_do:{card_id}:{level}"
MAIN_CARD_BUTTON_TEXT = "⭐ انتخاب به‌عنوان کارت اصلی"


def get_card_detail_keyboard(
    card_id: int, current_main_card_id: int | None = None
) -> InlineKeyboardMarkup:
    """Card view: Main Card selector, upgrade action and collection navigation.

    The callback carries the Main Card id visible when this screen was
    rendered (zero means no Main Card) so the service can reject stale writes.
    """
    rows = []
    if current_main_card_id != card_id:
        rows.append(
            [
                success_button(
                    text=MAIN_CARD_BUTTON_TEXT,
                    callback_data=CARD_MAIN_SET.format(
                        card_id=card_id,
                        expected_main_card_id=current_main_card_id or 0,
                    ),
                )
            ]
        )
    rows.append(
        [
            success_button(
                text=UPGRADE_BUTTON_TEXT,
                callback_data=CARD_UPGRADE.format(card_id=card_id),
            )
        ]
    )
    rows += nav_back_row(BACK_TO_CARDS_TEXT, "apple_cards")
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_card_upgrade_keyboard(
    card_id: int, level: int, can_upgrade: bool
) -> InlineKeyboardMarkup:
    """Upgrade screen. The action carries the level the player SAW, so a
    replayed/double-tapped button can never buy a second level. At the
    maximum level there is no action button at all."""
    rows = []
    if can_upgrade:
        rows.append(
            [
                success_button(
                    text=UPGRADE_BUTTON_TEXT,
                    callback_data=CARD_UPGRADE_DO.format(card_id=card_id, level=level),
                )
            ]
        )
    rows += nav_back_row(BACK_TO_CARD_TEXT, CARD_VIEW.format(card_id=card_id))
    rows.append([danger_button(text=MAIN_MENU_TEXT, callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
