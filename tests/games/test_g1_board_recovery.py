"""G1 transport and real committed-state duplicate-click regressions."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import EditMessageText

from sib.services import telegram_ui
from sib.handlers.games import common
from sib.database.models import Match
from sib.tests.test_gameplay_sync import _make_match, make_callback


def target(bot):
    return SimpleNamespace(bot=bot, message=SimpleNamespace(
        chat=SimpleNamespace(id=-100), message_id=123))


def bad(text="temporary error"):
    return TelegramBadRequest(method=EditMessageText(text="board", chat_id=-100, message_id=123), message=text)


@pytest.mark.parametrize("error", [bad(), TelegramForbiddenError(method=None, message="forbidden"),
                                   TelegramNetworkError(method=None, message="network")])
async def test_safe_edit_board_retries_on_telegram_error(monkeypatch, error):
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_ui.asyncio, "sleep", sleep)
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [error, error, True]
    assert await telegram_ui.safe_edit_board(target(bot), "board")
    assert [c.args[0] for c in sleep.call_args_list] == [0.5, 1.0]


async def test_safe_edit_board_swallows_not_modified():
    bot = AsyncMock()
    bot.edit_message_text.side_effect = bad("message is not modified")
    assert await telegram_ui.safe_edit_board(target(bot), "board")
    assert bot.edit_message_text.await_count == 1


async def test_safe_edit_board_honors_retry_after(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(telegram_ui.asyncio, "sleep", sleep)
    bot = AsyncMock()
    flood = TelegramRetryAfter(method=None, message="wait", retry_after=42)
    bot.edit_message_text.side_effect = [flood, flood, True]
    assert await telegram_ui.safe_edit_board(target(bot), "board", max_retries=0)
    assert [c.args[0] for c in sleep.call_args_list] == [42.0, 42.0]


async def test_safe_edit_board_returns_false_after_max_retries(monkeypatch, caplog):
    monkeypatch.setattr(telegram_ui.asyncio, "sleep", AsyncMock())
    bot = AsyncMock()
    bot.edit_message_text.side_effect = bad()
    assert not await telegram_ui.safe_edit_board(target(bot), "board")
    assert bot.edit_message_text.await_count == 4
    assert "123" in caplog.text


@pytest.mark.parametrize("game", ["connect_four", "tic_tac_toe", "rock_paper_scissors", "high_card", "battleship"])
async def test_duplicate_move_re_renders_board(db_session, monkeypatch, game):
    ctx = await _make_match(db_session, game)
    match = await db_session.get(Match, ctx.match_id)
    state = json.loads(match.game_state)
    actor = state.get("current_player_id") or ctx.u1_id
    group = ctx.g1_id if actor == ctx.u1_id else ctx.g2_id
    action = {"connect_four": {"col": 0}, "tic_tac_toe": {"cell": 0},
              "rock_paper_scissors": {"choice": "rock"}, "high_card": {"choice": "keep"},
              "battleship": {"r": 0, "c": 0}}[game]
    bot = AsyncMock()
    cb = make_callback(actor, "player", group, "unused")
    monkeypatch.setattr(telegram_ui.asyncio, "sleep", AsyncMock())
    # High Card's initial draw precedes its lockable keep/swap decision.
    if game == "high_card":
        await common.process_game_action(cb, bot, ctx.match_id, {"draw": True})
    bot.edit_message_text.side_effect = bad("transport unavailable")
    bot.send_message.side_effect = RuntimeError("send unavailable too")
    await common.process_game_action(cb, bot, ctx.match_id, action)
    await db_session.refresh(match)
    committed = match.game_state
    assert json.loads(committed) != state
    bot.edit_message_text.reset_mock(side_effect=True)
    cb.answer.reset_mock()
    await common.process_game_action(cb, bot, ctx.match_id, action)
    await db_session.refresh(match)
    assert match.game_state == committed
    expected = ctx.engine.render_for_group(json.loads(committed), group, actor)
    assert bot.edit_message_text.await_args.args[0] == expected
    # Duplicate click is informational, never an error popup.
    assert cb.answer.await_args.kwargs["show_alert"] is False
    assert cb.answer.await_args.args[0] in (common.DUPLICATE_MOVE_MESSAGE, common.STALE_TURN_MESSAGE)


@pytest.mark.parametrize("reason", ["شما قبلاً تصمیم خود را ثبت کرده‌اید.", "هنوز نوبت شما نشده است."])
async def test_duplicate_move_alert_is_informational(reason):
    feedback = common._duplicate_feedback(reason, True)
    assert feedback
    assert not any(word in feedback for word in ("خطا", "نامعتبر", "اشتباه", "❌", "!"))
    assert common._duplicate_feedback(reason, False) is None


async def test_board_fallback_returns_replacement_id(monkeypatch):
    monkeypatch.setattr(telegram_ui.asyncio, "sleep", AsyncMock())
    bot = AsyncMock()
    bot.edit_message_text.side_effect = bad()
    bot.send_message.return_value = SimpleNamespace(message_id=456)
    assert await common._edit_game_screen(bot, -100, 123, "current board") == 456
    assert bot.send_message.await_args.kwargs["text"] == "current board"


async def test_high_card_duplicate_draw_does_not_show_error_alert(db_session, monkeypatch):
    ctx = await _make_match(db_session, "high_card")
    cb = make_callback(ctx.u1_id, "player", ctx.g1_id, "unused")
    bot = AsyncMock()
    monkeypatch.setattr(telegram_ui.asyncio, "sleep", AsyncMock())
    bot.edit_message_text.side_effect = bad()
    bot.send_message.side_effect = RuntimeError("offline")
    await common.process_game_action(cb, bot, ctx.match_id, {"draw": True})
    match = await db_session.get(Match, ctx.match_id)
    await db_session.refresh(match)
    committed = match.game_state
    bot.edit_message_text.reset_mock(side_effect=True)
    cb.answer.reset_mock()
    await common.process_game_action(cb, bot, ctx.match_id, {"draw": True})
    assert cb.answer.await_args.kwargs["show_alert"] is False
    assert cb.answer.await_args.args[0] == common.DUPLICATE_MOVE_MESSAGE
    assert bot.edit_message_text.await_args.args[0] == ctx.engine.render_for_group(
        json.loads(committed), ctx.g1_id, ctx.u1_id)
