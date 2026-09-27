"""Inline keyboards for Games and Parties."""

from typing import List
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sib.database.models import Game, Party
from sib.keyboards.common import (
    primary_button,
    success_button,
    danger_button,
    nav_back_row,
    nav_refresh_row,
)


def get_game_selection_keyboard(games: List[Game]) -> InlineKeyboardMarkup:
    """Keyboard for selecting a game in group."""
    game_icons = {
        "high_card": "🃏",
        "rock_paper_scissors": "✂️",
        "tic_tac_toe": "⭕",
        "connect_four": "🔴",
        "battleship": "🚢",
    }
    
    keyboard = []
    row = []
    for game in games:
        icon = game_icons.get(game.code, "🎮")
        button = primary_button(
            text=f"{icon} {game.name_fa}",
            callback_data=f"select_game:{game.code}",
        )
        row.append(button)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.extend(nav_back_row("🔙 بازگشت به منوی بازی‌ها", "group_games_panel"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_game_mode_keyboard(game: Game) -> InlineKeyboardMarkup:
    """Keyboard for selecting game mode (solo, 2v2, 4v4)."""
    buttons = []
    
    if game.supports_solo:
        buttons.append([
            primary_button(
                text="👤 انفرادی (Solo - ۱ به ۱)",
                callback_data=f"create_party:{game.code}:solo",
            )
        ])
    if game.supports_2v2:
        buttons.append([
            primary_button(
                text="👥 تیمی ۲ به ۲ (2v2)",
                callback_data=f"create_party:{game.code}:2v2",
            )
        ])
    if game.supports_4v4:
        buttons.append([
            primary_button(
                text="👥 تیمی ۴ به ۴ (4v4)",
                callback_data=f"create_party:{game.code}:4v4",
            )
        ])

    buttons.extend(nav_back_row("🔙 بازگشت به منوی بازی‌ها", "group_games_panel"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_party_action_keyboard(party: Party, is_member: bool = False) -> InlineKeyboardMarkup:
    """Keyboard for party interaction (Join, Leave, Find Match, Cancel)."""
    buttons = []

    if party.is_full:
        # Full party actions
        if party.mode == "solo":
            buttons.append([
                success_button(
                    text="🤖 بازی با Bot",
                    callback_data=f"play_bot:{party.id}",
                )
            ])

        buttons.append([
            success_button(
                text="🔎 پیدا کردن حریف",
                callback_data=f"find_match:{party.id}",
            )
        ])
        buttons.append([
            danger_button(
                text="❌ لغو پارتی",
                callback_data=f"cancel_party:{party.id}",
            )
        ])
    else:
        # Open party with slots
        row = []
        if not is_member:
            row.append(
                success_button(
                    text="➕ پیوستن به تیم",
                    callback_data=f"join_party:{party.id}",
                )
            )
        else:
            row.append(
                danger_button(
                    text="🚪 ترک پارتی",
                    callback_data=f"leave_party:{party.id}",
                )
            )
        row.append(
            danger_button(
                text="❌ لغو پارتی",
                callback_data=f"cancel_party:{party.id}",
            )
        )
        buttons.append(row)

    buttons.extend(nav_refresh_row("🔄 بروزرسانی", f"view_party:{party.id}"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_active_parties_keyboard(parties: List[Party]) -> InlineKeyboardMarkup:
    """Keyboard displaying list of active parties in group."""
    buttons = []
    
    for party in parties:
        mode_text = "انفرادی" if party.mode == "solo" else f"تیمی {party.mode}"
        status_icon = "✅" if party.is_full else "⏳"
        label = f"{status_icon} {party.game.name_fa} ({mode_text}) — {party.current_players_count}/{party.max_players}"
        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"view_party:{party.id}",
            )
        ])

    buttons.append([
        primary_button(
            text="➕ ساخت بازی جدید",
            callback_data="create_new_party_menu",
        ),
    ])
    buttons.extend(nav_refresh_row("🔄 بروزرسانی", "group_games_panel"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)
