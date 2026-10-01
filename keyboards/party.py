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


# Games with a dedicated canonical entry point (not shown in generic grids).
HIDDEN_GAME_CODES = {"apple_battle"}


def _game_grid(games: List[Game]) -> list:
    """Game selection grid (2 buttons per row, no navigation rows)."""
    keyboard = []
    row = []
    for game in games:
        if game.code in HIDDEN_GAME_CODES:
            continue
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

    Apple Battle (نبرد سیبی) is the flagship game: it comes FIRST and
    uses the green (success) theme to stand out from the blue game grid.
    """
    from sib.services.nav import with_origin

    keyboard: list = [
        [
            success_button(
                text="⚔️ نبرد سیبی",
                callback_data=with_origin("apple_battle", "group_games_panel"),
            )
        ]
    ]
    keyboard.extend(_game_grid(games))
    keyboard.append(
        [
            primary_button(
                text="🃏 کارت‌ها",
                callback_data=with_origin("apple_cards", "group_games_panel"),
            )
        ]
    )
    label = "🎮 بازی‌های فعال گروه"
    if active_count:
        label += f" ({active_count})"
    keyboard.append([primary_button(text=label, callback_data="group_active_games")])
    keyboard.extend(nav_back_row(BACK_TO_MAIN, "main_menu"))
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_game_mode_keyboard(
    game: Game, back_to: str = "group_games_panel"
) -> InlineKeyboardMarkup:
    """Screen: choose a supported mode before choosing match type."""
    buttons = []

    if game.supports_solo:
        buttons.append([
            primary_button(
                text="👤 انفرادی (Solo - ۱ به ۱)",
                callback_data=f"select_match_type:{game.code}:solo:{back_to}",
            )
        ])
    if game.supports_2v2:
        buttons.append([
            primary_button(
                text="👥 تیمی ۲ به ۲ (2v2)",
                callback_data=f"select_match_type:{game.code}:2v2:{back_to}",
            )
        ])
    if game.supports_4v4:
        buttons.append([
            primary_button(
                text="👥 تیمی ۴ به ۴ (4v4)",
                callback_data=f"select_match_type:{game.code}:4v4:{back_to}",
            )
        ])

    buttons.extend(nav_back_row(BACK_TO_GAMES, back_to))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_match_type_keyboard(
    game: Game, mode: str, back_to: str = "group_games_panel"
) -> InlineKeyboardMarkup:
    """Screen after mode selection: Competitive or Friendly."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                success_button(
                    text="🏆 رقابتی با گروه‌های دیگه",
                    callback_data=f"create_party:{game.code}:{mode}:competitive",
                )
            ],
            [
                primary_button(
                    text="🤝 دوستانه داخل همین گروه",
                    callback_data=f"create_party:{game.code}:{mode}:friendly",
                )
            ],
            nav_back_row(
                "↩️ بازگشت به انتخاب حالت",
                f"select_game:{game.code}:{back_to}",
            )[0],
        ]
    )


def get_party_action_keyboard(
    party: Party,
    is_member: bool = False,
    is_creator: bool = False,
) -> InlineKeyboardMarkup:
    """Screen for an active party; Back never changes its lifecycle."""
    buttons = []
    from sib.database.models import PartyStatus

    if party.status == PartyStatus.IN_QUEUE:
        if is_member:
            buttons.append([
                danger_button(
                    text="🚫 لغو جستجوی حریف",
                    callback_data=f"cancel_matchmaking:{party.id}",
                )
            ])
        buttons.extend(nav_refresh_row("🔄 بروزرسانی", f"view_party:{party.id}"))
        buttons.extend(nav_back_row(BACK_TO_ACTIVE, "group_active_games"))
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    if party.status == PartyStatus.IN_MATCH:
        buttons.extend(nav_refresh_row("🔄 بروزرسانی", f"view_party:{party.id}"))
        buttons.extend(nav_back_row(BACK_TO_ACTIVE, "group_active_games"))
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    if party.match_type == "FRIENDLY":
        team_capacity = {"solo": 1, "2v2": 2, "4v4": 4}.get(party.mode, 1)
        team_counts = {
            team: sum(1 for member in party.members if member.team == team)
            for team in (1, 2)
        }
        if party.is_full:
            if is_member:
                buttons.append([
                    success_button(
                        text="🎮 شروع بازی دوستانه",
                        callback_data=f"start_friendly:{party.id}",
                    )
                ])
            if is_creator:
                buttons.append([
                    danger_button(
                        text="❌ لغو پارتی",
                        callback_data=f"cancel_party:{party.id}",
                    )
                ])
        else:
            if not is_member:
                for team in (1, 2):
                    remaining = team_capacity - team_counts[team]
                    if remaining > 0:
                        team_label = "۱" if team == 1 else "۲"
                        buttons.append([
                            success_button(
                                text=(
                                    f"➕ پیوستن به تیم {team_label} "
                                    f"({team_counts[team]}/{team_capacity})"
                                ),
                                callback_data=f"join_party:{party.id}:{team}",
                            )
                        ])
            else:
                buttons.append([
                    danger_button(
                        text="🚪 ترک پارتی",
                        callback_data=f"leave_party:{party.id}",
                    )
                ])
            if is_creator:
                if is_member and party.game.code != "apple_battle":
                    buttons.append([
                        primary_button(
                            text="🤖 پر کردن جای خالی با بات‌های سیب",
                            callback_data=f"fill_friendly_bots:{party.id}",
                        )
                    ])
                buttons.append([
                    danger_button(
                        text="❌ لغو پارتی",
                        callback_data=f"cancel_party:{party.id}",
                    )
                ])
    elif party.is_full:
        # Competitive matchmaking remains unchanged.
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
    """Screen: active games of the group (child of the games menu).

    Apple Battle is always available: it leads the list with its green
    theme, above the group's waiting parties.
    """
    from sib.services.nav import with_origin

    buttons: list = [
        [
            success_button(
                text="⚔️ نبرد سیبی",
                callback_data=with_origin("apple_battle", "group_active_games"),
            )
        ]
    ]

    from sib.database.models import PartyStatus

    for party in parties:
        mode_text = "انفرادی" if party.mode == "solo" else f"تیمی {party.mode}"
        counter = f"{party.current_players_count}/{party.max_players}"
        # Every LIVE state is visible: a party that blocks a new game may
        # never be missing from this list ("ghost active game" fix).
        if party.status == PartyStatus.IN_QUEUE:
            label = f"🔍 {party.game.name_fa} ({mode_text}) — در جستجوی حریف"
        elif party.status == PartyStatus.IN_MATCH:
            label = f"⚔️ {party.game.name_fa} ({mode_text}) — در حال مسابقه"
        else:
            status_icon = "✅" if party.is_full else "⏳"
            label = f"{status_icon} {party.game.name_fa} ({mode_text}) — {counter}"
        buttons.append([
            primary_button(
                text=label,
                callback_data=f"view_party:{party.id}",
            )
        ])

    buttons.append([
        primary_button(
            text="➕ ساخت بازی جدید",
            callback_data="create_new_party_menu",
        ),
        primary_button(
            text="🔄 بروزرسانی",
            callback_data="group_active_games",
        ),
    ])
    buttons.extend(nav_back_row(BACK_TO_GAMES, "group_games_panel"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)
