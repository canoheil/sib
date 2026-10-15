"""Regression tests for the Battleship stale-white-cell bug and, more
generally, for the "DB is authoritative / presentation is best-effort"
contract of game actions.

Root cause analysis (see implementation report):
- A shot was committed to the DB, but the Telegram message edit could
  fail (transient API errors, flood waits, or Markdown parse failures
  caused by unescaped characters in player names). The old code swallowed
  those errors, leaving the rendered board stale: the shot cell visually
  stayed white (◽) even though the shot was recorded.
- Fix: state is committed before any UI work, edits retry and degrade
  gracefully, names are Markdown-escaped, and every board carries a
  refresh button that re-renders from the authoritative DB state.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser

from sib.core.text import escape_markdown, safe_name
from sib.database.models import MatchState
from sib.games.registry import get_game_engine
from sib.handlers.games.battleship import handle_bs_action
from sib.handlers.games.common import handle_game_refresh, process_game_action
from sib.matchmaking.acceptance import AcceptanceService
from sib.matchmaking.matcher import MatchmakingService
from sib.services.party import PartyService
from sib.services.telegram_ui import safe_edit_message_text, with_refresh_button
from sib.tests.test_parties import create_test_group, create_test_user


class FakeBadRequest(TelegramBadRequest):
    """TelegramBadRequest with a simple constructor for tests."""

    def __init__(self, text: str):
        self.message = text
        self.method = None


class FakeRetryAfter(TelegramRetryAfter):
    """TelegramRetryAfter with a simple constructor for tests."""

    def __init__(self, seconds: int):
        self.message = f"flood wait {seconds}"
        self.method = None
        self.retry_after = seconds


async def _start_battleship_match(db_session):
    """Create a fully accepted 1v1 Battleship match and return (match, u1, u2)."""
    g1 = await create_test_group(db_session, "گروه رگرسیون ۱")
    g2 = await create_test_group(db_session, "گروه رگرسیون ۲")
    u1 = await create_test_user(db_session, "بازیکن_یک")
    u2 = await create_test_user(db_session, "بازیکن*دو*")

    p1 = await PartyService.create_party(db_session, g1.id, u1.id, "battleship", "solo")
    p2 = await PartyService.create_party(db_session, g2.id, u2.id, "battleship", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, u1.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, u2.id)
    match = await MatchmakingService.find_and_create_match(db_session, p2.id)
    await AcceptanceService.accept_match(db_session, match.id, u1.id)
    await AcceptanceService.accept_match(db_session, match.id, u2.id)

    engine = get_game_engine("battleship")
    state = engine.create_initial_state(match.players, match.mode)
    match.game_state = json.dumps(state, ensure_ascii=False)
    match.group1_message_id = 111
    match.group2_message_id = 222
    await db_session.commit()
    return match, u1, u2


def _make_callback(user_id: int, data: str, first_name: str = "کاربر") -> CallbackQuery:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name=first_name)
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=-100123, type="supergroup", title="گروه تست")
    cb.message.message_id = 777
    cb.answer = AsyncMock()
    return cb


def _make_bot_with_failing_edits(exc: Exception) -> AsyncMock:
    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(side_effect=exc)
    return bot


def _cell_label(keyboard, r: int, c: int) -> str:
    return keyboard.inline_keyboard[r][c].text


@pytest.mark.asyncio
async def test_battleship_shot_survives_persistent_edit_failure(db_session):
    """The white-cell regression: a recorded shot must stay recorded even
    when every Telegram edit fails."""
    match, u1, u2 = await _start_battleship_match(db_session)
    state = json.loads(match.game_state)
    active_pid = state["current_player_id"]

    bot = _make_bot_with_failing_edits(Exception("network is down"))
    cb = _make_callback(active_pid, f"bs_action:{match.id}:0:0")

    await handle_bs_action(cb, bot)

    # Presentation attempted (and kept retrying) but failed...
    assert bot.edit_message_text.call_count >= 2
    # ...yet the shot itself is committed in the authoritative DB state.
    await db_session.rollback()
    await db_session.refresh(match)
    st = json.loads(match.game_state)
    team = st["players"][str(active_pid)]["team_code"]
    assert "0,0" in st["teams"][team]["shots_taken"]
    assert cb.answer.called


@pytest.mark.asyncio
async def test_battleship_refresh_recovers_stale_board(db_session):
    """After a failed edit, the refresh button must re-render the board
    from DB state, showing the shot result instead of a white cell."""
    match, u1, u2 = await _start_battleship_match(db_session)
    state = json.loads(match.game_state)
    active_pid = state["current_player_id"]

    bot = _make_bot_with_failing_edits(Exception("network is down"))
    cb = _make_callback(active_pid, f"bs_action:{match.id}:0:0")
    await handle_bs_action(cb, bot)
    await db_session.rollback()

    # User taps the refresh button.
    bot2 = AsyncMock()
    refresh_cb = _make_callback(active_pid, f"game_refresh:{match.id}")
    await handle_game_refresh(refresh_cb, bot2)

    assert refresh_cb.answer.called
    assert bot2.edit_message_text.called

    # The re-rendered keyboard must show the shot result at (0, 0).
    kwargs = bot2.edit_message_text.call_args.kwargs
    kb = kwargs.get("reply_markup")
    assert kb is not None
    # Cell (0,0) is at keyboard row 1, col 1 (offset by header + label)
    label = _cell_label(kb, 1, 1)
    assert label in ("💥", "🌊", "🔥")  # the button itself shows the shot outcome
    refresh = kb.inline_keyboard[-1][0]
    assert refresh.callback_data == f"game_refresh:{match.id}"
    assert refresh.style == "success"


@pytest.mark.asyncio
async def test_safe_edit_retries_transient_failures():
    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(
        side_effect=[Exception("timeout"), Exception("reset"), None]
    )

    ok = await safe_edit_message_text(bot, chat_id=1, message_id=2, text="سلام")
    assert ok is True
    assert bot.edit_message_text.call_count == 3


@pytest.mark.asyncio
async def test_safe_edit_flood_wait_is_retried(monkeypatch):
    slept = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("sib.services.telegram_ui.asyncio.sleep", _fake_sleep)

    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(side_effect=[FakeRetryAfter(0), None])

    ok = await safe_edit_message_text(bot, chat_id=1, message_id=2, text="سلام")
    assert ok is True
    assert bot.edit_message_text.call_count == 2
    assert slept  # flood-wait delay was honoured


@pytest.mark.asyncio
async def test_safe_edit_markdown_parse_failure_degrades_gracefully():
    """Markdown parse failures (e.g. bad user names) must not leave the
    board stale: the helper falls back to a plain-text edit."""
    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(
        side_effect=[
            FakeBadRequest("Bad Request: can't parse entities: ..."),
            None,
        ]
    )

    ok = await safe_edit_message_text(bot, chat_id=1, message_id=2, text="نام: a_b")
    assert ok is True
    assert bot.edit_message_text.call_count == 2
    # The fallback call must not request Markdown parsing.
    fallback_kwargs = bot.edit_message_text.call_args.kwargs
    assert fallback_kwargs.get("parse_mode") is None


@pytest.mark.asyncio
async def test_safe_edit_treats_not_modified_as_success():
    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(
        side_effect=FakeBadRequest("Bad Request: message is not modified")
    )
    ok = await safe_edit_message_text(bot, chat_id=1, message_id=2, text="x")
    assert ok is True


def test_player_names_are_markdown_escaped_in_state():
    assert escape_markdown("ali_reza*") == "ali\\_reza\\*"
    assert escape_markdown("a`b[c") == "a\\`b\\[c"
    assert safe_name(None, "بازیکن") == "بازیکن"


@pytest.mark.asyncio
async def test_battleship_state_names_are_escaped(db_session):
    match, u1, u2 = await _start_battleship_match(db_session)
    state = json.loads(match.game_state)
    for pdata in state["players"].values():
        # Names created from users containing _ or * must be escaped.
        assert "\\_" in pdata["name"] or "\\*" in pdata["name"]


@pytest.mark.asyncio
async def test_battleship_keyboard_marks_hits_and_misses(db_session):
    match, u1, u2 = await _start_battleship_match(db_session)
    state = json.loads(match.game_state)
    active_pid = state["current_player_id"]

    bot = AsyncMock()
    cb = _make_callback(active_pid, f"bs_action:{match.id}:0:0")
    await process_game_action(cb, bot, match.id, {"r": 0, "c": 0})

    assert bot.edit_message_text.called

    # Each group renders its own radar: the shooter's group message must
    # show the shot result at (0, 0).
    shooter_group_calls = [
        call
        for call in bot.edit_message_text.call_args_list
        if call.kwargs.get("chat_id") == match.group1_id
    ]
    assert shooter_group_calls
    kb = shooter_group_calls[-1].kwargs.get("reply_markup")
    assert kb is not None
    # Cell (0,0) is at keyboard row 1, col 1 (offset by header + label)
    label = _cell_label(kb, 1, 1)
    assert label in ("💥", "🌊", "🔥")
    # Refresh button present as the recovery path.
    last_row = kb.inline_keyboard[-1]
    assert last_row[0].text == "🔄 بروزرسانی صفحه"
    assert last_row[0].callback_data == f"game_refresh:{match.id}"
    assert last_row[0].style == "success"


@pytest.mark.asyncio
async def test_refresh_rejects_non_participants(db_session):
    match, u1, u2 = await _start_battleship_match(db_session)
    outsider = await create_test_user(db_session, "بیگانه")

    bot = AsyncMock()
    cb = _make_callback(outsider.id, f"game_refresh:{match.id}")
    await handle_game_refresh(cb, bot)
    assert not bot.edit_message_text.called
    assert cb.answer.called


def test_with_refresh_button_is_idempotent():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="noop")]]
    )
    kb2 = with_refresh_button(kb, 7)
    kb3 = with_refresh_button(kb2, 7)
    assert len(kb3.inline_keyboard) == 2
    assert kb3.inline_keyboard[-1][0].callback_data == "game_refresh:7"
    # The recovery row is blue by default - no game renders a transparent
    # refresh button (Battleship passes its own green style explicitly).
    assert kb3.inline_keyboard[-1][0].style == "primary"
