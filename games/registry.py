"""Game Engine Registry."""

from typing import Dict
from goje.games.base import BaseGameEngine
from goje.games.rock_paper_scissors.engine import RockPaperScissorsEngine
from goje.games.high_card.engine import HighCardEngine
from goje.games.tic_tac_toe.engine import TicTacToeEngine
from goje.games.connect_four.engine import ConnectFourEngine
from goje.games.battleship.engine import BattleshipEngine

_GAME_ENGINES: Dict[str, BaseGameEngine] = {
    "rock_paper_scissors": RockPaperScissorsEngine(),
    "high_card": HighCardEngine(),
    "tic_tac_toe": TicTacToeEngine(),
    "connect_four": ConnectFourEngine(),
    "battleship": BattleshipEngine(),
}


def get_game_engine(game_code: str) -> BaseGameEngine:
    """Retrieve the game engine for a given code."""
    engine = _GAME_ENGINES.get(game_code)
    if not engine:
        raise ValueError(f"Game engine for '{game_code}' is not registered.")
    return engine


def register_game_engine(game_code: str, engine: BaseGameEngine) -> None:
    """Register a new game engine."""
    _GAME_ENGINES[game_code] = engine
