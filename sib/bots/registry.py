"""Bot strategy registry."""

from typing import Dict, Any

from sib.bots.strategies.tic_tac_toe import TicTacToeBotStrategy
from sib.bots.strategies.connect_four import ConnectFourBotStrategy


BOT_STRATEGIES: Dict[str, Any] = {
    "tic_tac_toe": TicTacToeBotStrategy(),
    "connect_four": ConnectFourBotStrategy(),
}


def get_bot_strategy(game_code: str):
    """Return the bot strategy for a game."""
    strategy = BOT_STRATEGIES.get(game_code)

    if not strategy:
        raise ValueError(
            f"Bot strategy for '{game_code}' is not registered."
        )

    return strategy


def register_bot_strategy(game_code: str, strategy: Any) -> None:
    """Register a bot strategy."""
    BOT_STRATEGIES[game_code] = strategy
