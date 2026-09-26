"""Inline keyboards for Leaderboard and Statistics."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_leaderboard_keyboard(is_group: bool = True) -> InlineKeyboardMarkup:
    """Keyboard shown under global leaderboard."""
    buttons = []
    row = [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="show_leaderboard")]
    if is_group:
        row.append(InlineKeyboardButton(text="📊 آمار گروه ما", callback_data="group_win_stats"))
    buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 بستن", callback_data="close_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_group_stats_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown under group statistics."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏆 برترین گروه‌ها", callback_data="show_leaderboard"),
                InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="group_win_stats"),
            ],
            [
                InlineKeyboardButton(text="🔙 بستن", callback_data="close_panel"),
            ]
        ]
    )


def get_player_stats_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown under personal player statistics."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏆 برترین گروه‌ها", callback_data="show_leaderboard"),
            ]
        ]
    )
