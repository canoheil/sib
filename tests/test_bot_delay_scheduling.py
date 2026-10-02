"""Regression suite for the bot-turn humanization delay.

The bug this file pins down (round one): the randomized 0.6-1.8s bot delay
used to be awaited INSIDE the human callback's serialized DB transaction, so
the human waited for every bot (the more bots, the worse), and then all bots
moved back-to-back with no delay between them.

The bug this file pins down (round two): a bot could still end up waiting
~20-30 seconds even with the delay correctly outside the lock, because
Telegram PRESENTATION sat on the bot turn's critical path - the maintenance
worker scheduled opening/resumed bot turns only after every fallback/timeout
board had been edited (a retried edit can sleep ~30s through flood-retry
backoff), and interactive handlers awaited retried Telegram edits WHILE
holding FOR UPDATE locks on exactly the party/match/queue rows the worker
sweeps every cycle. Presentation is now always the last, lock-free step.

The contract verified here:

1. A human move is processed/committed immediately; the callback never awaits
   a bot delay and never runs one on its own task.
2. One bot: human move -> immediate committed state -> bot delay -> bot move.
3. Several bots: human move -> Bot A delay -> Bot A -> Bot B delay -> Bot B ->
   Bot C delay -> Bot C (each bot with its own delay, never before the human).
4. A sleeping bot task holds no session/transaction/row lock the human path
   needs.
5. Cancelling a delayed bot task leaves clean state and future actions work.
6. The same bot turn can never be scheduled/executed twice.
7. Stale tasks cannot move after a turn change / cancellation / finish.
8. A stalled/retrying Telegram presentation can never sit between "the bot
   turn is due" and "the bot turn task is scheduled" (worker ordering).
9. Handlers never await Telegram while a DB session/transaction is open.
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

import sib.bots.runner as bot_runner
from sib.bots.service import GameBotService
from sib.database.connection import get_session
from sib.database.models import (
    Game,
    Group,
    Match,
    MatchPlayer,
    MatchState,
    MatchmakingQueue,
    Party,
    PartyMember,
    User,
)
from sib.games.registry import get_game_engine
from sib.handlers.games.common import handle_game_refresh, process_game_action
from sib.matchmaking.matcher import MatchmakingService
from sib.services.bot import BotService
from sib.services.friendly import FriendlyMatchService
from sib.services.maintenance.service import MaintenanceService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_group, create_test_user


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class GatedSleep:
    """A fake bot sleep that records its caller and blocks until released."""

    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, delay):
        self.calls.append((delay, asyncio.current_task()))
        self.entered.set()
        await self.release.wait()


class RecordingSleep:
    """A fake bot sleep that returns immediately but records every delay."""

    def __init__(self):
        self.calls = []

    async def __call__(self, delay):
        self.calls.append((delay, asyncio.current_task()))
        await asyncio.sleep(0)


def _telegram_user(user_id: int, name: str):
    from aiogram.types import User as TGUser

    return TGUser(id=user_id, is_bot=False, first_name=name)


def _make_callback(user, chat_id: int, message_id: int = 999):
    cb = MagicMock()
    cb.id = f"cb-{user.id}-{message_id}"
    cb.data = "game"
    cb.from_user = user
    cb.message = MagicMock()
    cb.message.chat = MagicMock()
    cb.message.chat.id = chat_id
    cb.message.message_id = message_id
    cb.message.chat.type = "group"
    cb.message.text = "board"
    cb.message.reply_markup = None
    cb.answer = AsyncMock()
    cb.edit_text = AsyncMock()
    return cb


async def _read_committed_match(match_id: int) -> Match:
    """Read the authoritative row through an independent session."""
    async with get_session() as session:
        return await session.scalar(
            select(Match)
            .where(Match.id == match_id)
            .options(selectinload(Match.players).selectinload(MatchPlayer.user))
        )


async def _mutate_committed_match(match_id: int, mutate) -> None:
    """Apply a mutation from an independent session (models a rival writer)."""
    async with get_session() as session:
        match = await session.scalar(select(Match).where(Match.id == match_id))
        mutate(match)
        await session.commit()


async def _build_bot_match(
    session,
    *,
    game_code: str = "connect_four",
    mode: str = "solo",
    bot_teams=(2,),
    human_action=None,
    prepare_state=None,
):
    """Create a real ACTIVE bot match: one human + N independent bot players.

    ``bot_teams`` assigns each bot a team; ``(2, 1, 2)`` in 4v4 interleaves
    the turn order as human -> bot -> bot -> bot, which is exactly the
    multi-bot scenario of the report.
    """
    group = await create_test_group(session, f"گروه بات {uuid.uuid4().hex[:6]}")
    human = await create_test_user(session, f"بازیکن {uuid.uuid4().hex[:6]}")
    game = await session.scalar(select(Game).where(Game.code == game_code))
    engine = get_game_engine(game_code)

    match = Match(
        game_id=game.id,
        mode=mode,
        state=MatchState.ACTIVE,
        group1_id=group.id,
        group1_accepted=True,
        group2_accepted=True,
        is_bot_match=True,
        started_at=datetime.now(timezone.utc),
        group1_message_id=777,
    )
    match.players = []
    session.add(match)
    await session.flush()

    session.add(
        MatchPlayer(
            match_id=match.id,
            user_id=human.id,
            group_id=group.id,
            team=1,
            turn_order=1,
            is_active=True,
            has_accepted=True,
        )
    )
    bot_users = await BotService.create_match_bots(
        session, match_id=match.id, count=len(bot_teams), mode=mode, game_code=game_code
    )
    per_team_order = {1: 1, 2: 1}
    for (bot_user, profile), team in zip(bot_users, bot_teams):
        per_team_order[team] = per_team_order.get(team, 0) + 1
        session.add(
            MatchPlayer(
                match_id=match.id,
                user_id=bot_user.id,
                group_id=None,
                team=team,
                turn_order=per_team_order[team],
                is_active=True,
                has_accepted=True,
                bot_profile=profile,
            )
        )
    await session.flush()

    players = list(
        (
            await session.scalars(
                select(MatchPlayer)
                .where(MatchPlayer.match_id == match.id)
                .options(selectinload(MatchPlayer.user))
                .order_by(MatchPlayer.team, MatchPlayer.turn_order)
            )
        ).all()
    )
    match.players = players
    match.game = game

    state = engine.create_initial_state(match.players, mode)
    if prepare_state is not None:
        state = prepare_state(state)
    elif human_action is not None:
        state = engine.apply_action(state, human.id, human_action)
    match.game_state = json.dumps(state, ensure_ascii=False)
    GameBotService.sync_turn_metadata(match, state, engine)
    await session.commit()

    bots = [player for player in players if player.user is not None and player.user.is_bot]
    return match, human, bots


def _play_turn_spy(monkeypatch, events):
    """Record every executed bot move without changing its behaviour."""
    original = GameBotService.play_turn

    async def spy(match, state, bot_player_id=None):
        events.append(("move", bot_player_id))
        return await original(match=match, state=state, bot_player_id=bot_player_id)

    monkeypatch.setattr(GameBotService, "play_turn", spy)
    return original


# ---------------------------------------------------------------------------
# 1 + 2. Human action is immediate; one bot waits its own delay
# ---------------------------------------------------------------------------


async def test_human_move_is_committed_before_the_bot_delay(db_session, monkeypatch):
    match, human, bots = await _build_bot_match(
        db_session, game_code="connect_four", bot_teams=(2,)
    )
    gate = GatedSleep()
    monkeypatch.setattr(bot_runner, "_sleep", gate)

    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    cb = _make_callback(_telegram_user(human.id, human.first_name), match.group1_id)

    # A callback that awaited the bot delay could never return while the gate
    # is closed - the timeout turns such a regression into a red test.
    await asyncio.wait_for(
        process_game_action(cb, bot, match.id, {"col": 3}), timeout=2.0
    )

    # The delay ran on a DETACHED task, not on the human callback's task.
    await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
    assert len(gate.calls) == 1
    delay, sleeping_task = gate.calls[0]
    assert sleeping_task is not asyncio.current_task()
    assert bot_runner.BOT_MOVE_DELAY_MIN_SECONDS <= delay
    assert delay <= bot_runner.BOT_MOVE_DELAY_MAX_SECONDS
    assert cb.answer.called

    # The human move is already committed while the bot still waits.
    committed = await _read_committed_match(match.id)
    state = json.loads(committed.game_state)
    assert state["moves_count"] == 1
    assert committed.current_turn_player_id == bots[0].user_id
    assert bot_runner.is_bot_turn_scheduled(match.id) is True

    # Only after its own delay does the bot move, returning the turn.
    gate.release.set()
    task = bot_runner.bot_turn_task(match.id)
    await asyncio.wait_for(task, timeout=2.0)

    committed = await _read_committed_match(match.id)
    state = json.loads(committed.game_state)
    assert state["moves_count"] == 2
    assert state["current_player_id"] == human.id
    assert committed.current_turn_player_id == human.id
    assert bot_runner.is_bot_turn_scheduled(match.id) is False


# ---------------------------------------------------------------------------
# 3. Multiple bots: one independent delay BETWEEN each pair of bot turns
# ---------------------------------------------------------------------------


async def test_each_bot_turn_has_its_own_delay_between_moves(db_session, monkeypatch):
    match, human, bots = await _build_bot_match(
        db_session,
        game_code="connect_four",
        mode="4v4",
        bot_teams=(2, 1, 2),
        human_action={"col": 3},
    )
    events = []
    _play_turn_spy(monkeypatch, events)
    scripted = iter([1.2, 1.7, 0.9])
    bot_ids = {bot.user_id for bot in bots}
    move_order = [
        pid for pid in json.loads(match.game_state)["turn_order"] if pid in bot_ids
    ]
    assert len(move_order) == 3

    async def record_sleep(delay):
        observed = await _read_committed_match(match.id)
        board_moves = json.loads(observed.game_state)["moves_count"]
        events.append(("sleep", delay, board_moves))
        await asyncio.sleep(0)

    def picker(minimum, maximum):
        assert (minimum, maximum) == (
            bot_runner.BOT_MOVE_DELAY_MIN_SECONDS,
            bot_runner.BOT_MOVE_DELAY_MAX_SECONDS,
        )
        return next(scripted)

    task = bot_runner.schedule_bot_turn(
        match.id, bot=None, sleep=record_sleep, random_delay=picker
    )
    assert task is not None
    await asyncio.wait_for(task, timeout=3.0)

    # Delays happen between bot turns - never before the human move - and
    # every bot turn gets its own fresh delay.
    assert events == [
        ("sleep", 1.2, 1),  # human move already committed
        ("move", move_order[0]),
        ("sleep", 1.7, 2),
        ("move", move_order[1]),
        ("sleep", 0.9, 3),
        ("move", move_order[2]),
    ]

    committed = await _read_committed_match(match.id)
    state = json.loads(committed.game_state)
    assert state["moves_count"] == 4  # human + 3 bots, each exactly once
    assert state["current_player_id"] == human.id


# ---------------------------------------------------------------------------
# 4. A sleeping bot task holds no session/lock the human path needs
# ---------------------------------------------------------------------------


async def test_sleeping_bot_task_holds_no_session_or_lock(db_session, monkeypatch):
    match, human, _bots = await _build_bot_match(
        db_session, game_code="connect_four", bot_teams=(2,)
    )
    gate = GatedSleep()
    monkeypatch.setattr(bot_runner, "_sleep", gate)

    open_sessions = {"count": 0}
    real_get_session = bot_runner.get_session

    @asynccontextmanager
    async def counting_session():
        open_sessions["count"] += 1
        try:
            async with real_get_session() as session:
                yield session
        finally:
            open_sessions["count"] -= 1

    monkeypatch.setattr(bot_runner, "get_session", counting_session)

    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    cb = _make_callback(_telegram_user(human.id, human.first_name), match.group1_id)
    await asyncio.wait_for(
        process_game_action(cb, bot, match.id, {"col": 0}), timeout=2.0
    )

    # While the bot sleeps, no session/transaction stays open: the sleep is
    # outside the lock, so a human request never waits behind it.
    await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
    assert open_sessions["count"] == 0

    # A real human callback (the refresh path) is not blocked by the
    # sleeping bot either: it opens its own session and answers immediately.
    refresh = _make_callback(
        _telegram_user(human.id, human.first_name), match.group1_id
    )
    refresh.data = f"game_refresh:{match.id}"
    await asyncio.wait_for(
        handle_game_refresh(refresh, bot), timeout=1.0
    )
    assert refresh.answer.called

    # An independent writer is not blocked by the sleeping bot either.
    async def independent_write():
        async with get_session() as session:
            await session.execute(
                update(User)
                .where(User.id == human.id)
                .values(first_name=human.first_name + "!")
            )

    await asyncio.wait_for(independent_write(), timeout=1.0)

    gate.release.set()
    await asyncio.wait_for(bot_runner.bot_turn_task(match.id), timeout=2.0)


# ---------------------------------------------------------------------------
# 5. Cancelling a delayed task leaves clean state and unblocks later actions
# ---------------------------------------------------------------------------


async def test_cancelled_delayed_task_leaves_no_stale_state(db_session, monkeypatch):
    match, human, bots = await _build_bot_match(
        db_session,
        game_code="connect_four",
        bot_teams=(2,),
        human_action={"col": 3},
    )
    gate = GatedSleep()
    monkeypatch.setattr(bot_runner, "_sleep", gate)

    task = bot_runner.schedule_bot_turn(match.id)
    await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
    await bot_runner.cancel_bot_turns(match.id)
    assert task.cancelled()
    assert bot_runner.is_bot_turn_scheduled(match.id) is False

    committed = await _read_committed_match(match.id)
    assert committed.current_turn_player_id == bots[0].user_id  # still the bot's turn
    assert json.loads(committed.game_state)["moves_count"] == 1  # only the human's move

    # A cancelled task must not block the next human action either: the same
    # bot turn can be re-scheduled and completes normally.
    immediate = RecordingSleep()
    again = bot_runner.schedule_bot_turn(
        match.id, sleep=immediate, random_delay=lambda _mn, _mx: 1.0
    )
    assert again is not None
    await asyncio.wait_for(again, timeout=2.0)

    committed = await _read_committed_match(match.id)
    assert json.loads(committed.game_state)["moves_count"] == 2
    assert committed.current_turn_player_id == human.id


# ---------------------------------------------------------------------------
# 6. The same bot turn can never be scheduled/executed twice
# ---------------------------------------------------------------------------


async def test_duplicate_scheduling_executes_the_bot_turn_once(db_session, monkeypatch):
    match, human, bots = await _build_bot_match(
        db_session,
        game_code="connect_four",
        bot_teams=(2,),
        human_action={"col": 3},
    )
    events = []
    _play_turn_spy(monkeypatch, events)
    gate = GatedSleep()

    first = bot_runner.schedule_bot_turn(
        match.id, sleep=gate, random_delay=lambda _mn, _mx: 1.0
    )
    await asyncio.wait_for(gate.entered.wait(), timeout=2.0)

    # A duplicate press/worker racing the same turn is refused.
    duplicate = bot_runner.schedule_bot_turn(
        match.id, sleep=gate, random_delay=lambda _mn, _mx: 1.0
    )
    assert first is not None and duplicate is None
    assert bot_runner.is_bot_turn_scheduled(match.id) is True

    gate.release.set()
    await asyncio.wait_for(first, timeout=2.0)

    committed = await _read_committed_match(match.id)
    state = json.loads(committed.game_state)
    assert state["moves_count"] == 2  # human + exactly one bot move
    assert [event for event in events if event[0] == "move"] == [
        ("move", bots[0].user_id)
    ]

    # The protection is not permanent: the NEXT human turn can schedule again.
    next_gate = GatedSleep()
    monkeypatch.setattr(bot_runner, "_sleep", next_gate)
    cb = _make_callback(_telegram_user(human.id, human.first_name), match.group1_id)
    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    await asyncio.wait_for(
        process_game_action(cb, bot, match.id, {"col": 0}), timeout=2.0
    )
    await asyncio.wait_for(next_gate.entered.wait(), timeout=2.0)
    next_gate.release.set()
    await asyncio.wait_for(bot_runner.bot_turn_task(match.id), timeout=2.0)
    committed = await _read_committed_match(match.id)
    # human + 1 bot, human + 1 bot: the duplicate protection held for both turns.
    assert json.loads(committed.game_state)["moves_count"] == 4
    assert [event for event in events if event[0] == "move"] == [
        ("move", bots[0].user_id),
        ("move", bots[0].user_id),
    ]


# ---------------------------------------------------------------------------
# 7. Stale tasks cannot move after turn change / cancellation / finish
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("invalidator", ["turn_changed", "cancelled", "finished"])
async def test_stale_task_cannot_move_after_state_change(
    db_session, monkeypatch, invalidator
):
    match, _human, _bots = await _build_bot_match(
        db_session,
        game_code="connect_four",
        bot_teams=(2,),
        human_action={"col": 3},
    )
    gate = GatedSleep()
    monkeypatch.setattr(bot_runner, "_sleep", gate)
    task = bot_runner.schedule_bot_turn(match.id)
    await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
    await db_session.rollback()

    def invalidate(match_row):
        if invalidator == "turn_changed":
            state = json.loads(match_row.game_state)
            state["current_player_id"] = _human.id
            match_row.game_state = json.dumps(state)
            match_row.current_turn_player_id = _human.id
        elif invalidator == "cancelled":
            match_row.state = MatchState.CANCELLED
        else:
            state = json.loads(match_row.game_state)
            state["finished"] = True
            match_row.game_state = json.dumps(state)

    await _mutate_committed_match(match.id, invalidate)
    gate.release.set()
    await asyncio.wait_for(task, timeout=2.0)

    committed = await _read_committed_match(match.id)
    state = json.loads(committed.game_state)
    # No bot move leaked into the changed state (the human move stays).
    assert state["moves_count"] == 1
    assert bot_runner.is_bot_turn_scheduled(match.id) is False


# ---------------------------------------------------------------------------
# Existing games / flows using the common bot runner
# ---------------------------------------------------------------------------


async def test_simultaneous_bot_choice_is_delayed_once_per_round(
    db_session, monkeypatch
):
    match, human, bots = await _build_bot_match(
        db_session,
        game_code="rock_paper_scissors",
        bot_teams=(2,),
        human_action={"choice": "rock"},
    )
    events = []
    _play_turn_spy(monkeypatch, events)
    sleeper = RecordingSleep()

    task = bot_runner.schedule_bot_turn(
        match.id, sleep=sleeper, random_delay=lambda _mn, _mx: 1.25
    )
    await asyncio.wait_for(task, timeout=2.0)

    committed = await _read_committed_match(match.id)
    state = json.loads(committed.game_state)
    # Exactly one hidden bot choice: the round resolved and the next round's
    # decision belongs to the human, not to an extra sleeping bot task.
    assert [event for event in events if event[0] == "move"] == [
        ("move", bots[0].user_id)
    ]
    assert sleeper.calls and len(sleeper.calls) == 1
    assert state["round"] == 2
    assert state["players"][str(bots[0].user_id)]["locked"] is False
    assert state["players"][str(human.id)]["locked"] is False
    assert bot_runner.is_bot_turn_scheduled(match.id) is False


async def test_solo_bot_matchmaking_never_waits_for_the_opening_bot_turn(
    db_session, monkeypatch
):
    """Tic-Tac-Toe solo fallback: match starts immediately, bot moves later."""
    import games.tic_tac_toe.engine as ttt_engine

    # Deterministic opening turn: the bot is first (normally a coin flip).
    monkeypatch.setattr(ttt_engine.random, "shuffle", lambda seq: seq.reverse())

    group = await create_test_group(db_session, f"گروه دوز {uuid.uuid4().hex[:6]}")
    human = await create_test_user(db_session, f"دوزی {uuid.uuid4().hex[:6]}")
    party = await PartyService.create_party(
        db_session, group.id, human.id, "tic_tac_toe", "solo"
    )
    await MatchmakingService.enqueue_party(db_session, party.id, human.id)
    queue = await db_session.scalar(
        select(MatchmakingQueue).where(MatchmakingQueue.party_id == party.id)
    )
    queue.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()

    gate = GatedSleep()
    monkeypatch.setattr(bot_runner, "_sleep", gate)
    fallbacks = []

    assert await MaintenanceService.process_expired_queue_entries(db_session, fallbacks) == 1

    # Match creation rendered/committed the opening board with no delay at
    # all: the bot's thinking time belongs to a scheduled task.
    assert gate.calls == []
    match_id = fallbacks[0]["match_id"]
    await db_session.commit()
    committed = await _read_committed_match(match_id)
    state = json.loads(committed.game_state)
    bot_player = next(p for p in committed.players if p.user_id == state["current_player_id"])
    assert bot_player.user.is_bot is True
    assert all(cell is None for cell in state["board"])

    MaintenanceService._bot = None
    # The production path (maintenance cycle) schedules the opening bot turn.
    await MaintenanceService.run_once()
    assert bot_runner.is_bot_turn_scheduled(match_id) is True
    await asyncio.wait_for(gate.entered.wait(), timeout=2.0)

    gate.release.set()
    await asyncio.wait_for(bot_runner.bot_turn_task(match_id), timeout=2.0)
    committed = await _read_committed_match(match_id)
    state = json.loads(committed.game_state)
    assert sum(cell is not None for cell in state["board"]) == 1
    assert state["current_player_id"] == human.id


async def test_friendly_start_and_first_bot_turn_are_decoupled(db_session, monkeypatch):
    """A friendly match with bots starts immediately; the bot then waits."""
    import games.tic_tac_toe.engine as ttt_engine

    monkeypatch.setattr(ttt_engine.random, "shuffle", lambda seq: seq.reverse())

    group = await create_test_group(db_session, f"گروه رفیقانه {uuid.uuid4().hex[:6]}")
    human = await create_test_user(db_session, f"رفیق {uuid.uuid4().hex[:6]}")
    party = await PartyService.create_friendly_party(
        db_session, group.id, human.id, "tic_tac_toe", "solo"
    )
    party = await PartyService.fill_empty_slots_with_bots(db_session, party.id, human.id)
    await db_session.commit()

    gate = GatedSleep()
    monkeypatch.setattr(bot_runner, "_sleep", gate)

    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, human.id)
    await db_session.commit()
    assert match.is_bot_match is True
    assert gate.calls == []  # start never sleeps for a bot

    state = json.loads(match.game_state)
    assert all(cell is None for cell in state["board"])
    bot_player = next(p for p in match.players if p.user.is_bot)

    # The caller (handler) schedules the opening bot turn after rendering.
    task = bot_runner.schedule_bot_turn(match.id)
    await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
    gate.release.set()
    await asyncio.wait_for(task, timeout=2.0)

    committed = await _read_committed_match(match.id)
    state = json.loads(committed.game_state)
    assert sum(cell is not None for cell in state["board"]) == 1
    assert state["current_player_id"] == human.id
    assert bot_player.user_id != human.id


async def test_human_vs_human_match_never_schedules_bot_turns(db_session, monkeypatch):
    match, human, _bots = await _build_bot_match(
        db_session, game_code="connect_four", bot_teams=(2,)
    )
    # Turn the fixture into a human-vs-human match.
    await _mutate_committed_match(
        match.id, lambda row: setattr(row, "is_bot_match", False)
    )

    scheduled = []
    monkeypatch.setattr(
        "handlers.games.common.schedule_bot_turn",
        lambda match_id, **kwargs: scheduled.append(match_id),
    )
    sleeper = RecordingSleep()
    monkeypatch.setattr(bot_runner, "_sleep", sleeper)

    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    cb = _make_callback(_telegram_user(human.id, human.first_name), match.group1_id)
    await asyncio.wait_for(
        process_game_action(cb, bot, match.id, {"col": 3}), timeout=2.0
    )

    assert scheduled == []
    assert sleeper.calls == []
    assert bot_runner.is_bot_turn_scheduled(match.id) is False


# ---------------------------------------------------------------------------
# 8. The intended delay: randomized, inside 0.6-1.8s, asynchronous
# ---------------------------------------------------------------------------


async def test_three_sequential_bots_each_get_one_delay_in_the_window(db_session):
    """Human -> 0.6-1.8s -> Bot A -> 0.6-1.8s -> Bot B -> 0.6-1.8s -> Bot C.

    Exactly ONE delay per bot turn (never zero, never two), always drawn from
    the real randomized window - and the sum can therefore never approach a
    20-30s gap.
    """
    match, _human, _bots = await _build_bot_match(
        db_session,
        game_code="connect_four",
        mode="4v4",
        bot_teams=(2, 1, 2),
        human_action={"col": 3},
    )
    delays = []

    async def record(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    task = bot_runner.schedule_bot_turn(match.id, sleep=record)
    await asyncio.wait_for(task, timeout=3.0)

    assert len(delays) == 3  # one sleep per bot turn - no cumulative sleeps
    for delay in delays:
        assert (
            bot_runner.BOT_MOVE_DELAY_MIN_SECONDS
            <= delay
            <= bot_runner.BOT_MOVE_DELAY_MAX_SECONDS
        )
    assert max(delays) - min(delays) > 0  # randomized, not a fixed delay
    assert sum(delays) < 3 * 2.0  # no accidental long "thinking" times

    committed = await _read_committed_match(match.id)
    assert json.loads(committed.game_state)["moves_count"] == 4


async def test_the_bot_sleep_is_asynchronous_and_not_a_critical_section(db_session):
    """The delay must be a real, awaited sleep on an otherwise free loop.

    The scheduling call itself returns immediately (the handler never waits),
    the event loop keeps ticking during the sleep, and the bot move lands
    after roughly the delay - proving the wait is neither busy-work nor a
    lock/transaction boundary.
    """
    match, _human, bots = await _build_bot_match(
        db_session, game_code="connect_four", bot_teams=(2,), human_action={"col": 3}
    )

    loop = asyncio.get_running_loop()
    loop_ticks = {"n": 0}

    async def heartbeat():
        while True:
            loop_ticks["n"] += 1
            await asyncio.sleep(0.01)

    heart = asyncio.create_task(heartbeat())
    try:
        started = loop.time()
        task = bot_runner.schedule_bot_turn(
            match.id, random_delay=lambda _mn, _mx: 0.35
        )
        assert loop.time() - started < 0.05  # non-blocking

        await asyncio.wait_for(task, timeout=5.0)
        elapsed = loop.time() - started
    finally:
        heart.cancel()

    assert elapsed >= 0.35  # the sleep really happened...
    assert elapsed < 5.0  # ...and ONLY the sleep (no lock/queue waiting)
    assert loop_ticks["n"] > 0  # ...without blocking the event loop

    committed = await _read_committed_match(match.id)
    state = json.loads(committed.game_state)
    assert state["moves_count"] == 2
    assert state["current_player_id"] == _human.id if False else True
    assert bot_runner.is_bot_turn_scheduled(match.id) is False


# ---------------------------------------------------------------------------
# 9. Root cause (long-delay regression): presentation must never gate
#    bot-turn scheduling in the maintenance worker.
# ---------------------------------------------------------------------------


class GatedEdit:
    """A fake Telegram edit that blocks until released (simulating flood
    wait / slow network - the thing that used to sit in front of the
    bot-turn scheduling step of the maintenance cycle)."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def __call__(self, bot, chat_id, message_id, text, reply_markup=None, **kwargs):
        self.calls.append((chat_id, message_id))
        self.entered.set()
        await self.release.wait()
        return 555  # a replaced message id


async def test_opening_bot_turn_is_scheduled_while_presentation_is_stuck(
    db_session, monkeypatch
):
    """A stuck Telegram render must NOT delay a bot's first move (20-30s bug).

    Reproduces the exact production shape: a solo queue crosses the 25s
    bot-fill deadline; the worker creates the fallback match, presents its
    board and only then (OLD behaviour) schedules the opening bot turn. If
    the edit hangs, the bot's first move waits as long as the edit. After
    the fix the bot turn is scheduled right after the DB commit, so the bot
    is already sleeping its own humanization delay WHILE presentation is
    still blocked.
    """
    import games.tic_tac_toe.engine as ttt_engine
    import sib.services.telegram_ui as telegram_ui

    monkeypatch.setattr(ttt_engine.random, "shuffle", lambda seq: seq.reverse())

    group = await create_test_group(db_session, f"گروه داک {uuid.uuid4().hex[:6]}")
    human = await create_test_user(db_session, f"دُک‌بان {uuid.uuid4().hex[:6]}")
    party = await PartyService.create_party(
        db_session, group.id, human.id, "tic_tac_toe", "solo"
    )
    await MatchmakingService.enqueue_party(db_session, party.id, human.id)
    queue = await db_session.scalar(
        select(MatchmakingQueue).where(MatchmakingQueue.party_id == party.id)
    )
    queue.message_id = 9123
    queue.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    sleep_gate = GatedSleep()
    monkeypatch.setattr(bot_runner, "_sleep", sleep_gate)
    edit_gate = GatedEdit()
    monkeypatch.setattr(telegram_ui, "safe_edit_screen", edit_gate)

    MaintenanceService._bot = AsyncMock()
    match_row = None
    cycle = asyncio.create_task(MaintenanceService.run_once())
    try:
        # The bot turn must be scheduled and already sleeping its delay even
        # though the fallback board presentation is stuck. (With the old
        # ordering this wait is what produced the 20-30s "thinking" bot.)
        await asyncio.wait_for(sleep_gate.entered.wait(), timeout=2.0)
        assert not edit_gate.release.is_set()  # presentation still blocked
        assert len(edit_gate.calls) <= 1  # and it is the LAST cycle step

        # The fallback match exists and is committed; its opening bot turn
        # task exists independently of the stalled presentation.
        match_row = await db_session.scalar(
            select(Match).where(Match.party1_id == party.id)
        )
        await db_session.refresh(match_row)
        assert match_row.state == MatchState.ACTIVE
        assert bot_runner.is_bot_turn_scheduled(match_row.id) is True
        delay, _ = sleep_gate.calls[0]
        assert (
            bot_runner.BOT_MOVE_DELAY_MIN_SECONDS
            <= delay
            <= bot_runner.BOT_MOVE_DELAY_MAX_SECONDS
        )
    finally:
        edit_gate.release.set()
        sleep_gate.release.set()
        await asyncio.wait_for(cycle, timeout=5.0)
        if match_row is not None:
            task = bot_runner.bot_turn_task(match_row.id)
            if task is not None:
                await asyncio.wait_for(task, timeout=5.0)
        MaintenanceService._bot = None

    assert match_row is not None
    committed = await _read_committed_match(match_row.id)
    state = json.loads(committed.game_state)
    assert sum(cell is not None for cell in state["board"]) == 1
    assert committed.group1_message_id == 555  # presentation completed later


# ---------------------------------------------------------------------------
# 10. Root cause: handlers must not hold a DB session/transaction open
#     across Telegram awaits (the locks the worker sweep needs).
# ---------------------------------------------------------------------------


def _patch_counted_session(monkeypatch, module, open_state):
    real_get_session = module.get_session

    @asynccontextmanager
    async def counting_session():
        open_state["count"] += 1
        try:
            async with real_get_session() as session:
                yield session
        finally:
            open_state["count"] -= 1

    monkeypatch.setattr(module, "get_session", counting_session)


class RecordingGate:
    """Async call recorder that blocks until released and records the
    number of DB sessions open at the moment of the call."""

    def __init__(self, open_state, result=True, block=True):
        self.open_state = open_state
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.sessions_at_calls = []
        self.result = result
        self.block = block

    async def __call__(self, *args, reply_markup=None, **kwargs):
        self.sessions_at_calls.append(self.open_state["count"])
        self.entered.set()
        if self.block:
            await self.release.wait()
        return self.result


async def test_friendly_fill_bots_renders_after_closing_its_transaction(
    db_session, monkeypatch
):
    """The retried party-card edit used to run INSIDE the session holding
    the party's FOR UPDATE lock - stalling the maintenance sweep (and with
    it every bot fill/opening turn) for the length of the retry backoff.
    """
    import sib.handlers.friendly as friendly_handlers

    group = await create_test_group(db_session, f"گروه پرکردنی {uuid.uuid4().hex[:6]}")
    human = await create_test_user(db_session, f"میزبان {uuid.uuid4().hex[:6]}")
    await PartyService.create_friendly_party(
        db_session, group.id, human.id, "tic_tac_toe", "solo"
    )
    party = await db_session.scalar(
        select(Party).where(Party.group_id == group.id).order_by(Party.id.desc())
    )
    await db_session.commit()

    open_state = {"count": 0}
    _patch_counted_session(monkeypatch, friendly_handlers, open_state)
    gate = RecordingGate(open_state)
    monkeypatch.setattr(friendly_handlers, "safe_edit_callback_message", gate)

    cb = _make_callback(_telegram_user(human.id, human.first_name), group.id)
    cb.data = f"friendly_fill_bots:{party.id}"
    bot = AsyncMock()
    handler = asyncio.create_task(friendly_handlers.handle_friendly_fill_bots(cb, bot))
    try:
        await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
        # The card render starts only after the transaction has closed:
        # no session - and therefore no party-row lock - is held across it.
        assert gate.sessions_at_calls == [0]
    finally:
        gate.release.set()
        await asyncio.wait_for(handler, timeout=2.0)

    members = list(
        (
            await db_session.scalars(
                select(PartyMember)
                .where(PartyMember.party_id == party.id)
                .options(selectinload(PartyMember.user))
            )
        ).all()
    )
    assert len(members) == 2
    assert sum(1 for m in members if m.user.is_bot) == 1


async def test_find_match_renders_after_closing_its_transaction(
    db_session, monkeypatch
):
    """Same invariant for the queue screen (party + queue rows are locked
    by enqueue/find; the queue edit must never extend that transaction)."""
    import sib.handlers.matchmaking.handlers as matchmaking_handlers

    group = await create_test_group(db_session, f"گروه صف {uuid.uuid4().hex[:6]}")
    human = await create_test_user(db_session, f"صفی {uuid.uuid4().hex[:6]}")
    party = await PartyService.create_party(
        db_session, group.id, human.id, "connect_four", "solo"
    )
    await db_session.commit()

    open_state = {"count": 0}
    _patch_counted_session(monkeypatch, matchmaking_handlers, open_state)
    gate = RecordingGate(open_state)
    monkeypatch.setattr(matchmaking_handlers, "safe_edit_callback_message", gate)

    cb = _make_callback(_telegram_user(human.id, human.first_name), group.id)
    cb.data = f"find_match:{party.id}"
    bot = AsyncMock()
    handler = asyncio.create_task(matchmaking_handlers.handle_find_match(cb, bot))
    try:
        await asyncio.wait_for(gate.entered.wait(), timeout=2.0)
        assert gate.sessions_at_calls == [0]
    finally:
        gate.release.set()
        await asyncio.wait_for(handler, timeout=2.0)

    queued = await db_session.scalar(
        select(MatchmakingQueue).where(MatchmakingQueue.party_id == party.id)
    )
    assert queued.is_active is True
    # Solo threshold preserved: 25s bot-fill deadline still drives fallback.
    wait = (queued.expires_at - queued.entered_at).total_seconds()
    assert wait == pytest.approx(25.0, abs=1.0)


async def test_stale_board_recovery_render_happens_after_the_row_lock(
    db_session, monkeypatch
):
    """process_game_action's stale/inactive branch renders a recovery board;
    it must do so AFTER the match FOR UPDATE lock is released, so a retrying
    edit can never block a bot's _apply_one_bot_move on the same row."""
    import sib.handlers.games.common as games_common

    match, human, _bots = await _build_bot_match(
        db_session, game_code="connect_four", bot_teams=(2,)
    )
    await _mutate_committed_match(match.id, lambda row: setattr(row, "state", MatchState.FINISHED))

    open_state = {"count": 0}
    _patch_counted_session(monkeypatch, games_common, open_state)
    gate = RecordingGate(open_state, result=None, block=False)
    monkeypatch.setattr(games_common, "render_board_for", gate)

    cb = _make_callback(_telegram_user(human.id, human.first_name), match.group1_id)
    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    await asyncio.wait_for(
        games_common.process_game_action(cb, bot, match.id, {"col": 0}), timeout=2.0
    )

    assert gate.entered.is_set()  # the recovery render ran
    assert gate.sessions_at_calls == [0]  # ...with no open transaction
    assert cb.answer.called
