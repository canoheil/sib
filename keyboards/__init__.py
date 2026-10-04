"""Keyboards package."""

from sib.keyboards.common import (
    get_membership_keyboard,
    get_private_welcome_keyboard,
    get_group_welcome_keyboard,
    get_admin_main_keyboard,
)
from sib.keyboards.party import (
    get_game_selection_keyboard,
    get_game_mode_keyboard,
    get_party_action_keyboard,
    get_active_parties_keyboard,
)
from sib.keyboards.leaderboard import (
    get_leaderboard_keyboard,
    get_group_stats_keyboard,
    get_player_stats_keyboard,
)
from sib.keyboards.marriage import (
    get_marriage_proposal_keyboard,
    marriage_proposal_callback,
    parse_marriage_proposal_callback,
)

__all__ = [
    "get_membership_keyboard",
    "get_private_welcome_keyboard",
    "get_group_welcome_keyboard",
    "get_admin_main_keyboard",
    "get_game_selection_keyboard",
    "get_game_mode_keyboard",
    "get_party_action_keyboard",
    "get_active_parties_keyboard",
    "get_leaderboard_keyboard",
    "get_group_stats_keyboard",
    "get_player_stats_keyboard",
    "get_marriage_proposal_keyboard",
    "marriage_proposal_callback",
    "parse_marriage_proposal_callback",
]
