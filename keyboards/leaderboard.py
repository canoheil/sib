"""Inline keyboards for Leaderboard and Statistics."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.keyboards.common import danger_button, primary_button


def get_leaderboard_keyboard(is_group: bool = True) -> InlineKeyboardMarkup:
    """Keyboard shown under global leaderboard."""
    buttons = []
    row = [primary_button(text="🔄 بروزرسانی", callback_data="show_leaderboard")]
    if is_group:
        row.append(primary_button(text="📊 آمار گروه ما", callback_data="group_win_stats"))
    buttons.append(row)
    buttons.append(
        [danger_button(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")]
    )
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
