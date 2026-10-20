"""Inline keyboards for Leaderboard and Statistics."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.keyboards.common import danger_button, primary_button


def get_leaderboard_keyboard(is_group: bool = True) -> InlineKeyboardMarkup:
    """Main leaderboard menu — 4 tabs, then back/refresh.

    Uses semantic colors: tabs are primary/blue, back is danger/red.
    """
    buttons = [
        [
            primary_button(text="🏰 برترین قلعه‌ها", callback_data="lb:castles"),
            primary_button(text="⚔️ برترین جنگاوران", callback_data="lb:warriors"),
        ],
        [
            primary_button(text="🛡 برترین مدافعان", callback_data="lb:defenders"),
            primary_button(text="🍎 برترین اهداکنندگان", callback_data="lb:donors"),
        ],
    ]
    # Refresh row + optional group stats
    refresh_row = [primary_button(text="🔄 بروزرسانی", callback_data="show_leaderboard")]
    if is_group:
        refresh_row.append(primary_button(text="📊 آمار گروه ما", callback_data="group_win_stats"))
    buttons.append(refresh_row)
    buttons.append(
        [danger_button(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_leaderboard_tab_keyboard(tab: str, is_group: bool = True) -> InlineKeyboardMarkup:
    """Keyboard shown inside a tab — back + refresh.

    tab values: castles, warriors, defenders, donors
    """
    # Row with refresh for the current tab
    buttons = [
        [primary_button(text="🔄 بروزرسانی", callback_data=f"lb:{tab}")],
        [danger_button(text="🔙 بازگشت", callback_data="show_leaderboard")],
        [danger_button(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")],
    ]
    # Keep is_group param for future extension; not used for now
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_group_stats_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown under group statistics."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                primary_button(text="🏆 برترین گروه‌ها", callback_data="show_leaderboard"),
                primary_button(text="🔄 بروزرسانی", callback_data="group_win_stats"),
            ],
            [
                danger_button(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu"),
            ]
        ]
    )


def get_player_stats_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown under personal player statistics."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                primary_button(text="🏆 برترین گروه‌ها", callback_data="show_leaderboard"),
            ]
        ]
    )
