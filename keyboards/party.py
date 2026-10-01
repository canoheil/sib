"""Inline keyboards for Games and Parties.

Navigation is a context-aware chain (task spec #1/#2):

    main menu -> games menu -> active games -> one game

and every screen has a working Back button to its parent. Back never
cancels an active match or party - cancellations have their own explicit
buttons.
"""

from typing import List, Optional
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
    """Screen: game mode selection (solo, 2v2, 4v4).

    Each mode leads to the MATCH TYPE screen (this game's mode + how to play
    it) - never to a second top-level game list.
    """
    buttons = []

    if game.supports_solo:
        buttons.append([
            primary_button(
                text="👤 انفرادی (Solo - ۱ به ۱)",
                callback_data=f"select_mode:{game.code}:solo:{back_to}",
            )
        ])
    if game.supports_2v2:
        buttons.append([
            primary_button(
                text="👥 تیمی ۲ به ۲ (2v2)",
                callback_data=f"select_mode:{game.code}:2v2:{back_to}",
            )
        ])
    if game.supports_4v4:
        buttons.append([
            primary_button(
                text="👥 تیمی ۴ به ۴ (4v4)",
                callback_data=f"select_mode:{game.code}:4v4:{back_to}",
            )
        ])

    buttons.extend(nav_back_row(BACK_TO_GAMES, back_to))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


MODE_TITLES = {
    "solo": "انفرادی (Solo - ۱ به ۱)",
    "2v2": "تیمی ۲ به ۲ (2v2)",
    "4v4": "تیمی ۴ به ۴ (4v4)",
}


def get_match_type_keyboard(
    game: Game, mode: str, back_to: str = "group_games_panel"
) -> InlineKeyboardMarkup:
    """Screen: MATCH TYPE for one game mode - never a top-level game split.

    «پیدا کردن حریف» is the existing ranked/competitive flow; «بازی با اعضای
    گروه» is the FRIENDLY match type of the very same mode.
    """
    buttons = [
        [
            success_button(
                text="🔎 پیدا کردن حریف",
                callback_data=f"create_party:{game.code}:{mode}",
            )
        ],
        [
            success_button(
                text="🎉 بازی با اعضای گروه (رفیقانه)",
                callback_data=f"create_friendly_party:{game.code}:{mode}",
            )
        ],
    ]
    buttons.extend(
        nav_back_row("↩️ بازگشت به حالت‌های بازی", f"select_game:{game.code}:{back_to}")
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


FRIENDLY_TEAM_JOIN_TITLES = {
    1: "➕ پیوستن به تیم 🍎",
    2: "➕ پیوستن به تیم 🍏",
}


def friendly_free_team_slots(party: Party) -> dict:
    """Free slots per team of a friendly party, from its loaded members.

    The party service eager-loads ``party.members`` (with their teams) for
    every render path, so the shared party card can derive the per-team
    free slots itself - no extra query and no dependency on who is viewing.
    Mirrors ``PartyService.free_slots`` (per-team cap = ``party.team_size``).
    """
    counts = {1: 0, 2: 0}
    for member in party.members or []:
        team = int(member.team or 1)
        if team in counts:
            counts[team] += 1
    try:
        team_size = max(1, int(party.team_size))
    except (TypeError, ValueError):
        # Defensive: if the team data cannot be read, keep offering both
        # teams - the service still enforces the real per-team cap.
        return {1: 1, 2: 1}
    return {team: max(0, team_size - counts[team]) for team in (1, 2)}


def friendly_join_row(party: Party) -> list:
    """Join buttons for every team of a friendly party that still has a slot.

    Once a team is full its Join button is not rendered at all (the party
    service enforces the same per-team cap on the callback itself).
    """
    free = friendly_free_team_slots(party)
    return [
        success_button(
            text=FRIENDLY_TEAM_JOIN_TITLES[team],
            callback_data=f"join_party:{party.id}:{team}",
        )
        for team in (1, 2)
        if free.get(team, 0) > 0
    ]


def get_shared_friendly_party_keyboard(party: Party) -> InlineKeyboardMarkup:
    """Keyboard of a friendly party on its ONE shared group message.

    A friendly party holds both teams inside a single Telegram group, so
    its card is a single message and Telegram renders exactly ONE keyboard
    for every viewer. The card is therefore built for the GROUP, never for
    one viewer's membership: it carries the Team 🍎/🍏 join buttons of
    every team with a free slot (so other members can join immediately -
    a creator-side render used to hide them until someone pressed
    Refresh), the member actions (leave, cancel, start when full) and the
    host's bot filling.

    Visibility is not authorization: which presser may actually use an
    action is still enforced server-side - the panel guard keeps
    ``friendly_fill_bots:``/``friendly_start:`` host-scoped and the party
    service re-checks host/member rights on every callback.
    """
    from sib.database.models import PartyStatus

    buttons = []

    if party.status == PartyStatus.IN_MATCH:
        buttons.extend(nav_refresh_row("🔄 بروزرسانی", f"view_party:{party.id}"))
        buttons.extend(nav_back_row(BACK_TO_ACTIVE, "group_active_games"))
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    if not party.is_full:
        join_row = friendly_join_row(party)
        if join_row:
            buttons.append(join_row)
        buttons.append([
            danger_button(
                text="🚪 ترک پارتی",
                callback_data=f"leave_party:{party.id}",
            )
        ])
        buttons.append([
            success_button(
                text="🤖 پر کردن جای خالی با بات",
                callback_data=f"friendly_fill_bots:{party.id}",
            )
        ])
    else:
        # Only members can start the friendly match (the service enforces it).
        buttons.append([
            success_button(
                text="🚀 شروع بازی رفیقانه",
                callback_data=f"friendly_start:{party.id}",
            )
        ])

    buttons.append([
        danger_button(
            text="❌ لغو پارتی",
            callback_data=f"cancel_party:{party.id}",
        )
    ])
    buttons.extend(nav_refresh_row("🔄 بروزرسانی", f"view_party:{party.id}"))
    buttons.extend(nav_back_row(BACK_TO_ACTIVE, "group_active_games"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_friendly_party_keyboard(
    party: Party,
    is_member: bool = False,
    is_host: Optional[bool] = None,
) -> InlineKeyboardMarkup:
    """Screen: one friendly party from ONE viewer's side.

    Actions: join a specific team / leave, bot filling (shown to the host;
    the service enforces host-only anyway), start the friendly match when
    full, cancel, refresh and Back to Active Games. Back NEVER cancels the
    party (cancellation has its own button).

    NOTE: the friendly party card lives on ONE shared group message, so the
    handlers render :func:`get_shared_friendly_party_keyboard` for it (via
    ``get_party_action_keyboard``) - a per-viewer keyboard cannot serve a
    shared message (the creator's render left everyone else without the
    join buttons). This per-viewer variant remains for direct/private use.
    """
    from sib.database.models import PartyStatus

    buttons = []

    if party.status == PartyStatus.IN_MATCH:
        buttons.extend(nav_refresh_row("🔄 بروزرسانی", f"view_party:{party.id}"))
        buttons.extend(nav_back_row(BACK_TO_ACTIVE, "group_active_games"))
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    if not party.is_full:
        if is_member:
            buttons.append([
                danger_button(
                    text="🚪 ترک پارتی",
                    callback_data=f"leave_party:{party.id}",
                )
            ])
        else:
            buttons.append([
                success_button(
                    text="➕ پیوستن به تیم 🍎",
                    callback_data=f"join_party:{party.id}:1",
                ),
                success_button(
                    text="➕ پیوستن به تیم 🍏",
                    callback_data=f"join_party:{party.id}:2",
                ),
            ])
        if is_member and is_host is not False:
            buttons.append([
                success_button(
                    text="🤖 پر کردن جای خالی با بات",
                    callback_data=f"friendly_fill_bots:{party.id}",
                )
            ])
    elif is_member:
        # Only members can start the friendly match (the service enforces it).
        buttons.append([
            success_button(
                text="🚀 شروع بازی رفیقانه",
                callback_data=f"friendly_start:{party.id}",
            )
        ])

    buttons.append([
        danger_button(
            text="❌ لغو پارتی",
            callback_data=f"cancel_party:{party.id}",
        )
    ])
    buttons.extend(nav_refresh_row("🔄 بروزرسانی", f"view_party:{party.id}"))
    buttons.extend(nav_back_row(BACK_TO_ACTIVE, "group_active_games"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_party_action_keyboard(
    party: Party, is_member: bool = False, is_host: Optional[bool] = None
) -> InlineKeyboardMarkup:
    """Screen: one party/game (child of the active-games screen).

    Queue and in-match parties are LIVE (they block a new game) but have
    no party actions left: the queue screen owns "cancel search" and the
    board owns the match. They still get Refresh + Back so the Active
    Games screen can always show and inspect every live game.

    A friendly party (match type, not a game mode) renders its own screen
    with team joining, bot filling and the friendly start action. Because
    that card lives on ONE shared group message, it is rendered for the
    whole group (:func:`get_shared_friendly_party_keyboard`) - rendering it
    from the creator's side used to leave every other member without the
    Team join buttons until someone pressed Refresh.
    """
    if party.is_friendly:
        return get_shared_friendly_party_keyboard(party)

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

    if party.is_full:
        # Every mode uses the same real-opponent queue with automatic bot fallback.
        buttons.append([
            success_button(
                text="🔎 پیدا کردن حریف",
                callback_data=f"find_match:{party.id}",
            )
        ])
        if party.mode == "solo":
            # Solo menu: the SAME party can become a friendly 1v1 with a group
            # member (which can be filled with a bot) - a match type, not a
            # separate game.
            buttons.append([
                success_button(
                    text="🎉 بازی با اعضای گروه (رفیقانه)",
                    callback_data=f"make_friendly:{party.id}",
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
    """Screen: active games of the group (child of the games menu).

    Lists every live party of the group plus the actions of this screen:
    create a new game, refresh and Back to the games menu. Apple Battle is
    NOT duplicated here - its ONE entry lives in the games menu
    (⚔️ نبرد سیبی).
    """
    buttons: list = []

    from sib.database.models import PartyStatus

    for party in parties:
        mode_text = "انفرادی" if party.mode == "solo" else f"تیمی {party.mode}"
        if party.is_friendly:
            mode_text += " رفیقانه"
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


def get_friendly_rematch_keyboard(match_id: int) -> InlineKeyboardMarkup:
    """Post-match actions of a FINISHED friendly match.

    Rematch recreates the same game/mode as a friendly match (never ranked);
    Back always returns to the group's active games without cancelling
    anything.
    """
    buttons = [
        [
            success_button(
                text="🔄 بازی مجدد رفیقانه",
                callback_data=f"friendly_rematch:{match_id}",
            )
        ],
    ]
    buttons.extend(nav_back_row(BACK_TO_ACTIVE, "group_active_games"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)
