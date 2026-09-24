"""Base Game Engine abstraction for sib games."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardMarkup
from sib.database.models import MatchPlayer


@dataclass
class GameResult:
    """Standard game outcome result."""
    winner_team: Optional[int]  # 1 or 2, None if draw
    is_draw: bool
    summary: str  # Persian explanation of result
    details: Dict[str, Any]


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
