"""Regression tests for the G2 friendly RPS and High Card reveal flow."""

import itertools
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    Message,
    User as TelegramUser,
)

from sib.database.models import Match, MatchState
from sib.games.high_card.engine import HighCardEngine
from sib.games.rock_paper_scissors.engine import RockPaperScissorsEngine
from sib.handlers.games.high_card import handle_high_card_action
from sib.handlers.games.rps import handle_rps_action
from sib.services.friendly import FriendlyMatchService
from sib.services.game_sync import sync_match_messages
from sib.services.party import PartyService
from sib.services.timeout.service import TimeoutService
from sib.tests.test_parties import create_test_group, create_test_user


async def _make_friendly_match(db_session, game_code: str):
    group = await create_test_group(db_session, f"گروه رفیقانه {game_code}")
    player_a = await create_test_user(db_session, "بازیکن الف")
    player_b = await create_test_user(db_session, "بازیکن ب")
    party = await PartyService.create_friendly_party(
        db_session, group.id, player_a.id, game_code, "solo"
    )
    party = await PartyService.join_party(db_session, party.id, player_b.id)
    await db_session.commit()

    match = await FriendlyMatchService.start_friendly_match(
        db_session, party.id, player_a.id
    )
    match.group1_message_id = 900
    match.group2_message_id = 900
    await db_session.commit()
    return group.id, player_a.id, player_b.id, match.id


def _callback(data: str, user_id: int, group_id: int, message_id: int = 900):
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.from_user = TelegramUser(id=user_id, is_bot=False, first_name="بازیکن")
    callback.message = MagicMock(spec=Message)
    callback.message.chat = TelegramChat(
        id=group_id, type="supergroup", title="گروه رفیقانه"
    )
    callback.message.message_id = message_id
    callback.answer = AsyncMock()
    return callback


def _callbacks(keyboard):
    return [
        (button.text, button.callback_data)
        for row in keyboard.inline_keyboard
        for button in row
    ]


def _friendly_rps_state():
    return {
        "game": "rock_paper_scissors",
        "friendly": True,
        "round": 1,
        "total_rounds": 3,
        "finished": False,
        "players": {
            "101": {
                "team": 1,
                "group_id": -1001,
                "name": "بازیکن الف",
                "choice": None,
                "locked": False,
                "score": 0,
            },
            "102": {
                "team": 2,
                "group_id": 1001002,
                "name": "بازیکن ب",
                "choice": None,
                "locked": False,
                "score": 0,
            },
        },
    }


def test_rps_choice_not_leaked_in_keyboard():
    engine = RockPaperScissorsEngine()
    state = _friendly_rps_state()
    initial_a = _callbacks(engine.get_keyboard(state, 42, 101))
    initial_b = _callbacks(engine.get_keyboard(state, 42, 102))

    engine.apply_action(state, 101, {"choice": "rock"})
    after_first_choice_a = _callbacks(engine.get_keyboard(state, 42, 101))
    after_first_choice_b = _callbacks(engine.get_keyboard(state, 42, 102))

    assert initial_a == initial_b == after_first_choice_a == after_first_choice_b
    assert {callback for _, callback in after_first_choice_a} == {
        "rps_action:42:rock",
        "rps_action:42:paper",
        "rps_action:42:scissors",
    }
    assert all("انتخاب شما:" not in text for text, _ in after_first_choice_a)
    assert all(callback != "noop" for _, callback in after_first_choice_a)


@pytest.mark.asyncio
async def test_rps_reveals_both_on_second_commit(db_session):
    group_id, player_a_id, player_b_id, match_id = await _make_friendly_match(
        db_session, "rock_paper_scissors"
    )
    bot = AsyncMock()

    first = _callback(f"rps_action:{match_id}:rock", player_a_id, group_id)
    await handle_rps_action(first, bot)

    # First commit changes only authoritative state and the clicker's toast.
    bot.edit_message_text.assert_not_awaited()
    first.answer.assert_awaited_once_with(
        "✅ انتخاب شما ثبت شد. منتظر حریف...", show_alert=False
    )
    await db_session.rollback()
    match = await db_session.get(Match, match_id)
    first_state = json.loads(match.game_state)
    assert first_state["players"][str(player_a_id)]["choice"] == "rock"
    assert first_state["players"][str(player_a_id)]["locked"] is True

    duplicate = _callback(f"rps_action:{match_id}:paper", player_a_id, group_id)
    await handle_rps_action(duplicate, bot)
    duplicate.answer.assert_awaited_once_with(None, show_alert=False)
    bot.edit_message_text.assert_not_awaited()

    second = _callback(f"rps_action:{match_id}:scissors", player_b_id, group_id)
    await handle_rps_action(second, bot)

    bot.edit_message_text.assert_awaited_once()
    revealed_text = bot.edit_message_text.await_args.args[0]
    assert "بازیکن الف" in revealed_text and "🗿 سنگ" in revealed_text
    assert "بازیکن ب" in revealed_text and "✂️ قیچی" in revealed_text
    assert "این راند را برد" in revealed_text


def _fixed_high_card_draws(monkeypatch, *cards):
    draws = itertools.cycle(cards)
    monkeypatch.setattr(
        HighCardEngine,
        "_draw_card",
        lambda self, action=None: next(draws),
    )


@pytest.mark.asyncio
async def test_high_card_draw_sends_ephemeral_message(db_session, monkeypatch):
    group_id, player_a_id, _player_b_id, match_id = await _make_friendly_match(
        db_session, "high_card"
    )
    _fixed_high_card_draws(monkeypatch, (14, "♠️"))
    bot = AsyncMock()
    callback = _callback(f"hc_action:{match_id}:draw", player_a_id, group_id)

    await handle_high_card_action(callback, bot)

    bot.send_message.assert_awaited_once()
    sent = bot.send_message.await_args.kwargs
    assert sent["chat_id"] == group_id
    assert sent["receiver_user_id"] == player_a_id
    assert "تک / آس (A)" in sent["text"]
    assert sent["parse_mode"] is None


@pytest.mark.asyncio
async def test_high_card_swap_sends_new_ephemeral_message(db_session, monkeypatch):
    group_id, player_a_id, _player_b_id, match_id = await _make_friendly_match(
        db_session, "high_card"
    )
    _fixed_high_card_draws(monkeypatch, (14, "♠️"), (2, "♥️"))
    bot = AsyncMock()

    draw = _callback(f"hc_action:{match_id}:draw", player_a_id, group_id)
    await handle_high_card_action(draw, bot)
    swap = _callback(f"hc_action:{match_id}:swap", player_a_id, group_id)
    await handle_high_card_action(swap, bot)

    assert bot.send_message.await_count == 2
    first_card, swapped_card = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert "تک / آس (A)" in first_card
    assert "۲" in swapped_card and "♥️" in swapped_card
    assert bot.send_message.await_args_list[1].kwargs["receiver_user_id"] == player_a_id


@pytest.mark.asyncio
async def test_high_card_group_message_neutral_during_draw(db_session, monkeypatch):
    group_id, player_a_id, _player_b_id, match_id = await _make_friendly_match(
        db_session, "high_card"
    )
    _fixed_high_card_draws(monkeypatch, (14, "♠️"))
    bot = AsyncMock()
    callback = _callback(f"hc_action:{match_id}:draw", player_a_id, group_id)

    await handle_high_card_action(callback, bot)

    group_text = bot.edit_message_text.await_args.args[0]
    assert "بازیکن الف کارت خود را کشید" in group_text
    assert "تک / آس (A)" not in group_text
    assert "♠️" not in group_text
    assert "کارت شما:" not in group_text


@pytest.mark.asyncio
async def test_high_card_reveals_both_after_lock(db_session, monkeypatch):
    group_id, player_a_id, player_b_id, match_id = await _make_friendly_match(
        db_session, "high_card"
    )
    _fixed_high_card_draws(monkeypatch, (14, "♠️"), (10, "♦️"))
    bot = AsyncMock()

    async def act(user_id: int, action_type: str):
        callback = _callback(f"hc_action:{match_id}:{action_type}", user_id, group_id)
        await handle_high_card_action(callback, bot)
        return callback

    await act(player_a_id, "draw")
    await act(player_b_id, "draw")
    await act(player_a_id, "keep")

    before_second_lock = bot.edit_message_text.await_args.args[0]
    assert "تک / آس (A)" not in before_second_lock
    assert "♦️" not in before_second_lock

    await act(player_b_id, "keep")

    showdown = bot.edit_message_text.await_args.args[0]
    assert "تک / آس (A)" in showdown
    assert "۱۰" in showdown
    assert "بازیکن الف" in showdown and "بازیکن ب" in showdown
    assert "برنده این راند شد" in showdown


@pytest.mark.asyncio
async def test_high_card_fallback_to_alert_if_ephemeral_fails(db_session, monkeypatch):
    group_id, player_a_id, _player_b_id, match_id = await _make_friendly_match(
        db_session, "high_card"
    )
    _fixed_high_card_draws(monkeypatch, (14, "♠️"))
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=group_id, text="کارت خصوصی"),
        message="ephemeral delivery rejected",
    )
    callback = _callback(f"hc_action:{match_id}:draw", player_a_id, group_id)

    await handle_high_card_action(callback, bot)

    callback.answer.assert_awaited_once()
    fallback_text = callback.answer.await_args.args[0]
    assert "کارت شما:" in fallback_text
    assert "تک / آس (A)" in fallback_text
    assert callback.answer.await_args.kwargs["show_alert"] is True
    # A failed private send does not prevent the neutral group progress edit.
    assert bot.edit_message_text.await_count == 1
    group_text = bot.edit_message_text.await_args.args[0]
    assert "تک / آس (A)" not in group_text and "♠️" not in group_text

    await db_session.rollback()
    match = await db_session.get(Match, match_id)
    committed_state = json.loads(match.game_state)
    assert committed_state["players"][str(player_a_id)]["card_rank"] == 14


@pytest.mark.asyncio
async def test_high_card_timeout_cleans_up_without_revealing_partial_hand(
    db_session, monkeypatch
):
    group_id, player_a_id, _player_b_id, match_id = await _make_friendly_match(
        db_session, "high_card"
    )
    _fixed_high_card_draws(monkeypatch, (14, "♠️"))
    bot = AsyncMock()

    await handle_high_card_action(
        _callback(f"hc_action:{match_id}:draw", player_a_id, group_id), bot
    )
    await handle_high_card_action(
        _callback(f"hc_action:{match_id}:keep", player_a_id, group_id), bot
    )

    await db_session.rollback()
    match = await FriendlyMatchService.load_match(db_session, match_id)
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    processed = await TimeoutService.process_turn_timeouts(db_session)
    assert processed == [match_id]
    await db_session.rollback()
    finished = await FriendlyMatchService.load_match(db_session, match_id)
    assert finished.state == MatchState.FINISHED

    bot.edit_message_text.reset_mock()
    await sync_match_messages(bot, finished)
    final_text = bot.edit_message_text.await_args.args[0]
    assert "تک / آس (A)" not in final_text
    assert "♠️" not in final_text
    callback_data = [
        button.callback_data
        for row in bot.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"friendly_rematch:{match_id}" in callback_data
