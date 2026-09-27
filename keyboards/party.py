"""Inline keyboards for Games and Parties.

Navigation is a context-aware chain (task spec #1/#2):

    main menu -> games menu -> active games -> one game

and every screen has a working Back button to its parent. Back never
cancels an active match or party - cancellations have their own explicit
buttons.
"""

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

BACK_TO_MAIN = "↩️ بازگشت به منوی اصلی"
BACK_TO_GAMES = "↩️ بازگشت به منوی بازی‌ها"
BACK_TO_ACTIVE = "↩️ بازگشت به بازی‌های فعال"

GAME_ICONS = {
    "high_card": "🃏",
    "rock_paper_scissors": "✂️",
    "tic_tac_toe": "⭕",
    "connect_four": "🔴",
    "battleship": "🚢",
}


def _game_grid(games: List[Game]) -> list:
    """Game selection grid (2 buttons per row, no navigation rows)."""
    keyboard = []
    row = []
    for game in games:
        icon = GAME_ICONS.get(game.code, "🎮")
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
    return keyboard


def get_game_selection_keyboard(
    games: List[Game],
    back_to: str = "group_games_panel",
    back_label: str = BACK_TO_GAMES,
) -> InlineKeyboardMarkup:
    """Game grid + a contextual Back row (used by 'create party' screens)."""
    keyboard = _game_grid(games)
    keyboard.extend(nav_back_row(back_label, back_to))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_games_menu_keyboard(
    games: List[Game], active_count: int = 0
) -> InlineKeyboardMarkup:
    """Screen: the games menu (parent of the active-games screen).

    Contains the DB game grid plus the extended game sections (Apple
    Battle, Cards) with the same navigation convention as every game:
    entered from here, Back returns here (spec #2).
    """
    from sib.services.nav import with_origin

    keyboard = _game_grid(games)
    keyboard.append(
        [
            InlineKeyboardButton(
                text="⚔️ نبرد سیبی",
                callback_data=with_origin("apple_battle", "group_games_panel"),
            ),
            InlineKeyboardButton(
                text="🃏 کارت‌ها",
                callback_data=with_origin("apple_cards", "group_games_panel"),
            ),
        ]
    )
    label = "🎮 بازی‌های فعال گروه"
    if active_count:
        label += f" ({active_count})"
    keyboard.append(
        [InlineKeyboardButton(text=label, callback_data="group_active_games")]
    )
    keyboard.extend(nav_back_row(BACK_TO_MAIN, "main_menu"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_game_mode_keyboard(
    game: Game, back_to: str = "group_games_panel"
) -> InlineKeyboardMarkup:
    """Screen: game mode selection (solo, 2v2, 4v4)."""
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

    buttons.extend(nav_back_row(BACK_TO_GAMES, back_to))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_party_action_keyboard(
    party: Party, is_member: bool = False
) -> InlineKeyboardMarkup:
    """Screen: one party/game (child of the active-games screen)."""
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
    buttons.extend(nav_back_row(BACK_TO_ACTIVE, "group_active_games"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_active_parties_keyboard(parties: List[Party]) -> InlineKeyboardMarkup:
    """Screen: active games of the group (child of the games menu)."""
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
        InlineKeyboardButton(
            text="🔄 بروزرسانی",
            callback_data="group_active_games",
        ),
    ])
    buttons.extend(nav_back_row(BACK_TO_GAMES, "group_games_panel"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)
