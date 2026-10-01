"""Base Game Engine abstraction for sib games."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardMarkup
from sib.database.models import MatchPlayer, MatchType


@dataclass
class GameResult:
    """Standard game outcome result."""
    winner_team: Optional[int]  # 1 or 2, None if draw
    is_draw: bool
    summary: str  # Persian explanation of result
    details: Dict[str, Any]


def turn_line(
    game_state: Dict[str, Any],
    for_group_id: Optional[int],
    for_player_id: Optional[int],
    your_verb: str,
    name_verb: str,
) -> str:
    """Honest turn indicator for a possibly-shared group message.

    The phrase "نوبت شماست" is only used when the message audience is
    exactly the current player (solo/private boards). In team modes the
    message is shared, so the current player is always NAMED - never
    claim "your turn" to someone who cannot act.
    """
    current_pid = game_state.get("current_player_id")
    players = game_state.get("players", {})
    curr = players.get(str(current_pid), {}) if current_pid is not None else {}
    curr_name = curr.get("name", "بازیکن")
    members = [
        p for p in players.values() if p.get("group_id") == for_group_id
    ] if for_group_id is not None else []
    if current_pid is not None and current_pid == for_player_id and len(members) <= 1:
        return f"🟢 **نوبت شماست!** {your_verb}"
    return f"🟢 **نوبت {curr_name} است!** {name_verb}"


def is_friendly_state(game_state: Dict[str, Any]) -> bool:
    """Whether an engine state is a same-group Friendly match."""
    return game_state.get("match_type") == MatchType.FRIENDLY


def player_for_view(
    game_state: Dict[str, Any],
    for_group_id: Optional[int],
    for_player_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Pick the viewer's player state without confusing same-group teams."""
    players = game_state.get("players", {})
    if is_friendly_state(game_state) and for_player_id is not None:
        return players.get(str(for_player_id))
    return next(
        (p for p in players.values() if p.get("group_id") == for_group_id),
        None,
    )


def opponent_for_view(
    game_state: Dict[str, Any],
    viewer: Optional[Dict[str, Any]],
    for_group_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Select the opposing side in a public Friendly view or private PvP view."""
    if is_friendly_state(game_state):
        if viewer is None:
            return None
        return next(
            (p for p in game_state.get("players", {}).values() if p.get("team") != viewer.get("team")),
            None,
        )
    return next(
        (p for p in game_state.get("players", {}).values() if p.get("group_id") != for_group_id),
        None,
    )


def team_label(game_state: Dict[str, Any], team: Any) -> str:
    """Return the configured team name, with a Friendly-safe fallback."""
    key = str(team)
    configured = game_state.get("team_names", {}).get(key)
    if configured:
        return configured
    if is_friendly_state(game_state):
        return "تیم اول" if key in ("1", "A") else "تیم دوم"
    return "تیم A" if key in ("1", "A") else "تیم B"


class BaseGameEngine(ABC):
    """Abstract base class for all sib game engines."""

    game_code: str
    name_fa: str
    supports_solo: bool = True
    supports_2v2: bool = False
    supports_4v4: bool = False
    turn_timeout_seconds: int = 60

    @abstractmethod
    def create_initial_state(self, players: List[MatchPlayer], mode: str) -> Dict[str, Any]:
        """Generate serialized initial game state dictionary."""
        pass

    @abstractmethod
    def validate_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """Validate if action is legal. Returns (is_valid, error_reason)."""
        pass

    @abstractmethod
    def apply_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply player action and return updated state."""
        pass

    @abstractmethod
    def is_finished(self, game_state: Dict[str, Any]) -> bool:
        """Check if game has reached a terminal state."""
        pass

    @abstractmethod
    def get_result(self, game_state: Dict[str, Any]) -> GameResult:
        """Calculate winner or draw."""
        pass

    @abstractmethod
    def render_for_group(
        self,
        game_state: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int] = None,
    ) -> str:
        """Render board/state message in Persian for a specific group."""
        pass

    @abstractmethod
    def get_keyboard(
        self,
        game_state: Dict[str, Any],
        match_id: int,
        for_player_id: int,
    ) -> InlineKeyboardMarkup:
        """Generate inline keyboard for gameplay actions."""
        pass

    async def after_start(self, bot: Any, match_id: int) -> None:
        """Optional hook invoked ONCE when both sides have accepted a match.

        Turn-based engines never need this. Auto-play engines (e.g. Apple
        Battle) may schedule their own background presentation loop here.
        Default implementation is a no-op.
        """
        return None
