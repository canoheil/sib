"""Bot strategy registry."""

from typing import Dict, Any

from sib.bots.strategies.tic_tac_toe import TicTacToeBotStrategy
from sib.bots.strategies.connect_four import ConnectFourBotStrategy
from sib.bots.strategies.rock_paper_scissors import RockPaperScissorsBotStrategy
from sib.bots.strategies.high_card import HighCardBotStrategy
from sib.bots.strategies.battleship import BattleshipBotStrategy


BOT_STRATEGIES: Dict[str, Any] = {
    "tic_tac_toe": TicTacToeBotStrategy(),
    "connect_four": ConnectFourBotStrategy(),
    "rock_paper_scissors": RockPaperScissorsBotStrategy(),
    "high_card": HighCardBotStrategy(),
    "battleship": BattleshipBotStrategy(),
}


def get_bot_strategy(game_code: str, strategy_key: str | None = None):
    """Return a strategy by registered key, defaulting to the game strategy.

    ``strategy_key`` is stored per bot participant, allowing future bot types
    to select an independent policy without changing Match or game engines.
    """
    strategy = BOT_STRATEGIES.get(strategy_key or game_code)

    if not strategy:
        raise ValueError(
            f"Bot strategy for '{game_code}' is not registered."
        )

    return strategy


def register_bot_strategy(game_code: str, strategy: Any) -> None:
    """Register a bot strategy."""
    BOT_STRATEGIES[game_code] = strategy
