from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, StorageKey

from sib.database.models import AppleTransaction, AppleWallet, User
from sib.handlers import casino
from sib.services import casino_service
from sib.services.apple_wallet import AppleWalletService

PROMPT_MESSAGE_ID = 555
BOT_USER = SimpleNamespace(id=42, is_bot=True, first_name="sib")
PLAYER = SimpleNamespace(id=1, is_bot=False, first_name="test", full_name="Test Player")


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    col = AppleTransaction.__table__.c.id
    old = col.type
    col.type = sa.Integer()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: User.metadata.create_all(c, tables=[User.__table__, AppleWallet.__table__, AppleTransaction.__table__]))
    finally:
        col.type = old
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory.begin() as session:
        session.add(User(id=1, first_name="test"))
        await session.flush()
        await AppleWalletService.earn(session, 1, 10000, reason="test")
    @asynccontextmanager
    async def get_session():
        async with factory.begin() as session:
            yield session
    monkeypatch.setattr(casino, "get_session", get_session)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_pending():
    casino._PENDING_PROMPTS.clear()
    casino._ACTIVE_USERS.clear()
    yield
    casino._PENDING_PROMPTS.clear()
    casino._ACTIVE_USERS.clear()


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


def _prompt_message(*, message_id=PROMPT_MESSAGE_ID):
    return SimpleNamespace(message_id=message_id, from_user=BOT_USER, delete=AsyncMock())


def _bet_message(text, *, reply_to=..., answer=None, answer_dice=None):
    """A player message; ``reply_to`` defaults to the bot prompt."""
    if reply_to is ...:
        reply_to = _prompt_message()
    return SimpleNamespace(
        message_id=777,
        from_user=PLAYER,
        text=text,
        reply_to_message=reply_to,
        answer=answer or AsyncMock(),
        reply=AsyncMock(),
        answer_dice=answer_dice or AsyncMock(
            return_value=SimpleNamespace(dice=SimpleNamespace(value=1))
        ),
    )


def _callback(key="dice", *, message=None):
    sent = SimpleNamespace(message_id=PROMPT_MESSAGE_ID)
    message = message or SimpleNamespace(answer=AsyncMock(return_value=sent))
    return SimpleNamespace(
        data=f"casino_game:{key}",
        from_user=PLAYER,
        message=message,
        answer=AsyncMock(),
    )


async def _enter_waiting_for_bet(state, emoji="🎲", prompt_message_id=PROMPT_MESSAGE_ID):
    await state.set_state(casino.CasinoStates.waiting_for_bet)
    await state.update_data(emoji=emoji, prompt_message_id=prompt_message_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,emoji", casino.GAMES.items())
async def test_callback(key, emoji, state):
    sent = SimpleNamespace(message_id=PROMPT_MESSAGE_ID)
    callback = _callback(key, message=SimpleNamespace(answer=AsyncMock(return_value=sent)))
    await casino.choose_game(callback, state)
    assert await state.get_state() == casino.CasinoStates.waiting_for_bet.state
    data = await state.get_data()
    assert data["emoji"] == emoji
    assert data["prompt_message_id"] == PROMPT_MESSAGE_ID
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu(state):
    message = SimpleNamespace(answer_photo=AsyncMock())
    await casino.casino_menu(message, state)
    kwargs = message.answer_photo.await_args.kwargs
    assert len(kwargs["reply_markup"].inline_keyboard[0]) == 4
    assert casino.ASSET.exists()


# ---------------------------------------------------------------------------
# Prompt tagging, anti-spam clicks, reply-to enforcement and cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_mentions_the_player(state):
    callback = _callback("dice")
    await casino.choose_game(callback, state)
    prompt_text = callback.message.answer.await_args.args[0]
    assert "tg://user?id=1" in prompt_text
    assert "Test Player" in prompt_text
    assert "چقدر می‌خوای شرط بندی کنی؟" in prompt_text
    assert prompt_text.startswith("[")


@pytest.mark.asyncio
async def test_prompt_mention_survives_missing_full_name(state):
    sender = SimpleNamespace(id=5, is_bot=False, first_name="solo", full_name=None)
    assert "tg://user?id=5" in casino.mention(sender)
    assert casino.mention(SimpleNamespace(id=None, full_name="x")) == "x"


@pytest.mark.asyncio
async def test_second_click_in_waiting_for_bet_sends_no_prompt(state):
    await _enter_waiting_for_bet(state)
    callback = _callback("slots")
    await casino.choose_game(callback, state)
    callback.answer.assert_awaited_once()
    callback.message.answer.assert_not_awaited()
    # The pending prompt is untouched: same game, same prompt id.
    data = await state.get_data()
    assert data["emoji"] == "🎲"
    assert data["prompt_message_id"] == PROMPT_MESSAGE_ID


@pytest.mark.asyncio
async def test_spam_clicks_send_exactly_one_prompt(state):
    sent = SimpleNamespace(message_id=PROMPT_MESSAGE_ID)
    message = SimpleNamespace(answer=AsyncMock(return_value=sent))
    calls = [_callback(key, message=message) for key in ("dice", "dice", "darts", "slots")]
    for callback in calls:
        await casino.choose_game(callback, state)
    message.answer.assert_awaited_once()
    for callback in calls:
        callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_clicks_send_exactly_one_prompt(state):
    """Two taps landing in the same tick both see an empty FSM state."""
    import asyncio

    sent = SimpleNamespace(message_id=PROMPT_MESSAGE_ID)
    message = SimpleNamespace(answer=AsyncMock(return_value=sent))
    await asyncio.gather(*(
        casino.choose_game(_callback("dice", message=message), state) for _ in range(5)
    ))
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_prompt_send_clears_the_state_and_the_guard(state):
    message = SimpleNamespace(answer=AsyncMock(side_effect=RuntimeError("network")))
    await casino.choose_game(_callback("dice", message=message), state)
    assert casino._PENDING_PROMPTS == set()
    assert await state.get_state() is None
    # The player can simply tap again.
    callback = _callback("dice")
    await casino.choose_game(callback, state)
    callback.message.answer.assert_awaited_once()
    assert (await state.get_data())["prompt_message_id"] == PROMPT_MESSAGE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji,value,won", [(e, v, w) for e in casino.GAMES.values() for v, w in (
    [(1, True), (22, True), (43, True), (64, True), (10, False), (14, False)]
    if e == "🎰"
    else [(6, True), (1, False), (5, False)]
)])
@pytest.mark.parametrize("bet_text", ["۱۰", "10"])
async def test_round(db, state, emoji, value, won, bet_text, monkeypatch):
    await _enter_waiting_for_bet(state, emoji=emoji)
    dice = AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=value)))
    message = _bet_message(bet_text, answer_dice=dice)
    async def check_animation_delay(delay):
        dice.assert_awaited_once_with(emoji=emoji, reply_to_message_id=777)
        message.answer.assert_not_awaited()
    sleep = AsyncMock(side_effect=check_animation_delay)
    monkeypatch.setattr(casino.asyncio, "sleep", sleep)
    await casino.place_bet(message, state)
    sleep.assert_awaited_once_with(4.0 if emoji == "🎰" else 3.5)
    # The prompt the player replied to is deleted once the bet is accepted.
    message.reply_to_message.delete.assert_awaited_once()
    payout = (150 if emoji == "🎰" else 50) if won else 0
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 10 + payout
    result_text = message.answer.await_args.args[0]
    assert ("بردی" if won else "باختی") in result_text
    if emoji == "🎰":
        assert ("🎰 جکپات زدی!" if won else f"🎰 اسلات {value} آورد") in result_text
        if not won:
            assert "سه‌تای یکسان" in result_text
    else:
        played = {
            "🎯": f"دارت به {value} خورد",
            "🎳": f"بولینگ {value} پین انداخت",
        }.get(emoji, f"تاس {value} آورد")
        assert played in result_text
        # A loss explains that only an exact 6 wins.
        assert ("برای برد باید ۶ بیاد" in result_text) is not won
    if won:
        assert f"{payout} سیب گرفتی" in result_text
    assert await state.get_state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("key,emoji", [(k, v) for k, v in casino.GAMES.items() if v != "🎰"])
async def test_five_is_a_loss_for_dice_games(db, state, key, emoji, monkeypatch):
    """Rolling a 5 used to win; now only an exact 6 pays."""
    await _enter_waiting_for_bet(state, emoji=emoji)
    message = _bet_message(
        "10",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=5))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    result_text = message.answer.await_args.args[0]
    assert "باختی" in result_text
    assert "برای برد باید ۶ بیاد" in result_text
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 10


@pytest.mark.asyncio
@pytest.mark.parametrize("key,emoji", [(k, v) for k, v in casino.GAMES.items() if v != "🎰"])
async def test_six_wins_five_times_the_bet(db, state, key, emoji, monkeypatch):
    await _enter_waiting_for_bet(state, emoji=emoji)
    message = _bet_message(
        "100",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=6))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    result_text = message.answer.await_args.args[0]
    assert "بردی" in result_text
    assert "500 سیب گرفتی" in result_text
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 100 + 500


@pytest.mark.asyncio
async def test_slots_matching_combination_pays_fifteen_times(db, state, monkeypatch):
    await _enter_waiting_for_bet(state, emoji="🎰")
    message = _bet_message(
        "100",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=22))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    result_text = message.answer.await_args.args[0]
    assert "🎰 جکپات زدی!" in result_text
    assert "1500 سیب گرفتی" in result_text
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 100 + 1500


@pytest.mark.asyncio
async def test_slots_non_matching_value_loses(db, state, monkeypatch):
    await _enter_waiting_for_bet(state, emoji="🎰")
    message = _bet_message(
        "100",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=10))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    result_text = message.answer.await_args.args[0]
    assert "باختی" in result_text
    assert "🎰 اسلات 10 آورد" in result_text
    assert "بردی" not in result_text
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 100


@pytest.mark.asyncio
async def test_menu_explains_the_odds(state):
    message = SimpleNamespace(answer_photo=AsyncMock())
    await casino.casino_menu(message, state)
    caption = message.answer_photo.await_args.kwargs["caption"]
    assert "فقط با ۶ برنده" in caption
    assert "×5" in caption
    assert "سه‌تای یکسان" in caption
    assert "×15" in caption


@pytest.mark.asyncio
async def test_bet_without_reply_is_refused(db, state):
    await _enter_waiting_for_bet(state)
    message = _bet_message("10", reply_to=None)
    await casino.place_bet(message, state)
    message.reply.assert_awaited_once_with("لطفاً روی پیام بات ریپلای کنید و مبلغ را بنویسید.")
    message.answer_dice.assert_not_awaited()
    message.answer.assert_not_awaited()
    # The state survives so the player can reply to the prompt properly.
    assert await state.get_state() == casino.CasinoStates.waiting_for_bet.state
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000


@pytest.mark.asyncio
async def test_reply_to_a_different_message_is_refused(db, state):
    await _enter_waiting_for_bet(state)
    message = _bet_message("10", reply_to=_prompt_message(message_id=1234))
    await casino.place_bet(message, state)
    message.reply.assert_awaited_once_with(casino.REPLY_TO_PROMPT_REQUIRED)
    message.answer_dice.assert_not_awaited()
    message.reply_to_message.delete.assert_not_awaited()
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000


@pytest.mark.asyncio
async def test_cleanup_only_deletes_bot_messages():
    """A stale state can never make the bot delete a player's own message."""
    player_message = SimpleNamespace(
        message_id=PROMPT_MESSAGE_ID,
        from_user=SimpleNamespace(id=99, is_bot=False, first_name="other"),
        delete=AsyncMock(),
    )
    await casino._delete_prompt(_bet_message("10", reply_to=player_message))
    player_message.delete.assert_not_awaited()
    prompt = _prompt_message()
    await casino._delete_prompt(_bet_message("10", reply_to=prompt))
    prompt.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_refusal_falls_back_to_answer_without_reply_helper(db, state):
    await _enter_waiting_for_bet(state)
    message = _bet_message("10", reply_to=None)
    message.reply = None
    await casino.place_bet(message, state)
    message.answer.assert_awaited_once_with(casino.REPLY_TO_PROMPT_REQUIRED)


@pytest.mark.asyncio
async def test_prompt_deleted_before_the_dice_is_sent(db, state, monkeypatch):
    await _enter_waiting_for_bet(state)
    order = []
    message = _bet_message("10")
    message.reply_to_message.delete = AsyncMock(side_effect=lambda: order.append("delete"))
    message.answer_dice = AsyncMock(
        side_effect=lambda **kwargs: order.append("dice")
        or SimpleNamespace(dice=SimpleNamespace(value=1))
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    assert order == ["delete", "dice"]


@pytest.mark.asyncio
async def test_prompt_delete_failure_does_not_abort_the_round(db, state, monkeypatch):
    await _enter_waiting_for_bet(state)
    message = _bet_message("10")
    message.reply_to_message.delete = AsyncMock(side_effect=RuntimeError("message can't be deleted"))
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    message.answer_dice.assert_awaited_once()
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 10


@pytest.mark.asyncio
async def test_prompt_is_kept_when_the_bet_is_refused(db, state):
    """A cooldown / low balance is retryable, so the prompt must survive."""
    await _enter_waiting_for_bet(state)
    message = _bet_message("10")
    async with db.begin() as session:
        assert (await casino_service.validate_bet(session, 1, 10))["status"] == "started"
    await casino.place_bet(message, state)
    message.reply_to_message.delete.assert_not_awaited()
    message.answer_dice.assert_not_awaited()
    assert "ثانیه دیگر تلاش کن" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_state_without_prompt_id_still_requires_a_reply(db, state):
    await _enter_waiting_for_bet(state, prompt_message_id=None)
    message = _bet_message("10", reply_to=None)
    await casino.place_bet(message, state)
    message.reply.assert_awaited_once_with(casino.REPLY_TO_PROMPT_REQUIRED)
    message.answer_dice.assert_not_awaited()


@pytest.mark.asyncio
async def test_refusals_and_cooldown(db):
    async with db.begin() as session:
        for amount in (0, 9, 5001, -1, 1.5, True):
            assert (await casino_service.validate_bet(session, 1, amount))["status"] == "invalid_bet"
        assert (await casino_service.validate_bet(session, 1, 5000))["status"] == "started"
    async with db.begin() as session:
        assert (await casino_service.validate_bet(session, 1, 10))["status"] == "cooldown"
        assert (await casino_service.validate_bet(session, 999, 10))["status"] == "not_found"
    async with db.begin() as session:
        session.add(User(id=2, first_name="poor"))
        await session.flush()
        assert (await casino_service.validate_bet(session, 2, 10))["status"] == "insufficient_funds"


@pytest.mark.parametrize("value", [1, 22, 43, 64])
def test_slots_winning_values(value):
    """A matching combination pays 15x, floored to whole apples."""
    result = casino_service.calculate_result("🎰", value, 11)
    assert result["is_win"] is True
    assert result["payout"] == 165


@pytest.mark.parametrize("value", [2, 10, 20, 33, 63])
def test_slots_losing_values(value):
    result = casino_service.calculate_result("🎰", value, 100)
    assert result["is_win"] is False
    assert result["payout"] == 0


@pytest.mark.parametrize("emoji", ["🎲", "🎯", "🎳"])
@pytest.mark.parametrize("value,won", [(1, False), (3, False), (4, False), (5, False), (6, True)])
def test_dice_only_six_wins(emoji, value, won):
    """Dice, darts and bowling win on an exact 6 only — 5x the wager."""
    result = casino_service.calculate_result(emoji, value, 100)
    assert result["is_win"] is won
    assert result["payout"] == (500 if won else 0)


@pytest.mark.parametrize("bet", [10, 13, 5000])
def test_payouts_are_whole_apples(bet):
    for emoji, multiplier in (("🎲", 5), ("🎰", 15)):
        winning = max(casino_service.SLOTS_WINNING_VALUES) if emoji == "🎰" else 6
        payout = casino_service.calculate_result(emoji, winning, bet)["payout"]
        assert isinstance(payout, int)
        assert payout == int(bet * multiplier)


def test_win_rules_are_pinned():
    assert casino_service.DICE_WINNING_VALUES == {6}
    assert casino_service.SLOTS_WINNING_VALUES == {1, 22, 43, 64}
    assert casino_service.DICE_MULTIPLIER == 5
    assert casino_service.SLOTS_MULTIPLIER == 15


@pytest.mark.asyncio
async def test_send_failure_refunds(db, state):
    await state.update_data(emoji="🎲", prompt_message_id=PROMPT_MESSAGE_ID)
    message = _bet_message("10", answer_dice=AsyncMock(side_effect=RuntimeError("send failed")))
    await casino.place_bet(message, state)
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/casino", "قمارخانه", "قمارخونه", "قمار خانه", "قمار خونه", "قمار", "کازینو"])
async def test_menu_filters(text):
    from aiogram import Bot
    from aiogram.types import Chat, Message, User as TelegramUser
    from datetime import datetime
    message = Message(message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"), from_user=TelegramUser(id=1, is_bot=False, first_name="test"), text=text)
    bot = Bot("123456:ABCdef_123456789012345678901234567")
    try:
        matches = [await h.check(message, bot=bot) for h in casino.router.message.handlers if h.callback is casino.casino_menu]
        assert any(match for match, _ in matches)
    finally:
        await bot.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["abc", "1.5", "-10", "۵۰۰۱", "9"])
async def test_invalid_inputs_clear_state(db, state, text):
    await state.update_data(emoji="🎲", prompt_message_id=PROMPT_MESSAGE_ID)
    await state.set_state(casino.CasinoStates.waiting_for_bet)
    message = _bet_message(text, answer_dice=AsyncMock())
    await casino.place_bet(message, state)
    message.answer_dice.assert_not_awaited()
    assert await state.get_state() is None
    # An invalid amount keeps the prompt alive so it can be answered again.
    message.reply_to_message.delete.assert_not_awaited()
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000


@pytest.mark.asyncio
async def test_cooldown_exact_boundary(db, monkeypatch):
    async with db.begin() as session:
        assert (await casino_service.validate_bet(session, 1, 10))["status"] == "started"
    async with db() as session:
        last = await session.scalar(sa.select(AppleTransaction).where(
            AppleTransaction.user_id == 1, AppleTransaction.reason == casino_service.BET_REASON
        ))
        played_at = last.created_at
    now = AsyncMock(return_value=played_at + timedelta(seconds=9))
    monkeypatch.setattr(casino_service, "_database_now", now)
    async with db.begin() as session:
        refused = await casino_service.validate_bet(session, 1, 10)
        assert refused["status"] == "cooldown"
        assert "1 ثانیه" in refused["error"]
    now.return_value = played_at + timedelta(seconds=10)
    async with db.begin() as session:
        assert (await casino_service.validate_bet(session, 1, 10))["status"] == "started"
