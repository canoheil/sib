"""Inline keyboards for common bot interactions."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_membership_keyboard(channel_link: str) -> InlineKeyboardMarkup:
    """Keyboard prompting user to join official channel and verify."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 عضویت در کانال سیب",
                    url=channel_link,
                )
            ],
            [
                InlineKeyboardButton(
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
                InlineKeyboardButton(
                    text="🌳 باغ من",
                    callback_data="apple_garden",
                ),
                InlineKeyboardButton(
                    text="🎴 کارت‌های من",
                    callback_data="apple_cards",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🛒 فروشگاه",
                    callback_data="apple_shop",
                ),
                InlineKeyboardButton(
                    text="📚 ویکی کاراکترها",
                    callback_data="apple_wiki",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚔️ نبرد سیبی",
                    callback_data="apple_battle",
                ),
                InlineKeyboardButton(
                    text="🔄 مبادله کارت",
                    callback_data="apple_trade",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👤 پروفایل من",
                    callback_data="apple_profile",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ افزودن سیب به گروه",
                    url=add_group_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎮 بازی‌ها و راهنما",
                    callback_data="help_menu",
                )
            ],
        ]
    )
    return keyboard


def get_group_welcome_keyboard() -> InlineKeyboardMarkup:
    """Main keyboard for group chat /start."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎮 بازی‌های فعال گروه",
                    callback_data="group_games_panel",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏆 رتبه و آمار گروه",
                    callback_data="group_rating_stats",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎴 کارت‌های من",
                    callback_data="apple_cards",
                ),
                InlineKeyboardButton(
                    text="🛒 فروشگاه",
                    callback_data="apple_shop",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚔️ نبرد سیبی",
                    callback_data="apple_battle",
                ),
                InlineKeyboardButton(
                    text="👤 پروفایل من",
                    callback_data="apple_profile",
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
