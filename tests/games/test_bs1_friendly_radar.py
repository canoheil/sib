"""BS1 — privacy tests for friendly Battleship (neutral board + private radar).

The bug these tests lock down: a friendly match shares ONE group message
between both teams, so the old board flipped to the perspective of whoever was
on turn and published that player's radar (and a live targeting grid) to the
whole group.

Contract asserted here:

* the group board never contains a radar grid, never contains shot callbacks,
  and is identical for everybody;
* the shooting grid is delivered as an ephemeral message addressed to ONE
  player (``receiver_user_id``) and replaces the message they tapped;
* one tap updates the shooter's private panel AND the public group board;
* delivery degrades ephemeral -> DM -> short alert, and no failure path ever
  writes a radar into the group;
* a stale callback (wrong turn, expired panel, spectator) leaks nothing,
  crashes nothing, and cannot corrupt the stored group-board pointer.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    Message,
    User as TelegramUser,
)

from sib.database.models import Match, MatchPlayer, User
from sib.games.battleship.engine import BattleshipEngine
from sib.handlers.games.battleship import (
    handle_bs_action,
    handle_bs_radar,
    handle_bs_shot,
)
from sib.services.battleship_radar import ALERT_MAX_LENGTH
from sib.services.friendly import FriendlyMatchService, apply_friendly_state
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_group, create_test_user

GRID_HEADER = "A B C D E F G H"
GROUP_MESSAGE_ID = 700


class FakeBadRequest(TelegramBadRequest):
    """TelegramBadRequest with a simple constructor (mirrors other tests)."""

    def __init__(self, text: str):
        self.message = text
        self.method = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engine_state():
    """Deterministic two-player Battleship state with friendly team names."""
    u1 = User(id=101, first_name="آرش")
    u2 = User(id=102, first_name="بابک")
    mp1 = MatchPlayer(match_id=1, user_id=101, team="A", group_id=-1001)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=1, user_id=102, team="B", group_id=-1001)
    mp2.user = u2

    engine = BattleshipEngine()
    state = engine.create_initial_state(
        [mp1, mp2], mode="solo", fleet_seed_a=11, fleet_seed_b=22
    )
    state["team_names"] = {"1": "🍎 تیم سرخ", "2": "🍏 تیم سبز"}
    return engine, state


def _miss_cell(state, shooter_id: int):
    """A cell that is guaranteed to miss (no enemy ship there)."""
    engine = BattleshipEngine()
    my_code = engine.team_code_of(state, shooter_id)
    opp = "B" if my_code == "A" else "A"
    ship_cells = {
        tuple(cell)
        for ship in state["teams"][opp]["ships"]
        for cell in ship["cells"]
    }
    for r in range(engine.GRID_SIZE):
        for c in range(engine.GRID_SIZE):
            if (r, c) not in ship_cells:
                return r, c
    raise AssertionError("no empty cell on the board")


def _hit_cell(state, shooter_id: int):
    """A cell that is guaranteed to hit the enemy fleet."""
    engine = BattleshipEngine()
    my_code = engine.team_code_of(state, shooter_id)
    opp = "B" if my_code == "A" else "A"
    return tuple(state["teams"][opp]["ships"][0]["cells"][0])


async def _reload_match(db_session, match_id: int) -> Match:
    """Re-read the match from the DB (the handler committed in its own session)."""
    return await db_session.scalar(
        select(Match)
        .where(Match.id == match_id)
        .execution_options(populate_existing=True)
    )


async def _make_friendly_battleship(db_session):
    """A committed friendly 1v1 Battleship match with deterministic fleets."""
    group = await create_test_group(db_session, "گروه رفیقانه کشتی")
    player_a = await create_test_user(db_session, "آرش")
    player_b = await create_test_user(db_session, "بابک")

    party = await PartyService.create_friendly_party(
        db_session, group.id, player_a.id, "battleship", "solo"
    )
    party = await PartyService.join_party(db_session, party.id, player_b.id)
    await db_session.commit()

    match = await FriendlyMatchService.start_friendly_match(
        db_session, party.id, player_a.id
    )

    engine = BattleshipEngine()
    state = engine.create_initial_state(
        match.players, match.mode, fleet_seed_a=11, fleet_seed_b=22
    )
    apply_friendly_state(state, match, group.id)
    match.game_state = json.dumps(state, ensure_ascii=False)
    match.group1_message_id = GROUP_MESSAGE_ID
    match.group2_message_id = GROUP_MESSAGE_ID
    await db_session.commit()

    current = state["current_player_id"]
    on_turn, waiting = (
        (player_a, player_b) if current == player_a.id else (player_b, player_a)
    )
    return SimpleNamespace(
        group=group,
        match=match,
        state=state,
        engine=engine,
        a=player_a,
        b=player_b,
        on_turn=on_turn,
        waiting=waiting,
    )


def _group_callback(data: str, user_id: int, group_id: int, message_id: int = GROUP_MESSAGE_ID):
    cb = MagicMock(spec=CallbackQuery)
    cb.id = "callback-query-1"
    cb.data = data
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name="بازیکن")
    cb.message = Message(
        message_id=message_id,
        date=0,
        chat=TelegramChat(id=group_id, type="supergroup", title="گروه رفیقانه"),
    )
    cb.answer = AsyncMock()
    return cb


def _panel_callback(
    data: str,
    user_id: int,
    group_id: int,
    ephemeral_id: int = 555,
):
    """A callback coming FROM the player's ephemeral radar panel."""
    cb = _group_callback(data, user_id, group_id)
    cb.message = Message(
        message_id=0,
        date=0,
        chat=TelegramChat(id=group_id, type="supergroup"),
        ephemeral_message_id=ephemeral_id,
    )
    cb.answer = AsyncMock()
    return cb


def _ephemeral_bot(ephemeral_id: int = 555, chat_id: int = 0) -> AsyncMock:
    bot = AsyncMock(spec=Bot)
    bot.send_message.return_value = Message(
        message_id=0,
        date=0,
        chat=TelegramChat(id=chat_id, type="supergroup"),
        ephemeral_message_id=ephemeral_id,
    )
    return bot


def _callbacks(keyboard) -> list:
    return [
        button.callback_data
        for row in (keyboard.inline_keyboard if keyboard else [])
        for button in row
    ]


def _shot_callbacks(keyboard) -> list:
    return [
        callback
        for callback in _callbacks(keyboard)
        if str(callback).startswith("bs_shot:")
    ]


def _assert_neutral_group_text(text: str) -> None:
    """The public board must never carry a radar or side-relative wording."""
    assert GRID_HEADER not in text, "the group board must not render a radar grid"
    assert "·" not in text, "the group board must not render grid cells"
    assert "رادار شلیک‌های شما" not in text
    assert "ناوگان شما" not in text
    assert "نوبت شماست" not in text


# ---------------------------------------------------------------------------
# 1. The group board is neutral: no grid, no shot buttons
# ---------------------------------------------------------------------------


def test_group_board_has_no_grid_buttons():
    engine, state = _engine_state()

    keyboard = engine.get_public_keyboard(42)
    assert _callbacks(keyboard) == ["bs_radar:42", "game_refresh:42"]
    assert keyboard.inline_keyboard[0][0].text == "🎯 رادار من"
    assert keyboard.inline_keyboard[0][0].style == "success"
    assert keyboard.inline_keyboard[1][0].style == "primary"
    assert _shot_callbacks(keyboard) == []
    assert "bs_action:" not in str(_callbacks(keyboard))

    text = engine.render_public(state)
    _assert_neutral_group_text(text)
    assert "آرش" in text and "بابک" in text          # both players are named
    assert "🍎 تیم سرخ" in text and "🍏 تیم سبز" in text
    assert "🎯 **نوبت: آرش**" in text                 # whose turn it is
    assert "هنوز شلیکی انجام نشده است" in text

    # One public shot keeps the board readable and grid-free.
    r, c = _miss_cell(state, 101)
    state = engine.apply_action(state, 101, {"r": r, "c": c})
    text = engine.render_public(state)
    _assert_neutral_group_text(text)
    assert f"آرش به {engine._coord_label(r, c)} شلیک کرد" in text
    assert "🌊 آب" in text
    assert "🎯 **نوبت: بابک**" in text

    # A finished match names the winning TEAM (no "you").
    state["finished"] = True
    state["winner_team"] = "2"
    final_text = engine.render_public(state, finished=True)
    _assert_neutral_group_text(final_text)
    assert "بازی تمام شد" in final_text and "🍏 تیم سبز" in final_text


@pytest.mark.asyncio
async def test_group_board_edit_after_shot_has_no_grid(db_session):
    """The message edited in the group after a shot carries no radar."""
    ctx = await _make_friendly_battleship(db_session)
    bot = _ephemeral_bot(chat_id=ctx.group.id)
    r, c = _miss_cell(ctx.state, ctx.on_turn.id)

    await handle_bs_shot(
        _panel_callback(
            f"bs_shot:{ctx.match.id}:{r}:{c}", ctx.on_turn.id, ctx.group.id
        ),
        bot,
    )

    bot.edit_message_text.assert_awaited()
    call = bot.edit_message_text.await_args
    group_text, group_keyboard = call.args[0], call.kwargs["reply_markup"]
    _assert_neutral_group_text(group_text)
    assert f"{ctx.on_turn.first_name} به" in group_text
    assert "شلیک کرد" in group_text
    assert _shot_callbacks(group_keyboard) == []
    assert _callbacks(group_keyboard) == [
        f"bs_radar:{ctx.match.id}",
        f"game_refresh:{ctx.match.id}",
    ]


# ---------------------------------------------------------------------------
# 2. The radar panel is ephemeral and addressed to the active player only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ephemeral_radar_is_sent_to_active_player(db_session):
    ctx = await _make_friendly_battleship(db_session)
    bot = _ephemeral_bot(chat_id=ctx.group.id)
    callback = _group_callback(
        f"bs_radar:{ctx.match.id}", ctx.on_turn.id, ctx.group.id
    )

    await handle_bs_radar(callback, bot)

    assert bot.send_message.await_count == 1
    sent = bot.send_message.await_args.kwargs
    params = sent["ephemeral_message_parameters"]
    assert sent["chat_id"] == ctx.group.id          # sent INTO the group...
    assert params.receiver_user_id == ctx.on_turn.id  # ...but only this player sees it
    assert params.callback_query_id == callback.id
    assert params.replace_callback_query_message is True
    assert "🎯 **رادار شما (نوبت شماست)**" in sent["text"]
    assert GRID_HEADER in sent["text"]

    shot_callbacks = _shot_callbacks(sent["reply_markup"])
    assert len(shot_callbacks) == 64
    assert all(
        callback_data.startswith(f"bs_shot:{ctx.match.id}:")
        for callback_data in shot_callbacks
    )

    # Nothing is written to the shared group message when the radar opens.
    bot.edit_message_text.assert_not_awaited()
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_waiting_player_gets_a_read_only_panel(db_session):
    """The waiting player may look at their own radar, but cannot shoot."""
    ctx = await _make_friendly_battleship(db_session)
    bot = _ephemeral_bot(chat_id=ctx.group.id)

    await handle_bs_radar(
        _group_callback(f"bs_radar:{ctx.match.id}", ctx.waiting.id, ctx.group.id),
        bot,
    )

    sent = bot.send_message.await_args.kwargs
    assert sent["ephemeral_message_parameters"].receiver_user_id == ctx.waiting.id
    assert "🎯 **رادار شما**" in sent["text"]
    assert "⏳ نوبت" in sent["text"]
    assert _shot_callbacks(sent["reply_markup"]) == []


# ---------------------------------------------------------------------------
# 3. One shot updates BOTH the private panel and the public board
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shot_updates_both_ephemeral_and_group_messages(db_session):
    ctx = await _make_friendly_battleship(db_session)
    bot = _ephemeral_bot(chat_id=ctx.group.id)
    r, c = _hit_cell(ctx.state, ctx.on_turn.id)
    coord = ctx.engine._coord_label(r, c)

    await handle_bs_shot(
        _panel_callback(
            f"bs_shot:{ctx.match.id}:{r}:{c}",
            ctx.on_turn.id,
            ctx.group.id,
            ephemeral_id=777,
        ),
        bot,
    )

    # (a) the shooter's private panel shows the result and becomes read-only
    bot.edit_ephemeral_message_text.assert_awaited_once()
    panel = bot.edit_ephemeral_message_text.await_args.kwargs
    assert panel["chat_id"] == ctx.group.id
    assert panel["receiver_user_id"] == ctx.on_turn.id
    assert panel["ephemeral_message_id"] == 777
    assert f"شما به {coord} زدید" in panel["text"]
    assert "💥 برخورد" in panel["text"]
    assert _shot_callbacks(panel["reply_markup"]) == []

    # (b) the public group board gets the history line and the new turn
    bot.edit_message_text.assert_awaited()
    group = bot.edit_message_text.await_args
    group_text = group.args[0]
    assert group.kwargs["message_id"] == GROUP_MESSAGE_ID
    _assert_neutral_group_text(group_text)
    assert f"{ctx.on_turn.first_name} به {coord} شلیک کرد: 💥 برخورد" in group_text
    assert f"نوبت: {ctx.waiting.first_name}" in group_text

    # (c) the move is committed and the turn passed to the opponent
    await db_session.rollback()
    match = await _reload_match(db_session, ctx.match.id)
    state = json.loads(match.game_state)
    team_code = ctx.engine.team_code_of(state, ctx.on_turn.id)
    assert state["teams"][team_code]["shots_taken"][f"{r},{c}"] == "hit"
    assert state["current_player_id"] == ctx.waiting.id
    assert match.group1_message_id == GROUP_MESSAGE_ID

    # (d) the panel edit happens before the group edit (15-second window)
    assert bot.mock_calls.index(
        next(call for call in bot.mock_calls if call[0] == "edit_ephemeral_message_text")
    ) < bot.mock_calls.index(
        next(call for call in bot.mock_calls if call[0] == "edit_message_text")
    )


# ---------------------------------------------------------------------------
# 4. Fallbacks: ephemeral -> DM -> short alert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_to_dm_if_ephemeral_fails(db_session):
    ctx = await _make_friendly_battleship(db_session)
    attempts = []

    bot = AsyncMock(spec=Bot)

    async def send(*args, **kwargs):
        attempts.append(kwargs)
        if kwargs.get("ephemeral_message_parameters") is not None:
            raise FakeBadRequest("ephemeral delivery rejected")
        return Message(
            message_id=9001,
            date=0,
            chat=TelegramChat(id=kwargs["chat_id"], type="private"),
        )

    bot.send_message.side_effect = send
    callback = _group_callback(
        f"bs_radar:{ctx.match.id}", ctx.on_turn.id, ctx.group.id
    )

    await handle_bs_radar(callback, bot)

    # ephemeral (replace=True) -> ephemeral (replace=False) -> DM
    assert len(attempts) == 3
    assert attempts[0]["ephemeral_message_parameters"].replace_callback_query_message is True
    assert attempts[1]["ephemeral_message_parameters"].replace_callback_query_message is False
    assert "ephemeral_message_parameters" not in attempts[2]

    dm = attempts[-1]
    assert dm["chat_id"] == ctx.on_turn.id
    assert GRID_HEADER in dm["text"]
    assert dm["reply_markup"] is not None

    # the group chat is never used as a fallback surface
    bot.edit_message_text.assert_not_awaited()
    callback.answer.assert_awaited()

    # ...and a total delivery failure stays a short alert with no board text.
    bot2 = AsyncMock(spec=Bot)
    bot2.send_message.side_effect = FakeBadRequest("all private channels closed")
    callback2 = _group_callback(
        f"bs_radar:{ctx.match.id}", ctx.on_turn.id, ctx.group.id
    )
    await handle_bs_radar(callback2, bot2)

    alert = callback2.answer.await_args.args[0]
    assert callback2.answer.await_args.kwargs["show_alert"] is True
    assert len(alert) <= ALERT_MAX_LENGTH
    assert GRID_HEADER not in alert and "·" not in alert
    bot2.edit_message_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5. Stale / forged callbacks leak nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_callback_does_not_leak_radar(db_session):
    """The waiting player's stale grid cell is rejected without leaking."""
    ctx = await _make_friendly_battleship(db_session)
    bot = _ephemeral_bot(chat_id=ctx.group.id)
    r, c = _miss_cell(ctx.state, ctx.waiting.id)
    before = json.loads(ctx.match.game_state)

    callback = _panel_callback(
        f"bs_shot:{ctx.match.id}:{r}:{c}",
        ctx.waiting.id,
        ctx.group.id,
        ephemeral_id=888,
    )
    await handle_bs_shot(callback, bot)

    # (a) nothing was committed for the waiting player
    await db_session.rollback()
    match = await _reload_match(db_session, ctx.match.id)
    assert json.loads(match.game_state) == before

    # (b) their own stale panel is refreshed read-only (no live grid, no leak)
    bot.edit_ephemeral_message_text.assert_awaited()
    panel = bot.edit_ephemeral_message_text.await_args.kwargs
    assert panel["receiver_user_id"] == ctx.waiting.id
    assert panel["ephemeral_message_id"] == 888
    assert _shot_callbacks(panel["reply_markup"]) == []

    # (c) the group board is still neutral - the opponent's radar never lands there
    bot.edit_message_text.assert_awaited()
    group_text = bot.edit_message_text.await_args.args[0]
    _assert_neutral_group_text(group_text)

    # (d) the stored group-board pointer was not corrupted by an ephemeral id
    assert match.group1_message_id == GROUP_MESSAGE_ID
    assert match.group2_message_id == GROUP_MESSAGE_ID

    # (e) the callback was answered (no stuck spinner) - as a toast or alert
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_spectator_callback_is_refused_without_delivery(db_session):
    ctx = await _make_friendly_battleship(db_session)
    bot = _ephemeral_bot(chat_id=ctx.group.id)
    callback = _group_callback(f"bs_radar:{ctx.match.id}", 999_999, ctx.group.id)

    await handle_bs_radar(callback, bot)

    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_legacy_group_grid_tap_is_answered_privately(db_session):
    """A grid tap on a pre-change group board still plays, without a leak."""
    ctx = await _make_friendly_battleship(db_session)
    bot = _ephemeral_bot(chat_id=ctx.group.id)
    r, c = _miss_cell(ctx.state, ctx.on_turn.id)

    await handle_bs_action(
        _group_callback(f"bs_action:{ctx.match.id}:{r}:{c}", ctx.on_turn.id, ctx.group.id),
        bot,
    )

    # the shooter gets a private panel...
    sent = bot.send_message.await_args.kwargs
    assert sent["ephemeral_message_parameters"].receiver_user_id == ctx.on_turn.id
    assert GRID_HEADER in sent["text"]
    # ...and the group only receives the neutral public board.
    _assert_neutral_group_text(bot.edit_message_text.await_args.args[0])

    await db_session.rollback()
    match = await _reload_match(db_session, ctx.match.id)
    state = json.loads(match.game_state)
    team_code = ctx.engine.team_code_of(state, ctx.on_turn.id)
    assert state["teams"][team_code]["shots_taken"][f"{r},{c}"] == "miss"


@pytest.mark.asyncio
async def test_panel_close_deletes_the_ephemeral_message(db_session):
    """«✖️ بستن» removes the caller's private panel and nothing else."""
    from sib.handlers.games.battleship import handle_bs_close

    ctx = await _make_friendly_battleship(db_session)
    bot = _ephemeral_bot(chat_id=ctx.group.id)
    callback = _panel_callback(
        f"bs_close:{ctx.match.id}", ctx.on_turn.id, ctx.group.id, ephemeral_id=999
    )

    await handle_bs_close(callback, bot)

    bot.delete_ephemeral_message.assert_awaited_once_with(
        chat_id=ctx.group.id,
        receiver_user_id=ctx.on_turn.id,
        ephemeral_message_id=999,
    )
    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    callback.answer.assert_awaited()
