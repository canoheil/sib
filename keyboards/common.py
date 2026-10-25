"""Inline keyboards for common bot interactions.

Semantic glass-button system (Bot API 10.3 styles):
- success (green)  -> positive/confirm actions (buy, harvest, join, find)
- danger (red)     -> back / cancel / destructive actions
- primary (blue)   -> navigation / informational actions, playable cells

Every user-facing button carries one of the three colors. ``style=None`` is
NOT a design choice here: it is reserved for the legacy-client fallback path
in ``services.telegram_ui`` (old aiogram/Bot API builds that reject the
``style`` field), where the color is carried by the label instead.

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


def get_private_start_keyboard(
    bot_username: str, gate_channel_link: Optional[str] = None
) -> InlineKeyboardMarkup:
    """The ONE private menu: /start's home screen AND the «🏠 منوی اصلی» target.

    There used to be two private menus — this real one and an older
    ``get_private_welcome_keyboard`` that every back button jumped to. The
    older one listed the wiki and card trading at the top level, labelled its
    guide entry «🎮 بازی‌ها و راهنما» while opening the guide, and silently
    disagreed with /start, so pressing «🏠 منوی اصلی» inside the guide dropped
    the player into a menu the game no longer has anywhere else. That builder
    is gone: every private entry point renders THIS keyboard, so «back» lands
    on the screen /start shows.

    ``gate_channel_link`` is the only variant, and only the /start flow passes
    it: navigation never spends a chat-member API call to decide what the menu
    looks like.

    The wiki is reachable from «🎴 کارت‌های من» and card trading from
    «👤 پروفایل من», so nothing was orphaned by removing them from the top
    level.

    Replaces the Phase A3 "exactly one button" funnel. That funnel showed a
    brand-new player exactly one action — «افزودن سیب به گروه» — which is a
    dead end for anybody who is not an admin of a group: they had no card, no
    apples and no screen to open. The main menu worked but only behind the
    «سیب منو» phrase, which nobody types.

    Now /start opens the loop a solo player can actually play today:

    * «🎁 صندوق روزانه» — a free reward in the first session;
    * «🌳 باغ من» / «🛒 فروشگاه» / «🎴 کارت‌های من» — the base-building loop;
    * «👤 پروفایل من» / «📖 راهنما» — identity and help;
    * «➕ افزودن سیب به گروه» — LAST: still the goal, no longer the wall.

    ``gate_channel_link`` appends the official-channel row when the optional
    membership gate is on and the player has not joined yet (soft gate: it
    never blocks the menu).
    """
    add_group_url = f"https://t.me/{bot_username}?startgroup=true"
    rows: list[list[InlineKeyboardButton]] = [
        [
            success_button(text="🎁 صندوق روزانه", callback_data="apple_chest"),
            primary_button(text="🌳 باغ من", callback_data="apple_garden"),
        ],
        [
            primary_button(text="🎴 کارت‌های من", callback_data="apple_cards"),
            primary_button(text="🛒 فروشگاه", callback_data="apple_shop"),
        ],
        [
            primary_button(text="👤 پروفایل من", callback_data="apple_profile"),
            primary_button(text="📖 راهنما", callback_data="help_menu"),
        ],
    ]
    if gate_channel_link:
        # Soft gate row: join, then verify. «بررسی» removes the row; it never
        # unlocks anything, because the menu above is already usable.
        rows.append(
            [
                InlineKeyboardButton(
                    text="📢 عضویت در کانال سیب",
                    url=gate_channel_link,
                    style="primary",
                ),
                success_button(text="✅ بررسی", callback_data="check_membership"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ افزودن سیب به گروه",
                url=add_group_url,
                style="primary",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
                primary_button("📢 پیام همگانی", "admin_broadcast"),
            ],
        ]
    )
    return keyboard


def styled_button(
    text: str,
    callback_data: str,
    style: Optional[str] = None,
) -> InlineKeyboardButton:
    """Inline button with an explicit glass style.

    ``style`` is "primary" (blue), "success" (green) or "danger" (red).
    ``None`` keeps Telegram's app-specific default rendering and is reserved
    for the legacy-client fallback path in ``services.telegram_ui``.
    """
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        style=style,
    )


def keyboard_from_specs(
    rows,
    *,
    default_style: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Build a keyboard from ``(text, callback_data[, style])`` row specs.

    Tuple-based panels declare the color next to the label, so their buttons
    keep exactly one style source (``keyboards.common``) instead of building
    raw ``InlineKeyboardButton`` objects of their own. ``default_style`` is
    the color of a spec that omits one (``None`` keeps Telegram's default
    rendering and is reserved for the legacy-client fallback path).
    """
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for row in rows:
        buttons: list[InlineKeyboardButton] = []
        for spec in row:
            text, callback_data = spec[0], spec[1]
            style = spec[2] if len(spec) > 2 else default_style
            buttons.append(styled_button(text=text, callback_data=callback_data, style=style))
        keyboard_rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


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
                    text="◀️ قبلی",
                    callback_data=with_origin(f"apple_cards:{page - 1}", origin),
                )
            )
        if page + 1 < total_pages:
            nav.append(
                primary_button(
                    text="▶️ بعدی",
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


def get_profile_keyboard(
    origin: Optional[str] = None, *, reminders_on: Optional[bool] = None
) -> InlineKeyboardMarkup:
    """'پروفایل من' panel keyboard (contextual Back).

    ``reminders_on`` adds the «🔔/🔕 یادآوری‌ها» switch with the player's
    CURRENT state in the label. ``None`` omits the row entirely, so every
    call site that has no reminder state (and every test that predates the
    switch) keeps the previous keyboard.

    The profile is the ONE home of card ownership features: My Cards and
    Card Trading live here (they are no longer duplicated in the group main
    menu), next to the garden and the shop.
    """
    from sib.services.nav import back_label_for, back_target, with_origin

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                primary_button(
                    text="👥 لیست دوستان",
                    callback_data="profile:friends",
                )
            ],
            [
                primary_button(
                    text="🎁 دعوت دوستان",
                    callback_data="referral_invite",
                )
            ],
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
    if reminders_on is not None:
        label = "🔔 یادآوری‌ها: روشن" if reminders_on else "🔕 یادآوری‌ها: خاموش"
        keyboard.inline_keyboard.append(
            [
                success_button(label, "reminders_toggle")
                if reminders_on
                else primary_button(label, "reminders_toggle")
            ]
        )
    keyboard.inline_keyboard.extend(nav_back_row(back_label_for(origin), back_target(origin)))
    return keyboard


def get_referral_invite_keyboard(
    bot_username: str, user_id: int
) -> InlineKeyboardMarkup:
    """«🎁 دعوت دوستان» panel (REF1): share the personal deep link.

    The share button uses Telegram's ``t.me/share/url`` sheet, so it works
    even with the bot's inline mode disabled; the raw link itself is always
    in the message text for manual copying. Back returns to the profile.
    """
    from sib.services.referral import ReferralService

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🔗 اشتراک‌گذاری لینک",
                url=ReferralService.build_share_url(bot_username, user_id),
                style="primary",
            )
        ]
    ]
    rows.extend(nav_back_row("↩️ بازگشت به پروفایل", "apple_profile"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
