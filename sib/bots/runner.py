"""Helpers for running bot turns during active matches."""
from typing import Any, Dict

from sib.bots.service import GameBotService


async def run_bot_turns(
    match,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Run bot turns until it becomes a human turn or the game finishes."""

    if not match.is_bot_match:
        return state

    while not state.get("finished", False):
        current_player_id = state.get("current_player_id")

        # Turn-based games.
        if current_player_id is not None:
            current_player = next(
                (
                    player
                    for player in match.players
                    if player.user_id == current_player_id
                ),
                None,
            )

            if not current_player or not current_player.user.is_bot:
                break

            state, _ = await GameBotService.play_turn(
                match=match,
                state=state,
            )
            continue

        # Simultaneous games such as RPS / High Card.
        bot_player = next(
            (
                player
                for player in match.players
                if player.user.is_bot
                and not state["players"][str(player.user_id)].get(
                    "locked", False
                )
            ),
            None,
        )

        if not bot_player:
            break

        state, _ = await GameBotService.play_turn(
            match=match,
            state=state,
            bot_player_id=bot_player.user_id,
        )

        # Simultaneous games need only one hidden bot choice per call.
        break

    return state
