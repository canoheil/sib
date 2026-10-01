"""Helpers for running bot turns during active matches."""

import asyncio
import json
import random
from typing import Any, Awaitable, Callable, Dict, Optional

from sib.bots.service import GameBotService
from sib.database.models import MatchState


BOT_MOVE_DELAY_MIN_SECONDS = 0.8
BOT_MOVE_DELAY_MAX_SECONDS = 2.0
BotSleep = Callable[[float], Awaitable[None]]
DelayPicker = Callable[[float, float], float]


def _is_current_bot_turn(
    match,
    state: Dict[str, Any],
    bot_player_id: int,
    *,
    simultaneous: bool,
) -> bool:
    """Revalidate the match and bot turn before and after the async delay."""
    if (
        not match.is_bot_match
        or match.state != MatchState.ACTIVE
        or state.get("finished", False)
    ):
        return False

    player = next(
        (candidate for candidate in match.players if candidate.user_id == bot_player_id),
        None,
    )
    if player is None or player.user is None or not player.user.is_bot:
        return False

    if simultaneous:
        player_state = state.get("players", {}).get(str(bot_player_id))
        return (
            match.current_turn_player_id is None
            and player_state is not None
            and not player_state.get("locked", False)
        )

    return (
        state.get("current_player_id") == bot_player_id
        and match.current_turn_player_id == bot_player_id
    )


def _state_snapshot(state: Dict[str, Any]) -> str:
    """Stable snapshot used to reject any state transition during the wait."""
    return json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _wait_for_bot_turn(
    match,
    state: Dict[str, Any],
    bot_player_id: int,
    *,
    simultaneous: bool,
    sleep: BotSleep,
    delay_picker: DelayPicker,
) -> bool:
    if not _is_current_bot_turn(
        match, state, bot_player_id, simultaneous=simultaneous
    ):
        return False

    expected_state = _state_snapshot(state)
    delay = delay_picker(BOT_MOVE_DELAY_MIN_SECONDS, BOT_MOVE_DELAY_MAX_SECONDS)
    await sleep(delay)

    # Awaiting is a cancellation point. Cancellation propagates, and a normal
    # wake-up must still prove that this exact turn is live before the strategy
    # is allowed to choose or apply a move.
    return (
        _is_current_bot_turn(
            match, state, bot_player_id, simultaneous=simultaneous
        )
        and _state_snapshot(state) == expected_state
    )


async def run_bot_turns(
    match,
    state: Dict[str, Any],
    *,
    sleep: Optional[BotSleep] = None,
    random_delay: Optional[DelayPicker] = None,
) -> Dict[str, Any]:
    """Run bot turns until it becomes a human turn or the game finishes.

    The short randomized wait stays inside the caller's existing serialized
    transition (active game actions hold the match row lock). That avoids
    detached tasks and keeps timeout/cancel/move races governed by the same
    lock. ``sleep`` and ``random_delay`` are injectable for deterministic,
    non-waiting tests. Cancellation is intentionally allowed to propagate.
    """
    if not match.is_bot_match:
        return state

    sleeper = sleep or asyncio.sleep
    delay_picker = random_delay or random.uniform

    while not state.get("finished", False):
        if match.state != MatchState.ACTIVE:
            break

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

            if not current_player or not current_player.user or not current_player.user.is_bot:
                break

            if not await _wait_for_bot_turn(
                match,
                state,
                current_player_id,
                simultaneous=False,
                sleep=sleeper,
                delay_picker=delay_picker,
            ):
                break

            state, _ = await GameBotService.play_turn(
                match=match,
                state=state,
                bot_player_id=current_player_id,
            )
            continue

        # Simultaneous games such as RPS / High Card.
        bot_player = next(
            (
                player
                for player in match.players
                if player.user is not None
                and player.user.is_bot
                and not state.get("players", {}).get(str(player.user_id), {}).get(
                    "locked", False
                )
            ),
            None,
        )

        if not bot_player:
            break

        if not await _wait_for_bot_turn(
            match,
            state,
            bot_player.user_id,
            simultaneous=True,
            sleep=sleeper,
            delay_picker=delay_picker,
        ):
            break

        state, _ = await GameBotService.play_turn(
            match=match,
            state=state,
            bot_player_id=bot_player.user_id,
        )

        # Simultaneous games need only one hidden bot choice per call.
        break

    return state
