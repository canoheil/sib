"""Inline keyboards for common bot interactions.

Semantic glass-button system (Bot API 10.3 styles):
- success (green)  -> positive/confirm actions (buy, harvest, join, find)
- danger (red)     -> back / cancel / destructive actions
- primary (blue)   -> navigation / informational actions
- unstyled         -> neutral actions without a dedicated style

Game boards may define a separate style map for their structural controls and
playable cells. Colors communicate meaning - never decoration.
"""

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_membership_keyboard(channel_link: str) -> InlineKeyboardMarkup:
    """Keyboard prompting user to join official channel and verify."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 عضویت در کانال سیب",
                    url=channel_link,
                    style="primary",
                )
            ],
            [
                success_button(
                    text="✅ بررسی عضویت",
                    callback_data="check_membership",
                )
            ],
        ]
    )
    return keyboard


def get_private_welcome_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    """Main Apple Ecosystem keyboard for private chat."""
    add_group_url = f"https://t.me/{bot_username}?startgroup=true"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                primary_button(
                    text="🌳 باغ من",
                    callback_data="apple_garden",
                ),
                primary_button(
                    text="🎴 کارت‌های من",
                    callback_data="apple_cards",
                ),
            ],
            [
                primary_button(
                    text="🛒 فروشگاه",
                    callback_data="apple_shop",
                ),
                primary_button(
                    text="📚 ویکی کاراکترها",
                    callback_data="apple_wiki",
                ),
            ],
            [
                primary_button(
                    text="🔄 مبادله کارت",
                    callback_data="apple_trade",
                ),
            ],
            [
                primary_button(
                    text="🎁 صندوقچه گنج روزانه",
                    callback_data="apple_chest",
                ),
            ],
            [
                primary_button(
                    text="👤 پروفایل من",
                    callback_data="apple_profile",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ افزودن سیب به گروه",
                    url=add_group_url,
                    style="primary",
                )
            ],
            [
                success_button(
                    text="🎮 بازی‌ها و راهنما",
                    callback_data="help_menu",
                )
            ],
        ]
    )
    return keyboard


def get_group_welcome_keyboard() -> InlineKeyboardMarkup:
    """Main keyboard for group chat /start.

    Six entries: games, the guide, the shop, the player's profile, the group
    rating and the Daily Sib Treasure Chest (its own full-width row at the
    bottom, opening the chest in this same group). The Games entry leads the
    menu full-width.

    Card Trading, My Cards and Active Group Games are NOT duplicated here;
    each keeps its ONE canonical entry:

    - Card Trading  -> «👤 پروفایل من» (get_profile_keyboard)
    - My Cards      -> «👤 پروفایل من» (get_profile_keyboard)
    - Active Games  -> «🎮 بازی‌ها»    (get_games_menu_keyboard)

    Removing a shortcut here never removes the feature itself.
    """
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                success_button(
                    text="🎮 بازی‌ها",
                    callback_data="group_games_panel",
                )
            ],
            [
                primary_button(
                    text="📖 راهنما",
                    callback_data="help_menu",
                ),
                primary_button(
                    text="🛒 فروشگاه",
                    callback_data="apple_shop",
                ),
            ],
            [
                primary_button(
                    text="👤 پروفایل من",
                    callback_data="apple_profile",
                ),
                primary_button(
                    text="🏆 رتبه و آمار گروه",
                    callback_data="group_rating_stats",
                ),
            ],
            [
                primary_button(
                    text="🎁 صندوقچه گنج روزانه",
                    callback_data="group_chest",
                ),
            ],
        ]
    )
    return keyboard


def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    """Main keyboard for admin panel."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                primary_button("📊 داشبورد سیستم", "admin_dashboard"),
                primary_button("📢 تبلیغات و مانیتایزیشن", "admin_ads"),
            ],
            [
                success_button("🤝 کانال‌ها و گیت اسپانسر", "admin_channels"),
                primary_button("⚔️ وضعیت مسابقات", "admin_matches"),
            ],
            [
                primary_button("👤 مدیریت کاربران", "admin_users"),
            ],
        ]
    )
    return keyboard


def primary_button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        style="primary",
    )


def success_button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        style="success",
    )


def danger_button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        style="danger",
    )


def nav_back_row(text: str, callback_data: str) -> list:
    """Standard 'Back' navigation row (never cancels active matches).

    Back is a semantic RED action per the bot-wide button convention.
    """
    return [
        [
            InlineKeyboardButton(
                text=text,
                callback_data=callback_data,
                style="danger",
            )
        ]
    ]


def nav_refresh_row(text: str, callback_data: str) -> list:
    """Standard 'Refresh' row re-rendering from the authoritative state."""
    return [
        [
            InlineKeyboardButton(
                text=text,
                callback_data=callback_data,
                style="primary",
            )
        ]
    ]


BACK_TO_MAIN_LABEL = "↩️ بازگشت به منوی اصلی"


def get_cards_keyboard(
    origin: Optional[str] = None,
    card_rows: Optional[list] = None,
    page: int = 0,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    """'کارت‌های من' panel keyboard.

    ``card_rows`` is a list of (card_id, label) pairs shown on the current
    page (built by the handler from the player's cards); each opens the
    card's view screen (which carries the ⬆️ Card Upgrade action).
    """
    from sib.services.nav import back_label_for, back_target, with_origin

    keyboard_rows: list[list[InlineKeyboardButton]] = []

    row: list[InlineKeyboardButton] = []
    for card_id, label in card_rows or []:
        row.append(primary_button(text=label, callback_data=f"card_panel:{card_id}"))
        if len(row) == 2:
            keyboard_rows.append(row)
            row = []
    if row:
        keyboard_rows.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                primary_button(
                    text="▶️ قبلی",
                    callback_data=with_origin(f"apple_cards:{page - 1}", origin),
                )
            )
        if page + 1 < total_pages:
            nav.append(
                primary_button(
                    text="◀️ بعدی",
                    callback_data=with_origin(f"apple_cards:{page + 1}", origin),
                )
            )
        keyboard_rows.append(nav)

    keyboard_rows.append(
        [
            primary_button(
                text="📚 ویکی کاراکترها",
                callback_data=with_origin("apple_wiki", "apple_cards"),
            )
        ]
    )
    keyboard_rows.append([primary_button(text="🛒 فروشگاه", callback_data="apple_shop")])
    keyboard_rows.extend(nav_back_row(back_label_for(origin), back_target(origin)))
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def get_profile_keyboard(origin: Optional[str] = None) -> InlineKeyboardMarkup:
    """'پروفایل من' panel keyboard (contextual Back).

    The profile is the ONE home of card ownership features: My Cards and
    Card Trading live here (they are no longer duplicated in the group main
    menu), next to the garden and the shop.
    """
    from sib.services.nav import back_label_for, back_target, with_origin

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                primary_button(
                    text="🎴 کارت‌های من",
                    callback_data=with_origin("apple_cards", "apple_profile"),
                )
            ],
            [
                primary_button(
                    text="🔁 مبادله کارت",
                    callback_data=with_origin("apple_trade", "apple_profile"),
                )
            ],
            [
                primary_button(
                    text="🎨 تغییر پوسته",
                    callback_data="profile_skin",
                )
            ],
            [
                primary_button(
                    text="🌳 باغ من",
                    callback_data="apple_garden",
                )
            ],
            [
                primary_button(
                    text="🛒 فروشگاه",
                    callback_data="apple_shop",
                )
            ],
        ]
    )
    keyboard.inline_keyboard.extend(nav_back_row(back_label_for(origin), back_target(origin)))
    return keyboard


def get_profile_skin_keyboard(selected_skin) -> InlineKeyboardMarkup:
    """Skin picker with the active choice clearly marked and a profile Back."""
    from sib.services.profile_skin import (
        PROFILE_SKINS,
        normalize_profile_skin,
        profile_skin_label,
    )

    active_skin = normalize_profile_skin(selected_skin)
    rows: list[list[InlineKeyboardButton]] = []
    options = []
    for skin in PROFILE_SKINS:
        is_active = skin is active_skin
        label = profile_skin_label(skin)
        text = f"✅ {label} · فعال" if is_active else f"🍎 {label}"
        button = InlineKeyboardButton(
            text=text,
            callback_data=f"profile_skin_select:{skin.value}",
            style="success" if is_active else "primary",
        )
        options.append(button)

    for index in range(0, len(options), 2):
        rows.append(options[index : index + 2])

    rows.extend(nav_back_row("↩️ بازگشت به پروفایل", "apple_profile"))
    return InlineKeyboardMarkup(inline_keyboard=rows)
