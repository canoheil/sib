"""Helpers for running bot turns during active matches."""

from typing import Any, Dict

from sib.bots.service import GameBotService


async def run_bot_turns(
    match,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Run bot turns until it becomes a human turn or the game finishes."""

    while not match.is_bot_match:
        return state

    while not state.get("finished", False):
        current_player_id = state.get("current_player_id")

        if current_player_id is None:
            break

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

    return state
