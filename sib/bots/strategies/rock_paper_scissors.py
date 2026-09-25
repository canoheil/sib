"""Bot strategy for Rock Paper Scissors."""
import random
from typing import Any, Dict


class RockPaperScissorsBotStrategy:
    game_code = "rock_paper_scissors"

    def choose_action(
        self,
        state: Dict[str, Any],
        bot_player_id: int,
        difficulty: str = "normal",
    ) -> Dict[str, Any]:
        return {
            "choice": random.choice(
                ["rock", "paper", "scissors"]
            )
        }
