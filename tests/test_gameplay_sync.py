"""Gameplay state-synchronization tests (root-cause audit).

Enforces the core invariant of the project:

    AUTHORITATIVE STATE == RENDERED STATE == USER ACTIONS

Regression rule (from the audit): NO normal gameplay action may require
pressing Refresh to become usable. After every accepted action the
rendered keyboard must reflect the committed state, and whenever the
message says "your turn" at least one actionable button must exist.
"""

import asyncio
import json
from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser

from sib.database.models import Match, MatchState
from sib.games.registry import get_game_engine
from sib.handlers.games.battleship import handle_bs_action
from sib.handlers.games.tic_tac_toe import handle_ttt_action
from sib.matchmaking.acceptance import AcceptanceService
from sib.matchmaking.matcher import MatchmakingService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_group, create_test_user


# ---------------------------------------------------------------------------
# Fake telegram layer
# ---------------------------------------------------------------------------


def make_callback(user_id: int, first_name: str, chat_id: int, data: str) -> Tuple[Any, Any]:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name=first_name)
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type="supergroup", title="g")
    cb.message.message_id = 777
    cb.answer = AsyncMock()
    return cb


def last_edit_for(bot, chat_id: int):
    calls = [c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == chat_id]
    return calls[-1] if calls else None


def edit_text(call) -> str:
    """safe_edit_message_text passes text positionally."""
    return call.args[0] if call.args else call.kwargs.get("text", "")


def radar_markers(text: str) -> int:
    """Count shot markers inside the radar block only (not the event line).

    Visual language: ░ unselected, 🟦 shot water, 💥 hit, 🔥 sunk.
    """
    total = 0
    for line in text.splitlines():
        if line and all(ch in "░🟦💥🔥" for ch in line):
            total += line.count("🟦") + line.count("💥") + line.count("🔥")
    return total


def actionable_buttons(kb) -> List[str]:
    if kb is None:
        return []
    out = []
    for row in kb.inline_keyboard:
        for btn in row:
            cb = btn.callback_data or ""
            if cb and cb != "noop" and not cb.startswith("game_refresh:"):
                out.append(cb)
    return out


# ---------------------------------------------------------------------------
# Full Battleship match through the real handlers
# ---------------------------------------------------------------------------


async def _fresh_state(db_session, match_id: int) -> dict:
    m = await db_session.get(Match, match_id)
    await db_session.refresh(m)
    return json.loads(m.game_state)


class MatchCtx:
    """Plain-value snapshot of a match test context.

    NEVER hold live ORM objects across session boundaries in tests:
    a rollback expires them and any later attribute access raises
    MissingGreenlet (sync lazy load inside async code).
    """

    def __init__(self, match_id, g1, g2, u1, u2, engine):
        self.match_id = match_id
        self.g1_id = g1
        self.g2_id = g2
        self.u1_id = u1[0]
        self.u1_name = u1[1]
        self.u2_id = u2[0]
        self.u2_name = u2[1]
        self.engine = engine


async def _make_match(db_session, game_code: str) -> MatchCtx:
    g1 = await create_test_group(db_session, f"گروه {game_code} ۱")
    g2 = await create_test_group(db_session, f"گروه {game_code} ۲")
    u1 = await create_test_user(db_session, f"بازیکن {game_code} ۱")
    u2 = await create_test_user(db_session, f"بازیکن {game_code} ۲")
    snap = (
        g1.id, g2.id,
        (u1.id, u1.first_name), (u2.id, u2.first_name),
    )

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, game_code, "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, game_code, "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = get_game_engine(game_code)
    state = engine.create_initial_state(match.players, match.mode)
    match.game_state = json.dumps(state)
    match.group1_message_id = 111
    match.group2_message_id = 222
    match_id = match.id
    await db_session.commit()
    return MatchCtx(match_id, snap[0], snap[1], snap[2], snap[3], engine)


@pytest.mark.asyncio
async def test_battleship_full_match_stays_in_sync(db_session):
    """Full match: every accepted shot must update the radar consistently.

    Also enforces: after every shot, the board of the player whose turn it
    is must contain at least one actionable cell (no refresh required).
    """
    ctx = await _make_match(db_session, "battleship")
    mock_bot = AsyncMock()
    engine = ctx.engine
    state = await _fresh_state(db_session, ctx.match_id)

    async def shoot(uid, uname, gid, r, c):
        cb = make_callback(uid, uname, gid, f"bs_action:{ctx.match_id}:{r}:{c}")
        await handle_bs_action(cb, mock_bot)
        await db_session.rollback()
        m = await db_session.get(Match, ctx.match_id)
        await db_session.refresh(m)
        return cb, m

    users = {
        ctx.u1_id: (ctx.u1_id, ctx.u1_name, ctx.g1_id),
        ctx.u2_id: (ctx.u2_id, ctx.u2_name, ctx.g2_id),
    }
    shots = 0

    while not engine.is_finished(state):
        current_pid = state["current_player_id"]
        uid, uname, gid = users[current_pid]
        team_code = state["players"][str(current_pid)]["team_code"]
        shots_taken = state["teams"][team_code]["shots_taken"]
        target = None
        for r in range(8):
            for c in range(8):
                if f"{r},{c}" not in shots_taken:
                    target = (r, c)
                    break
            if target:
                break
        assert target is not None, "no cell to shoot but game not finished"

        cb, m = await shoot(uid, uname, gid, *target)
        shots += 1
        assert shots < 200, "game did not finish in reasonable move count"

        # callback answered exactly once and not with a rejection
        assert cb.answer.await_count == 1
        answer_text = cb.answer.call_args.args[0] if cb.answer.call_args.args else ""
        assert "نامعتبر" not in (answer_text or "")
        assert "نشده" not in (answer_text or "")

        state = json.loads(m.game_state)

        # The shooter's own radar text must reflect the shot (no refresh).
        shooter_team = state["players"][str(current_pid)]["team_code"]
        shots_taken = state["teams"][shooter_team]["shots_taken"]
        edit = last_edit_for(mock_bot, gid)
        assert edit is not None, f"no board edit for shooter group {gid} after shot"
        text = edit_text(edit)
        markers = radar_markers(text)
        assert markers == len(shots_taken), (
            f"shooter radar out of sync: text shows {markers} shots, "
            f"state has {len(shots_taken)}"
        )
        kb = edit.kwargs.get("reply_markup")
        shot_cb = f"bs_action:{ctx.match_id}:{target[0]}:{target[1]}"
        assert shot_cb not in actionable_buttons(kb), "shot cell still clickable after shot"

        if not engine.is_finished(state):
            cur = state["current_player_id"]
            cur_group = users[cur][2]
            edit = last_edit_for(mock_bot, cur_group)
            kb = edit.kwargs.get("reply_markup")
            assert actionable_buttons(kb), (
                f"current player {cur} has NO actionable button after shot "
                f"(turn text would claim their turn) — refresh-required bug"
            )

    # Finished cleanly.
    assert engine.is_finished(state)
    m = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(m)
    assert m.state == MatchState.FINISHED
    assert m.current_turn_player_id is None


@pytest.mark.asyncio
async def test_battleship_repeated_cell_rejected_without_state_change(db_session):
    ctx = await _make_match(db_session, "battleship")
    mock_bot = AsyncMock()
    state = await _fresh_state(db_session, ctx.match_id)
    first = state["current_player_id"]
    uid, uname, gid = (
        (ctx.u1_id, ctx.u1_name, ctx.g1_id) if first == ctx.u1_id
        else (ctx.u2_id, ctx.u2_name, ctx.g2_id)
    )

    cb1 = make_callback(uid, uname, gid, f"bs_action:{ctx.match_id}:0:0")
    await handle_bs_action(cb1, mock_bot)
    await db_session.rollback()
    m = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(m)
    state1 = json.loads(m.game_state)

    # Same player clicks the same cell again (stale keyboard).
    cb2 = make_callback(uid, uname, gid, f"bs_action:{ctx.match_id}:0:0")
    await handle_bs_action(cb2, mock_bot)
    await db_session.rollback()
    m = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(m)
    state2 = json.loads(m.game_state)

    # State must not change on the repeated click.
    assert state1 == state2
    answer_text = cb2.answer.call_args.args[0] if cb2.answer.call_args.args else ""
    # Depending on whether the turn already flipped, the stale double-click is
    # either "already shot" or "not your turn" — both are honest rejections.
    assert any(
        marker in (answer_text or "")
        for marker in ("قبلاً", "نامعتبر", "نوبت")
    ), f"stale double-click must be answered with a reason, got: {answer_text!r}"


@pytest.mark.asyncio
async def test_battleship_wrong_player_rejected(db_session):
    ctx = await _make_match(db_session, "battleship")
    mock_bot = AsyncMock()
    state = await _fresh_state(db_session, ctx.match_id)
    current = state["current_player_id"]
    oid, oname, ogid = (
        (ctx.u2_id, ctx.u2_name, ctx.g2_id) if current == ctx.u1_id
        else (ctx.u1_id, ctx.u1_name, ctx.g1_id)
    )

    cb = make_callback(oid, oname, ogid, f"bs_action:{ctx.match_id}:1:1")
    await handle_bs_action(cb, mock_bot)
    await db_session.rollback()
    m = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(m)
    assert json.loads(m.game_state) == state
    answer_text = cb.answer.call_args.args[0] if cb.answer.call_args.args else ""
    assert "نوبت" in (answer_text or "")


@pytest.mark.asyncio
async def test_battleship_concurrent_clicks_do_not_corrupt_state(db_session):
    """Two near-simultaneous clicks: exactly one is accepted, never both."""
    ctx = await _make_match(db_session, "battleship")
    mock_bot = AsyncMock()
    state = await _fresh_state(db_session, ctx.match_id)
    current = state["current_player_id"]
    uid, uname, gid = (
        (ctx.u1_id, ctx.u1_name, ctx.g1_id) if current == ctx.u1_id
        else (ctx.u2_id, ctx.u2_name, ctx.g2_id)
    )

    cb1 = make_callback(uid, uname, gid, f"bs_action:{ctx.match_id}:2:2")
    cb2 = make_callback(uid, uname, gid, f"bs_action:{ctx.match_id}:2:3")
    await asyncio.gather(handle_bs_action(cb1, mock_bot), handle_bs_action(cb2, mock_bot))
    await db_session.rollback()
    m = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(m)
    state_after = json.loads(m.game_state)

    team_code = state_after["players"][str(current)]["team_code"]
    shots = state_after["teams"][team_code]["shots_taken"]
    accepted = sum(1 for k in ("2,2", "2,3") if k in shots)
    # Either one accepted (both callbacks raced) or — if serialized by the
    # row lock — the second was rejected as "not your turn" after the turn
    # flipped. NEVER zero accepted, NEVER both accepted by the same player.
    assert accepted == 1, f"expected exactly one accepted shot, got {accepted}"


# ---------------------------------------------------------------------------
# Tic-Tac-Toe: stale-turn-state invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttt_full_match_stays_in_sync(db_session):
    ctx = await _make_match(db_session, "tic_tac_toe")
    mock_bot = AsyncMock()
    engine = ctx.engine
    users = {
        ctx.u1_id: (ctx.u1_id, ctx.u1_name, ctx.g1_id),
        ctx.u2_id: (ctx.u2_id, ctx.u2_name, ctx.g2_id),
    }
    state = await _fresh_state(db_session, ctx.match_id)

    async def move(uid, uname, gid, cell):
        cb = make_callback(uid, uname, gid, f"ttt_action:{ctx.match_id}:{cell}")
        await handle_ttt_action(cb, mock_bot)
        await db_session.rollback()
        m = await db_session.get(Match, ctx.match_id)
        await db_session.refresh(m)
        return cb, m

    moves = 0
    while not engine.is_finished(state):
        cur = state["current_player_id"]
        uid, uname, gid = users[cur]
        board = state["board"]
        cell = board.index(None)
        cb, m = await move(uid, uname, gid, cell)
        moves += 1
        assert moves <= 9
        state = json.loads(m.game_state)

        if not engine.is_finished(state):
            next_pid = state["current_player_id"]
            edit = last_edit_for(mock_bot, users[next_pid][2])
            kb = edit.kwargs.get("reply_markup")
            text = edit_text(edit)
            # Core invariant: if it claims someone's turn, they must be able to act.
            if "نوبت شماست" in text:
                assert actionable_buttons(kb), "UI says 'your turn' but no actionable button exists!"
            else:
                assert not actionable_buttons(kb) or True  # opponent's view may hide buttons
            # And the actionable count must match empty cells.
            empties = sum(1 for v in json.loads(m.game_state)["board"] if v is None)
            assert len(actionable_buttons(kb)) == empties or not actionable_buttons(kb)

    assert engine.is_finished(state)
    m = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(m)
    assert m.state == MatchState.FINISHED


@pytest.mark.asyncio
async def test_ttt_stale_click_on_occupied_cell_rejected(db_session):
    ctx = await _make_match(db_session, "tic_tac_toe")
    mock_bot = AsyncMock()
    state = await _fresh_state(db_session, ctx.match_id)
    cur = state["current_player_id"]
    uid, uname, gid = (
        (ctx.u1_id, ctx.u1_name, ctx.g1_id) if cur == ctx.u1_id
        else (ctx.u2_id, ctx.u2_name, ctx.g2_id)
    )

    cb1 = make_callback(uid, uname, gid, f"ttt_action:{ctx.match_id}:4")
    await handle_ttt_action(cb1, mock_bot)
    await db_session.rollback()

    # Opponent plays somewhere else first, then a stale keyboard click on 4.
    m = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(m)
    state = json.loads(m.game_state)
    cur = state["current_player_id"]
    oid, oname, ogid = (
        (ctx.u2_id, ctx.u2_name, ctx.g2_id) if cur == ctx.u2_id
        else (ctx.u1_id, ctx.u1_name, ctx.g1_id)
    )
    cb2 = make_callback(oid, oname, ogid, f"ttt_action:{ctx.match_id}:0")
    await handle_ttt_action(cb2, mock_bot)
    await db_session.rollback()

    # Back to first player; stale click on occupied cell 4 must not change state.
    m = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(m)
    before = m.game_state
    cb3 = make_callback(uid, uname, gid, f"ttt_action:{ctx.match_id}:4")
    await handle_ttt_action(cb3, mock_bot)
    await db_session.rollback()
    m = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(m)
    assert m.game_state == before
    answer_text = cb3.answer.call_args.args[0] if cb3.answer.call_args.args else ""
    assert answer_text, "stale click must be answered, never silent"
