"""Generic service for executing game bot moves."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Tuple

from sib.core.logging import log_event, Events
from sib.database.models import Match
from sib.games.registry import get_game_engine
from sib.bots.registry import get_bot_strategy


class GameBotService:
    """Executes bot moves through the normal game engine."""

    @staticmethod
    async def play_turn(
        match: Match,
        state: Dict[str, Any],
        bot_player_id: int | None = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Choose and apply one bot move.

        Returns:
            (updated_state, action)
        """
        if bot_player_id is None:
            bot_player_id = match.current_turn_player_id

        if bot_player_id is None:
            raise ValueError("Bot turn has no current player.")

        player = next(
            (p for p in match.players if p.user_id == bot_player_id),
            None,
        )

        if not player or not player.user.is_bot:
            raise ValueError(
                f"Current player {bot_player_id} is not a bot."
            )

        engine = get_game_engine(match.game.code)
        strategy = get_bot_strategy(match.game.code)

        action = strategy.choose_action(
            state=state,
            bot_player_id=bot_player_id,
        )

        is_valid, reason = engine.validate_action(
            state,
            bot_player_id,
            action,
        )

        if not is_valid:
            raise ValueError(
                f"Bot generated invalid action: {reason}"
            )

        state = engine.apply_action(
            state,
            bot_player_id,
            action,
        )

        match.game_state = json.dumps(
            state,
            ensure_ascii=False,
        )

        if engine.is_finished(state):
            match.current_turn_player_id = None
            match.turn_deadline = None
        else:
            current_player_id = state.get("current_player_id")

            match.current_turn_player_id = current_player_id

            if current_player_id:
                match.turn_deadline = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=engine.turn_timeout_seconds)
                )

        log_event(
            Events.MOVE_PLAYED,
            {
                "match_id": match.id,
                "user_id": bot_player_id,
                "action": action,
                "game": match.game.code,
                "is_bot": True,
            },
        )

        return state, action
