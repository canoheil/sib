"""Bot strategy for High Card."""

import random
from typing import Any, Dict


class HighCardBotStrategy:
    """Strategy for High Card.

    The game is simultaneous and luck-based: the bot simply draws a random
    card. The engine already assigns a random rank/suit when the action does
    not provide them, but the strategy returns explicit values so the bot
    move is self-describing and testable.
    """

    game_code = "high_card"

    SUITS = ["♠️", "♥️", "♦️", "♣️"]

    def choose_action(
        self,
        state: Dict[str, Any],
        bot_player_id: int,
        difficulty: str = "normal",
    ) -> Dict[str, Any]:
        return {
            "rank": random.randint(2, 14),
            "suit": random.choice(self.SUITS),
        }
