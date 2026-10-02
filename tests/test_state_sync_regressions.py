"""State-sync regression suite (bug report requirements #1/#2/#4).

Covers the three root-cause fixes:
A. Background state transitions (turn-timeout forfeits) re-render BOTH
   board messages via MaintenanceService + services.game_sync.
B. process_game_action self-heals stale UI: any rejected click re-renders
   the authoritative board and tells the user - manual Refresh is never
   required for normal play.
C. Shared group messages name the current player instead of claiming
   "your turn" to teammates who cannot act.
Plus: Battleship visual language (glassy ░, shot = button turns blue 🟦,
hit 💥, sunk 🔥) on keyboard AND text radar.
"""

import json
import types
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

from sib.database.connection import get_session
from sib.database.models import (
    Game,
    Match,
    MatchPlayer,
    MatchState,
)
from sib.games.battleship.engine import BattleshipEngine
from sib.games.tic_tac_toe.engine import TicTacToeEngine
from sib.handlers.games.common import process_game_action
from sib.services.maintenance.service import MaintenanceService

ENGINE_BY_CODE = {
    "tic_tac_toe": TicTacToeEngine,
    "battleship": BattleshipEngine,
}
NAME_BY_CODE = {
    "tic_tac_toe": " Tic Tac Toe",
    "battleship": "جنگ کشتی‌ها",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _player_stub(uid: int, gid: int, team: str, name: str):
    """Engine-facing stand-in for a MatchPlayer row."""
    return types.SimpleNamespace(
        user_id=uid,
        group_id=gid,
        team=team,
        user=types.SimpleNamespace(display_name=name),
    )


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


async def _start_match(session, game_code: str) -> Match:
    """Create a fresh active match with two players in two groups."""
    from sib.tests.test_parties import create_test_group, create_test_user

    g1 = await create_test_group(session, "گروه همگام ۱")
    g2 = await create_test_group(session, "گروه همگام ۲")
    u1 = await create_test_user(session, "Alice")
    u2 = await create_test_user(session, "Bob")
    from sqlalchemy import select as _select

    game = await session.scalar(_select(Game).where(Game.code == game_code))
    if game is None:
        game = Game(code=game_code, name_fa=NAME_BY_CODE[game_code])
        session.add(game)
        await session.flush()

    match = Match(
        game_id=game.id,
        mode="solo",
        state=MatchState.ACTIVE,
        group1_id=g1.id,
        group2_id=g2.id,
    )
    session.add(match)
    await session.flush()

    mp1 = MatchPlayer(match_id=match.id, user_id=u1.id, group_id=g1.id, team=1)
    mp1.user = u1
    mp2 = MatchPlayer(match_id=match.id, user_id=u2.id, group_id=g2.id, team=2)
    mp2.user = u2
    session.add_all([mp1, mp2])
    await session.flush()

    engine = ENGINE_BY_CODE[game_code]()
    if game_code == "battleship":
        state = engine.create_initial_state(
            [mp1, mp2], mode="solo", fleet_seed_a=7, fleet_seed_b=13
        )
    else:
        state = engine.create_initial_state([mp1, mp2], mode="solo")
    match.game_state = json.dumps(state, ensure_ascii=False)
    match.current_turn_player_id = u1.id
    match.turn_deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    match.group1_message_id = 111
    match.group2_message_id = 222
    await session.flush()
    return match


# ---------------------------------------------------------------------------
# A. Background timeout resolution re-renders both boards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_turn_timeout_forfeit_rerenders_both_boards(db_session):
    match = await _start_match(db_session, "battleship")
    match_id = match.id
    g1, g2 = match.group1_id, match.group2_id
    # Alice never plays -> her turn times out -> forfeit -> boards must update.
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(seconds=5)
    await db_session.commit()

    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)

    MaintenanceService._bot = bot
    try:
        await MaintenanceService.run_once()
    finally:
        MaintenanceService._bot = None

    # DB is authoritative and resolved.
    async with get_session() as session:
        m = await session.get(Match, match_id)
        assert m.state == MatchState.FINISHED
        assert m.result_applied is True

    # Both group board messages were re-rendered as final boards.
    edited_chats = {
        call.kwargs.get("chat_id") for call in bot.edit_message_text.call_args_list
    }
    assert g1 in edited_chats
    assert g2 in edited_chats

    texts = [
        call.args[0] if call.args else call.kwargs.get("text", "")
        for call in bot.edit_message_text.call_args_list
    ]
    assert any("به پایان رسید" in t for t in texts)
    # No dead playable keyboard was rendered: every edit is final (no game actions).
    for call in bot.edit_message_text.call_args_list:
        kb = call.kwargs.get("reply_markup")
        if kb is None:
            continue
        for row in kb.inline_keyboard:
            for button in row:
                assert not button.callback_data.startswith(
                    ("bs_action:", "ttt_action:", "ttt:", "bs:")
                )


# ---------------------------------------------------------------------------
# B. Self-healing on rejected clicks (stale UI never needs manual refresh)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_player_click_self_heals_with_latest_board(db_session):
    match = await _start_match(db_session, "tic_tac_toe")
    match_id = match.id
    g2 = match.group2_id
    await db_session.commit()

    # Bob clicks during Alice's turn -> rejected AND re-rendered for Bob.
    bob = _telegram_user(902, "Bob")
    cb = _make_callback(bob, chat_id=g2)
    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)

    await process_game_action(cb, bot, match_id, {"cell": 0})

    assert cb.answer.called
    answer_text = cb.answer.call_args[0][0]
    assert "نوبت" in answer_text
    assert "آخرین وضعیت بازی بارگذاری شد" in answer_text

    # The board was re-rendered (self-heal) - no Refresh button press needed.
    assert bot.edit_message_text.called
    kwargs = bot.edit_message_text.call_args.kwargs
    assert kwargs.get("chat_id") == g2
    text = bot.edit_message_text.call_args.args[0]
    # Bob's board must NAME Alice (the actual current player).
    assert "Alice" in text


@pytest.mark.asyncio
async def test_click_after_background_forfeit_self_heals(db_session):
    match = await _start_match(db_session, "battleship")
    match_id = match.id
    g2 = match.group2_id
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(seconds=5)
    await db_session.commit()

    # Background resolution happens but boards may fail to update (e.g. the
    # edit API is unreachable); the stale keyboard stays visible. Bob clicks.
    MaintenanceService._bot = None
    await MaintenanceService.run_once()

    bob = _telegram_user(902, "Bob")
    cb = _make_callback(bob, chat_id=g2)
    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)

    await process_game_action(cb, bot, match_id, {"r": 3, "c": 3})

    assert cb.answer.called
    answer_text = cb.answer.call_args[0][0]
    assert "دیگر در جریان نیست" in answer_text
    # Self-heal renders the final board instead of leaving a dead keyboard.
    assert bot.edit_message_text.called
    text = bot.edit_message_text.call_args.args[0]
    assert "به پایان رسید" in text


# ---------------------------------------------------------------------------
# C. Honest turn labels in shared group messages
# ---------------------------------------------------------------------------

def test_shared_board_never_says_your_turn_to_teammate():
    """Team mode: both teammates read the same message; only the current
    player may be told "نوبت شماست" - otherwise the current player is named."""
    engine = TicTacToeEngine()
    state = {
        "game": "tic_tac_toe",
        "mode": "2v2",
        "board": [None] * 9,
        "players": {
            "1": {"symbol": "X", "team": "A", "group_id": -7001, "name": "P1"},
            "2": {"symbol": "O", "team": "A", "group_id": -7001, "name": "P2"},
            "3": {"symbol": "X", "team": "B", "group_id": -7002, "name": "P3"},
            "4": {"symbol": "O", "team": "B", "group_id": -7002, "name": "P4"},
        },
        "current_player_id": 1,
        "finished": False,
        "winner_team": None,
        "is_draw": False,
    }

    # Current player is id=1; viewer is their teammate id=2 in the same group.
    text = engine.render_for_group(state, -7001, 2)
    assert "نوبت شماست" not in text
    assert "نوبت P1 است" in text

    # Even the current player gets the NAME in a shared team message
    # (their teammate reads the same text - never claim "your turn" there).
    text_self = engine.render_for_group(state, -7001, 1)
    assert "نوبت شماست" not in text_self
    assert "نوبت P1 است" in text_self


# ---------------------------------------------------------------------------
# Battleship visual language: state lives in the button itself
# ---------------------------------------------------------------------------

def test_battleship_radar_and_keyboard_share_labels():
    engine = BattleshipEngine()
    players = [
        _player_stub(1, -8001, "A", "P1"),
        _player_stub(2, -8002, "B", "P2"),
    ]
    state = engine.create_initial_state(players, mode="solo", fleet_seed_a=3, fleet_seed_b=5)

    # Pick deterministic cells from the actual seeded fleets.
    enemy_ship = state["teams"]["B"]["ships"][0]
    sunk_cell = tuple(enemy_ship["cells"][0])          # will be a sunk cell
    hit_cell = tuple(state["teams"]["B"]["ships"][1]["cells"][0])  # hit, not sunk
    miss_cell = (0, 0)
    while miss_cell in [tuple(c) for s in state["teams"]["B"]["ships"] for c in s["cells"]]:
        miss_cell = (miss_cell[0], (miss_cell[1] + 1) % 8)

    state["teams"]["A"]["shots_taken"] = {
        f"{miss_cell[0]},{miss_cell[1]}": "miss",
        f"{hit_cell[0]},{hit_cell[1]}": "hit",
        f"{sunk_cell[0]},{sunk_cell[1]}": "hit",
    }
    enemy_ship["sunk"] = True

    text = engine.render_for_group(state, -8001, 1)
    kb = engine.get_keyboard(state, 1, 1)

    # Keyboard buttons only use the board visual language + coordinate labels.
    # Valid button texts: coordinate labels ( , A-H, 1-8) + cell states (·, 🌊, 💥, 🔥)
    valid_labels = {" ", "·", "🌊", "💥", "🔥"} | set("ABCDEFGH12345678")
    for row in kb.inline_keyboard:
        for btn in row:
            assert btn.text in valid_labels

    assert kb.inline_keyboard[miss_cell[0] + 1][miss_cell[1] + 1].text == "🌊"  # miss: wave
    assert kb.inline_keyboard[miss_cell[0] + 1][miss_cell[1] + 1].callback_data == "noop"
    assert kb.inline_keyboard[hit_cell[0] + 1][hit_cell[1] + 1].text == "💥"   # hit
    assert kb.inline_keyboard[sunk_cell[0] + 1][sunk_cell[1] + 1].text == "🔥"  # sunk
    glass_cell = next(
        (r, c) for r in range(8) for c in range(8)
        if f"{r},{c}" not in state["teams"]["A"]["shots_taken"]
    )
    assert kb.inline_keyboard[glass_cell[0] + 1][glass_cell[1] + 1].text == "·"  # empty
    assert kb.inline_keyboard[glass_cell[0] + 1][glass_cell[1] + 1].callback_data.startswith("bs_action:")

    # 3 shot cells -> 61 empty unselected cells (skip header row and label columns).
    empty = sum(
        1 for row_idx in range(1, 9) for btn in kb.inline_keyboard[row_idx][1:]
        if btn.text == "·"
    )
    assert empty == 61

    # The text radar carries the identical grid (visual consistency).
    # Grid lines start with row numbers 1-8 followed by space-separated cells.
    radar_lines = [
        ln for ln in text.splitlines()
        if ln and ln[0] in "12345678" and " " in ln
    ]
    assert len(radar_lines) == 8
    # Each line: "{row_num} {cell} {cell} ..." - split and skip row number
    def get_cell(line: str, col: int) -> str:
        parts = line.split()
        return parts[col + 1]  # skip row number

    assert get_cell(radar_lines[miss_cell[0]], miss_cell[1]) == "🌊"
    assert get_cell(radar_lines[hit_cell[0]], hit_cell[1]) == "💥"
    assert get_cell(radar_lines[sunk_cell[0]], sunk_cell[1]) == "🔥"
    assert get_cell(radar_lines[glass_cell[0]], glass_cell[1]) == "·"
