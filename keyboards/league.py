"""Inline keyboards for the weekly group league."""

from aiogram.types import InlineKeyboardMarkup

from sib.keyboards.common import danger_button, primary_button


def get_league_keyboard(
    *, is_group: bool = True, enabled: bool = True
) -> InlineKeyboardMarkup:
    """League panel: refresh, a way back to the all-time leaderboard, main menu."""
    rows = []
    if enabled:
        rows.append([primary_button(text="🔄 بروزرسانی", callback_data="league_open")])
    rows.append(
        [primary_button(text="🏆 برترین گروه‌ها", callback_data="show_leaderboard")]
    )
    rows.append(
        [danger_button(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


LEAGUE_BUTTON_TEXT = "🏆 لیگ هفته"
