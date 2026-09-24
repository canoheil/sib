"""Games package."""

from sib.games.base import BaseGameEngine, GameResult
from sib.games.registry import get_game_engine, register_game_engine
from sib.games.rock_paper_scissors.engine import RockPaperScissorsEngine

__all__ = [
    "BaseGameEngine",
    "GameResult",
    "get_game_engine",
    "register_game_engine",
    "RockPaperScissorsEngine",
]
