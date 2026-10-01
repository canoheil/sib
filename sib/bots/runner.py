"""Helpers for running bot turns during active matches.

Design contract (bot humanization delay):

* A human game action is committed and rendered by its caller BEFORE any bot
  is allowed to think. The human callback never awaits a bot delay, and no
  session/row lock is held across one.
* Each bot turn is executed by ONE detached background task per match. The
  task first sleeps the randomized humanization delay OUTSIDE every
  lock/session, then opens a short-lived session, re-locks the match row,
  revalidates the live turn and executes exactly one bot move.
* When that move hands the turn to another bot, the same task loops and the
  next bot waits its OWN fresh delay. When the turn belongs to a human (or
  the match is no longer playable) the task exits.
* ``_ACTIVE_TASKS`` guarantees at most one bot-turn task per match, so a
  duplicate callback/matchmaking race can never schedule - or execute - the
  same bot turn twice.

Sleeping outside the lock matters: the previous implementation awaited the
delay inside the caller's serialized transaction, which pushed every bot
delay onto the human request that started the chain (the human waited for
all bots, then all bots moved back-to-back).
"""

import asyncio
import json
import random
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Match, MatchPlayer, MatchState
from sib.games.registry import get_game_engine
from sib.bots.registry import has_bot_strategy
from sib.bots.service import GameBotService


BOT_MOVE_DELAY_MIN_SECONDS = 0.8
BOT_MOVE_DELAY_MAX_SECONDS = 2.0
BotSleep = Callable[[float], Awaitable[None]]
DelayPicker = Callable[[float, float], float]

# Module-level seams. Production uses the real clock; tests replace them with
# a deterministic (often blocked) sleep so no test ever waits.
_sleep: BotSleep = asyncio.sleep
_random_delay: DelayPicker = random.uniform

# One live bot-turn task per match id. The task removes itself when it ends.
_ACTIVE_TASKS: Dict[int, "asyncio.Task[None]"] = {}


def _find_player(match: Any, player_id: int):
    return next(
        (player for player in match.players if player.user_id == player_id),
        None,
    )


def is_bot_player(match: Any, player_id: int) -> bool:
    """True when ``player_id`` is a persisted bot participant of ``match``."""
    player = _find_player(match, player_id)
    return bool(player is not None and player.user is not None and player.user.is_bot)


def next_bot_turn(
    match: Any,
    state: Dict[str, Any],
) -> Optional[Tuple[int, bool]]:
    """Return ``(bot_player_id, simultaneous)`` for the bot that must act now.

    Pure decision helper (no I/O). It is used both to decide whether a task is
    needed at all and to revalidate - on freshly reloaded state - that the
    exact turn this task was scheduled for is still the live bot turn.

    Turn-based games follow ``state["current_player_id"]`` (and the Match turn
    metadata kept in sync with it). Simultaneous games (RPS / High Card) have
    no current player: the next bot is the first bot whose hidden choice is
    still unlocked. Games without a registered bot strategy (e.g. Apple
    Battle's staged narration) never produce a scheduled bot turn.
    """
    if (
        not getattr(match, "is_bot_match", False)
        or getattr(match, "state", None) != MatchState.ACTIVE
        or state.get("finished", False)
    ):
        return None

    game = getattr(match, "game", None)
    if not has_bot_strategy(getattr(game, "code", None)):
        return None

    current_player_id = state.get("current_player_id")

    if current_player_id is not None:
        if not is_bot_player(match, current_player_id):
            return None
        # The Match row is part of the turn contract: a stale Match metadata
        # turn can never authorize a bot move on its own.
        if match.current_turn_player_id != current_player_id:
            return None
        return int(current_player_id), False

    players_state = state.get("players") or {}
    for player in match.players:
        if player.user is None or not player.user.is_bot:
            continue
        player_state = players_state.get(str(player.user_id))
        # ``locked`` must exist and be False: simultaneous engines manage it
        # explicitly, while unrelated states (e.g. Apple Battle) never match.
        if player_state is not None and player_state.get("locked") is False:
            if match.current_turn_player_id is None:
                return int(player.user_id), True
    return None


def has_pending_bot_turn(match: Any, state: Dict[str, Any]) -> bool:
    """True when a bot (not a human) must make the next move."""
    return next_bot_turn(match, state) is not None


def _is_current_bot_turn(
    match: Any,
    state: Dict[str, Any],
    bot_player_id: int,
    *,
    simultaneous: bool,
) -> bool:
    """Revalidate, on a freshly locked match, that this exact turn is live."""
    if (
        not match.is_bot_match
        or match.state != MatchState.ACTIVE
        or state.get("finished", False)
    ):
        return False

    player = _find_player(match, bot_player_id)
    if player is None or player.user is None or not player.user.is_bot:
        return False

    if simultaneous:
        player_state = (state.get("players") or {}).get(str(bot_player_id))
        return (
            match.current_turn_player_id is None
            and player_state is not None
            and player_state.get("locked") is False
        )

    return (
        state.get("current_player_id") == bot_player_id
        and match.current_turn_player_id == bot_player_id
    )


def bot_turn_task(match_id: int) -> Optional["asyncio.Task[None]"]:
    """The live bot-turn task of a match, if any."""
    task = _ACTIVE_TASKS.get(match_id)
    if task is not None and task.done():
        _ACTIVE_TASKS.pop(match_id, None)
        return None
    return task


def is_bot_turn_scheduled(match_id: int) -> bool:
    """True when a bot-turn task for this match is currently in flight."""
    return bot_turn_task(match_id) is not None


def schedule_bot_turn(
    match_id: int,
    *,
    bot: Any = None,
    sleep: Optional[BotSleep] = None,
    random_delay: Optional[DelayPicker] = None,
) -> Optional["asyncio.Task[None]"]:
    """Schedule the bot turns of one match in the background (non-blocking).

    Returns the created task, or ``None`` when a task for this match is
    already in flight (duplicate scheduling protection) so a duplicate
    callback can never double-run the same bot turn.

    The caller's request returns immediately: the delay belongs to the bot
    task, never to the human path. ``bot`` is only used for best-effort board
    presentation after a bot move.
    """
    existing = _ACTIVE_TASKS.get(match_id)
    if existing is not None and not existing.done():
        return None
    if existing is not None:
        _ACTIVE_TASKS.pop(match_id, None)

    task = asyncio.create_task(
        _run_bot_turns(
            match_id,
            bot=bot,
            sleeper=sleep if sleep is not None else _sleep,
            delay_picker=random_delay if random_delay is not None else _random_delay,
        ),
        name=f"sib:bot-turns:{match_id}",
    )
    _ACTIVE_TASKS[match_id] = task
    task.add_done_callback(lambda finished: _forget_task(match_id, finished))
    return task


def _forget_task(match_id: int, task: "asyncio.Task[None]") -> None:
    if _ACTIVE_TASKS.get(match_id) is task:
        _ACTIVE_TASKS.pop(match_id, None)


async def cancel_bot_turns(match_id: int) -> None:
    """Cancel a match's in-flight bot-turn task (used on shutdown/cleanup)."""
    task = _ACTIVE_TASKS.pop(match_id, None)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover - defensive
        logger.exception(f"Bot turn task for match {match_id} failed while cancelling")


async def cancel_all_bot_turns() -> None:
    """Cancel every in-flight bot-turn task (shutdown / test isolation)."""
    for match_id in list(_ACTIVE_TASKS.keys()):
        await cancel_bot_turns(match_id)


async def _run_bot_turns(
    match_id: int,
    *,
    bot: Any,
    sleeper: BotSleep,
    delay_picker: DelayPicker,
) -> None:
    """Background driver: one delay + one bot move per bot turn, in order."""
    try:
        while True:
            decision = await _next_turn_from_db(match_id)
            if decision is None:
                return
            bot_player_id, simultaneous = decision

            delay = delay_picker(
                BOT_MOVE_DELAY_MIN_SECONDS, BOT_MOVE_DELAY_MAX_SECONDS
            )
            # The sleep is the whole point of the humanization: it must never
            # happen inside a session, transaction or row lock, and never on
            # the human request that scheduled this task.
            await sleeper(delay)

            payload = await _apply_one_bot_move(
                match_id, bot_player_id, simultaneous=simultaneous
            )
            if payload is None:
                return

            if bot is not None:
                await _render_bot_move(bot, payload)

            if payload.get("finished"):
                return

            if simultaneous:
                # Simultaneous games need exactly ONE hidden choice per task:
                # the next round's bot turn is scheduled by the human action
                # that opens it (never pre-played without the human).
                return
            # Turn-based games loop only when the committed state says another
            # bot should act; that next bot gets its own fresh delay above.
    except asyncio.CancelledError:
        logger.info(f"Bot turn task cancelled for match {match_id}")
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(f"Bot turn task failed for match {match_id}: {exc}")


async def _next_turn_from_db(match_id: int) -> Optional[Tuple[int, bool]]:
    """Read-only snapshot of the pending bot turn (no lock, no delay)."""
    try:
        async with get_session() as session:
            match = await _load_match(session, match_id, lock=False)
            if match is None or not match.game_state:
                return None
            try:
                state = json.loads(match.game_state)
            except (TypeError, ValueError):
                logger.warning(f"Bot turn skipped for match {match_id}: invalid state")
                return None
            return next_bot_turn(match, state)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Could not read state for match {match_id}: {exc}")
        return None


async def _load_match(session, match_id: int, *, lock: bool):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    stmt = (
        select(Match)
        .where(Match.id == match_id)
        .execution_options(populate_existing=True)
        .options(
            selectinload(Match.players).selectinload(MatchPlayer.user),
            selectinload(Match.game),
        )
    )
    if lock:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def _apply_one_bot_move(
    match_id: int,
    bot_player_id: int,
    *,
    simultaneous: bool,
) -> Optional[Dict[str, Any]]:
    """Lock, revalidate, execute exactly ONE bot move and commit it.

    Returns the render payload for a successful move, or ``None`` when the
    task must stop (match gone/stale, turn changed, state no longer valid).
    """
    try:
        async with get_session() as session:
            match = await _load_match(session, match_id, lock=True)
            if match is None or not match.game_state:
                return None
            try:
                state = json.loads(match.game_state)
            except (TypeError, ValueError):
                logger.warning(f"Bot turn skipped for match {match_id}: invalid state")
                return None

            # Revalidate the whole turn contract on the locked row: match
            # still active, this bot still a bot, and this exact turn (not a
            # superseded one) still live.
            if not _is_current_bot_turn(
                match, state, bot_player_id, simultaneous=simultaneous
            ):
                return None

            engine = get_game_engine(match.game.code)
            state, _action = await GameBotService.play_turn(
                match=match,
                state=state,
                bot_player_id=bot_player_id,
            )
            match.game_state = json.dumps(state, ensure_ascii=False)
            GameBotService.sync_turn_metadata(match, state, engine)

            finished = engine.is_finished(state)
            if finished:
                # Apply the (idempotent) result before committing, exactly
                # like a human move does.
                from sib.services.match_result import MatchResultService

                await MatchResultService.apply_match_result(
                    session, match, engine.get_result(state)
                )

            await session.commit()
            return {"finished": finished, "match": match}
    except Exception as exc:  # pragma: no cover - defensive
        # A failed bot move must never kill the loop silently or leak a
        # half-open session; the maintenance worker re-schedules live bot
        # turns that lost their task.
        logger.exception(f"Bot move failed for match {match_id}: {exc}")
        return None


async def _render_bot_move(bot: Any, payload: Dict[str, Any]) -> None:
    """Best-effort board re-render after a committed bot move."""
    match = payload.get("match")
    try:
        from sib.services.game_sync import sync_match_messages

        await sync_match_messages(bot, match)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            f"Bot move presentation failed for match "
            f"{getattr(match, 'id', '?')}: {exc}"
        )
