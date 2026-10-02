"""Bot strategy for High Card."""

import random
from typing import Any, Dict


class HighCardBotStrategy:
    """Strategy for High Card (Swap or Keep mechanic).

    The bot draws a card, then decides to keep or swap based on rank:
    - Keep cards 10+ (good cards)
    - Swap cards 9- (try to improve)

    This creates a simple but meaningful decision that the player can
    reason about ("the bot probably kept a high card").
    """

    game_code = "high_card"

    SWAP_THRESHOLD = 9  # Swap if card is 9 or lower

    def choose_action(
        self,
        state: Dict[str, Any],
        bot_player_id: int,
        difficulty: str = "normal",
    ) -> Dict[str, Any]:
        bot_data = state.get("players", {}).get(str(bot_player_id), {})

        # If bot hasn't drawn a card yet, draw one
        if bot_data.get("card_rank") is None:
            return {"draw": True}

        # Bot has a card, decide keep or swap
        rank = bot_data["card_rank"]

        if rank >= 10:
            # Good card, keep it
            return {"choice": "keep"}
        else:
            # Low card, swap it
            return {"choice": "swap"}
