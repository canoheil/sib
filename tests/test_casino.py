import asyncio
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
    casino._CHAIN_LOCKS.clear()
    casino._CHAIN_GATES.clear()
    yield
    casino._PENDING_PROMPTS.clear()
    casino._ACTIVE_USERS.clear()
    casino._CHAIN_LOCKS.clear()
    casino._CHAIN_GATES.clear()


async def _window_times_out(gate, timeout):
    """Default test clock: nobody answers, so every window expires at once."""
    return False


async def _window_answered(gate, timeout):
    """A window that a (simulated) tap resolves immediately."""
    gate.set()
    return True


@pytest.fixture(autouse=True)
def _instant_chain_windows(monkeypatch):
    """Never burn ten real seconds per round; the window is tested on its own."""
    monkeypatch.setattr(casino, "_wait_for_decision", _window_times_out, raising=False)


@pytest.fixture
def state():
    return FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))


def _prompt_message(*, message_id=PROMPT_MESSAGE_ID):
    return SimpleNamespace(message_id=message_id, from_user=BOT_USER, delete=AsyncMock())


def _sent_message(message_id=900):
    """A bot message the handler may edit (the chain prompt lives here)."""
    return SimpleNamespace(message_id=message_id, edit_text=AsyncMock())


def _bet_message(text, *, reply_to=..., answer=None, answer_dice=None):
    """A player message; ``reply_to`` defaults to the bot prompt."""
    if reply_to is ...:
        reply_to = _prompt_message()
    return SimpleNamespace(
        message_id=777,
        from_user=PLAYER,
        text=text,
        reply_to_message=reply_to,
        answer=answer or AsyncMock(return_value=_sent_message()),
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
    assert prompt_text.startswith("\u200f[")  # RLM pins the paragraph to RTL


@pytest.mark.asyncio
async def test_prompt_mention_survives_missing_full_name(state):
    sender = SimpleNamespace(id=5, is_bot=False, first_name="solo", full_name=None)
    assert "tg://user?id=5" in casino.mention(sender)
    assert casino.mention(SimpleNamespace(id=None, full_name="x")) == "\u200ex\u200e"


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
    sent = _bet_message(bet_text, answer_dice=dice)
    message = sent
    # Every chain prompt is one more `answer` call; keep them in order so the
    # assertions can talk about "the rung that is live right now".
    prompts = []
    def _answer(text, **kwargs):
        prompt = _sent_message()
        prompts.append((text, prompt))
        return prompt
    message.answer = AsyncMock(side_effect=_answer)
    prompt = message.answer.return_value
    async def check_animation_delay(delay):
        """Only the two pre-roll sleeps are checked here; a winning round then
        enters the chain window, whose countdown sleeps come afterwards."""
        if dice.await_count:
            return  # countdown tick of the «زنجیره سیبی» window
        if delay == casino.ANTICIPATION_PAUSE:
            # Beat 1 landed before the roll: anticipation only, no dice yet.
            dice.assert_not_awaited()
            assert message.answer.await_args.args[0] == casino.ANTICIPATION[emoji]
            return
        # Beat 2: the animation had already been sent when its own delay ran.
        dice.assert_awaited_once_with(emoji=emoji, reply_to_message_id=777)
        assert message.answer.await_args.args[0] == casino.ANTICIPATION[emoji]
    sleep = AsyncMock(side_effect=check_animation_delay)
    monkeypatch.setattr(casino.asyncio, "sleep", sleep)
    await casino.place_bet(message, state)
    # Two beats of presentation around one animation: the anticipation pause,
    # then the animation delay itself. A winning round adds the chain window's
    # countdown sleeps AFTER those, and nothing before them.
    delays = [call.args[0] for call in sleep.await_args_list]
    # A round only ever gates on its two presentation beats; the chain window
    # waits on its own gate instead of sleeping (see the chain tests).
    assert delays == [casino.ANTICIPATION_PAUSE, 4.0 if emoji == "🎰" else 3.5]
    # The prompt the player replied to is deleted once the bet is accepted.
    message.reply_to_message.delete.assert_awaited_once()
    payout = (150 if emoji == "🎰" else 50) if won else 0
    # «زنجیره سیبی»: a winnable pot is OFFERED first and the player could not
    # answer, so the 10s window expires and auto-banks it. The wallet must end
    # up exactly where the plain payout would have left it.
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 10 + payout
    result_text = message.answer.await_args.args[0]
    if won and casino_service.chain_available(emoji, 10, 1):
        assert result_text == casino.chain_prompt(payout)
        # The window expired, so the live prompt carries the auto-bank receipt.
        assert prompts[-1][1].edit_text.await_args.args[0] == casino.banked_text(payout)
    else:
        assert result_text == casino.verdict_text(emoji, value, payout)
    # Slots never leak the raw 1-64 value: the player only sees fruit. (Wins
    # are covered by test_slots_loss_never_shows_the_number, because the
    # jackpot line legitimately carries the ×15 and the payout digits.)
    if emoji == "🎰" and not won:
        assert str(value) not in result_text
    # No parenthetical rule reminders in the chat any more.
    assert "(برای برد باید" not in result_text
    assert await state.get_state() is None


# ---------------------------------------------------------------------------
# Idea B — the three-beat round: anticipation → roll → verdict
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("key,emoji", casino.GAMES.items())
async def test_anticipation_message_precedes_the_roll(db, state, key, emoji, monkeypatch):
    """Beat 1 is its own message, and it is sent before the animation."""
    await _enter_waiting_for_bet(state, emoji=emoji)
    order = []
    sent = _sent_message()
    message = _bet_message("10")
    message.answer = AsyncMock(
        side_effect=lambda *a, **k: (order.append(("answer", a[0])), sent)[1]
    )
    message.answer_dice = AsyncMock(
        side_effect=lambda **k: order.append(("dice", None))
        or SimpleNamespace(dice=SimpleNamespace(value=6))
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    assert order[0] == ("answer", casino.ANTICIPATION[emoji])
    assert order[1] == ("dice", None)
    if emoji == "🎰":
        # 6 is three different fruits on the slots: a plain loss, no chain.
        assert order[-1] == ("answer", casino.verdict_text(emoji, 6, 0))
    else:
        # A win opens the chain: the last message is the bank/push prompt.
        assert order[-1][1].startswith("\u200f🎉 بردی! پات فعلی: 50 سیب.")


@pytest.mark.asyncio
async def test_anticipation_failure_never_aborts_the_round(db, state, monkeypatch):
    """Presentation cannot cost the player their wager: after the debit the
    anticipation line failing must still roll, settle and pay."""
    await _enter_waiting_for_bet(state)
    calls = {"count": 0}
    def answer(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:  # the anticipation line itself
            raise RuntimeError("not enough rights")
        return SimpleNamespace()
    message = _bet_message("100")
    message.answer = AsyncMock(side_effect=answer)
    message.answer_dice = AsyncMock(
        return_value=SimpleNamespace(dice=SimpleNamespace(value=6))
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    message.answer_dice.assert_awaited_once()
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 100 + 500
    assert await state.get_state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji,value,expected,payout", [
    # The four tiers the design review signed off, end to end.
    ("🎲", 1, "😭 باختی! تاس 1 آورد. زمین خوردی!", 0),
    ("🎲", 5, "😱 نزدیک بود! تاس 5 آورد. فقط یه دونه کم آوردی!", 0),
    # Wins open the chain instead of printing a receipt; the pot is the tier.
    ("🎲", 6, None, 500),
    ("🎰", 22, None, 1500),
])
async def test_verdict_tiers_end_to_end(db, state, emoji, value, expected, payout, monkeypatch):
    """Fumble / so-close / win / jackpot: one mocked value each, one verdict."""
    await _enter_waiting_for_bet(state, emoji=emoji)
    message = _bet_message(
        "100",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=value))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    if expected is None:
        assert message.answer.await_args.args[0] == casino.chain_prompt(payout)
    else:
        assert message.answer.await_args.args[0] == expected
    # The presentation never touches the economy: only the existing bet and
    # payout reasons are written.
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 100 + payout


@pytest.mark.parametrize("value", sorted(casino_service.SLOTS_WINNING_VALUES))
def test_every_slot_jackpot_is_three_matching_reels(value):
    """The reel decode must agree with the payout rule it decorates."""
    assert len(set(casino._slot_reels(value))) == 1
    assert not casino.slots_two_of_a_kind(value)


@pytest.mark.parametrize("value", range(1, 65))
def test_slot_reel_decode_matches_the_winning_set(value):
    """Reels are rebuilt purely from the 1-64 result, never guessed."""
    reels = casino._slot_reels(value)
    assert all(0 <= reel <= 3 for reel in reels)
    assert (len(set(reels)) == 1) == (value in casino_service.SLOTS_WINNING_VALUES)


def test_two_of_a_kind_count():
    """36 of the 64 results are a real «one fruit short»; 24 are not."""
    two = [v for v in range(1, 65) if casino.slots_two_of_a_kind(v)]
    assert len(two) == 36
    assert 5 in two and 9 in two
    assert 10 not in two and 14 not in two and 55 not in two


async def _play_win_to_the_top(monkeypatch, message, state, *, emoji, value=6):
    """Drive one winning chain: push at every rung, then let it auto-bank.

    Returns the pot the ladder ends on. Pushing is simulated by calling the
    same code path the button uses (``casino._await_chain_decision`` is the
    window; here we simply run the decision loop forward by answering the
    prompt ourselves), which keeps the test free of Telegram objects.
    """
    await casino.place_bet(message, state)
    pot = None
    while await state.get_state() == casino.CasinoStates.waiting_for_chain_decision.state:
        data = await state.get_data()
        pot = data["chain_pot"]
        if casino.casino_service.chain_available(emoji, data["chain_stake"], data["chain_step"]):
            # Push: same bookkeeping as the button, then roll the next rung.
            await state.clear()
            await state.update_data(
                emoji=emoji,
                chain_token=None,
                chain_stake=data["chain_stake"],
                chain_step=data["chain_step"] + 1,
                chain_game_id=data["chain_game_id"],
            )
            await casino._chain_round(
                message, state,
                user_id=message.from_user.id, emoji=emoji,
                stake=data["chain_stake"], game_id=data["chain_game_id"],
                step=data["chain_step"] + 1,
            )
        else:
            break
    return pot


def test_win_lines_are_natural():
    assert casino.win_line("🎲", 6) == "تاس 6 آورد"
    assert casino.win_line("🎯", 6) == "دارت به 6 خورد"
    assert casino.win_line("🎳", 6) == "بولینگ 6 پین انداخت"
    assert casino.win_line("🎰", 22) == "🎰 جکپات زدی"


@pytest.mark.parametrize("emoji,value,expected", [
    ("🎲", 1, "😭 باختی! تاس 1 آورد. زمین خوردی!"),
    ("🎲", 2, "😭 باختی! تاس 2 آورد. زمین خوردی!"),
    ("🎯", 1, "😭 باختی! دارت به بیرون خورد. دستت لرزید!"),
    ("🎯", 2, "😭 باختی! دارت به 2 خورد. دستت لرزید!"),
    ("🎳", 2, "😭 باختی! بولینگ 2 پین انداخت. پرت شد!"),
    ("🎰", 10, "🎰 اسلات‌ها جور درنیومدن... باختی."),
    ("🎰", 55, "🎰 اسلات‌ها جور درنیومدن... باختی."),
])
def test_fumble_verdicts_are_dramatic(emoji, value, expected):
    """Values 1-2 never had a chance: the «fumble» tier."""
    text = casino.verdict_text(emoji, value, 0)
    assert text == expected
    assert "(برای برد" not in text
    if emoji == "🎰":
        assert str(value) not in text


@pytest.mark.parametrize("emoji,value,expected", [
    # The gap is spoken honestly: a 3 is never sold as "one short".
    ("🎲", 3, "😱 نزدیک بود! تاس 3 آورد. سه تا کم آوردی!"),
    ("🎲", 4, "😱 نزدیک بود! تاس 4 آورد. دو تا کم آوردی!"),
    ("🎲", 5, "😱 نزدیک بود! تاس 5 آورد. فقط یه دونه کم آوردی!"),
    ("🎯", 5, "😱 نزدیک بود! دارت به 5 خورد. یک خط تا هدف فاصله داشتی!"),
    ("🎯", 3, "😱 نزدیک بود! دارت به 3 خورد. لبه‌ی تخته بود!"),
    ("🎳", 5, "😱 نزدیک بود! بولینگ 5 پین انداخت. یکی مونده بود!"),
])
def test_so_close_verdicts_are_dramatic(emoji, value, expected):
    """A losing 3-5 is a genuine near miss: the «so close» tier."""
    text = casino.verdict_text(emoji, value, 0)
    assert text == expected
    assert text.startswith("😱")
    assert "بردی" not in text
    assert "(برای برد" not in text


@pytest.mark.parametrize("emoji", ["🎲", "🎯", "🎳"])
def test_every_near_miss_speaks_its_own_gap(emoji):
    """Each losing face 3-5 gets a distinct, truthful line."""
    lines = {value: casino.verdict_text(emoji, value, 0) for value in (3, 4, 5)}
    assert len(set(lines.values())) == 3
    for value, line in lines.items():
        assert str(value) in line
        assert "بردی" not in line


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [5, 9, 64])
async def test_slots_loss_never_shows_the_number(db, state, value, monkeypatch):
    """A 55 must not appear: the player watched fruit, not a number."""
    await _enter_waiting_for_bet(state, emoji="🎰")
    message = _bet_message(
        "100",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=value))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    result_text = message.answer.await_args.args[0]
    if value in casino_service.SLOTS_WINNING_VALUES:
        # A jackpot opens the chain: the prompt names the pot, not the value.
        assert result_text == casino.chain_prompt(1500)
        assert "15 برابر" in casino.verdict_text("🎰", value, 1500)
    else:
        # 5 and 9 are two-of-a-kind, 64 is not: the verdicts must differ.
        assert result_text == (
            "🍒 خیلی نزدیک بود! فقط یه میوه کم داشتی... باختی."
            if casino.slots_two_of_a_kind(value)
            else "🎰 اسلات‌ها جور درنیومدن... باختی."
        )
        assert str(value) not in result_text


@pytest.mark.asyncio
async def test_darts_one_misses_the_board(db, state, monkeypatch):
    """A 1 on darts reads like a miss, not «دارت به 1 خورد»."""
    await _enter_waiting_for_bet(state, emoji="🎯")
    message = _bet_message(
        "10",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=1))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    assert message.answer.await_args.args[0] == (
        "😭 باختی! دارت به بیرون خورد. دستت لرزید!"
    )


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
    assert result_text == casino.verdict_text(emoji, 5, 0)
    assert result_text.startswith("😱 نزدیک بود!")
    assert "5" in result_text
    assert "6" not in result_text and "۶" not in result_text
    assert "برای برد" not in result_text
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 10


@pytest.mark.asyncio
@pytest.mark.parametrize("key,emoji", [(k, v) for k, v in casino.GAMES.items() if v != "🎰"])
async def test_six_wins_five_times_the_bet(db, state, key, emoji, monkeypatch):
    """A first win offers the chain; banking it pays exactly the ×5 payout."""
    await _enter_waiting_for_bet(state, emoji=emoji)
    message = _bet_message(
        "100",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=6))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    # Beat 3 is a decision now, not a receipt.
    prompt_kwargs = message.answer.await_args
    assert prompt_kwargs.args[0] == casino.chain_prompt(500)
    keyboard = prompt_kwargs.kwargs["reply_markup"].inline_keyboard[0]
    assert keyboard[0].style == "success" and "برداشت 500" in keyboard[0].text
    assert keyboard[1].style == "danger" and "ادامه" in keyboard[1].text
    # The player never answered, so the 10s window expired and auto-banked it:
    # the pot is credited exactly once and the prompt says so.
    prompt = message.answer.return_value
    assert prompt.edit_text.await_args.args[0] == casino.banked_text(500)
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 100 + 500
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_slots_matching_combination_pays_fifteen_times(db, state, monkeypatch):
    await _enter_waiting_for_bet(state, emoji="🎰")
    message = _bet_message(
        "100",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=22))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    prompt = message.answer.await_args
    # The ×15 pot is offered, then auto-banked when the window expires.
    assert prompt.args[0] == casino.chain_prompt(1500)
    assert "22" not in prompt.args[0]
    assert message.answer.return_value.edit_text.await_args.args[0] == casino.banked_text(1500)
    async with db() as session:
        assert await AppleWalletService.get_balance(session, 1) == 10000 - 100 + 1500


@pytest.mark.asyncio
async def test_slots_non_matching_value_loses(db, state, monkeypatch):
    await _enter_waiting_for_bet(state, emoji="🎰")
    message = _bet_message(
        "100",
        answer_dice=AsyncMock(return_value=SimpleNamespace(dice=SimpleNamespace(value=14))),
    )
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    await casino.place_bet(message, state)
    result_text = message.answer.await_args.args[0]
    # 14 is three different fruits: nothing was close, so no «so close» line.
    assert result_text == "🎰 اسلات‌ها جور درنیومدن... باختی."
    # The raw slot number must never be shown; only the fruit animation is.
    assert "14" not in result_text
    assert "بردی" not in result_text
    assert "برای برد" not in result_text
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


# ---------------------------------------------------------------------------
# «زنجیره سیبی» (Apple Chain) — CASINO-THRILL idea A
# ---------------------------------------------------------------------------

TOKEN = "game123.1"


def _chain_state(**overrides):
    """A player mid-chain, exactly as ``_await_chain_decision`` leaves them."""
    data = {
        "emoji": "🎲",
        "chain_owner": PLAYER.id,
        "chain_token": TOKEN,
        "chain_pot": 500,
        "chain_stake": 100,
        "chain_step": 1,
        "chain_game_id": "game123",
    }
    data.update(overrides)
    return data


async def _arm_chain(state, **overrides):
    await state.set_state(casino.CasinoStates.waiting_for_chain_decision)
    await state.update_data(**_chain_state(**overrides))


def _chain_callback(prefix, token=TOKEN, *, user=PLAYER, message=None):
    """The prompt message doubles as the round source: a push answers *in it*."""
    if message is None:
        message = _sent_message()
        message.chat = SimpleNamespace(id=1)
        message.answer = AsyncMock(return_value=_sent_message())
        message.answer_dice = AsyncMock(
            return_value=SimpleNamespace(message_id=1001, dice=SimpleNamespace(value=6))
        )
    return SimpleNamespace(
        data=f"{prefix}:{token}",
        from_user=user,
        message=message,
        answer=AsyncMock(),
    )


def _dice_returning(*values):
    """``answer_dice`` yields one result per call, in order."""
    queue = list(values)
    return AsyncMock(
        side_effect=lambda **kwargs: SimpleNamespace(
            message_id=1000 + len(values) - len(queue),
            dice=SimpleNamespace(value=queue.pop(0)),
        )
    )


async def _balance(db):
    async with db() as session:
        return await AppleWalletService.get_balance(session, 1)


# --- the ladder itself ------------------------------------------------------

def test_ladder_can_never_beat_the_first_rung():
    """Pushing is never worth MORE than banking: the house edge survives.

    This is the invariant the whole idea rests on. If somebody edits the
    ladder, this fails before the economy does.
    """
    p = {"dice": 1 / 6, "slots": 4 / 64}
    for family, ladder in casino_service.CHAIN_LADDERS.items():
        base = p[family] * ladder[0]
        for step, multiplier in enumerate(ladder, start=1):
            ev = p[family] ** step * multiplier
            # Rung 1 IS the base payout; every push after it must sit below.
            assert ev <= base + 1e-12, (family, step, ev, base)


def test_pushing_is_a_real_bet_not_a_punishment():
    """The top rung must keep most of the base return.

    A ladder that collapses (e.g. x120 on a 1-in-6 game, an earlier draft) makes
    «push» a guaranteed donation: technically EV-safe, but the risk would be
    dishonest drama. Every rung has to stay within a fair band of base RTP.
    """
    p = {"dice": 1 / 6, "slots": 4 / 64}
    for family, ladder in casino_service.CHAIN_LADDERS.items():
        base = p[family] * ladder[0]
        rtp_by_step = [p[family] ** step * m for step, m in enumerate(ladder, 1)]
        # Monotone decline, and never below half of the first rung's return.
        assert rtp_by_step == sorted(rtp_by_step, reverse=True)
        assert min(rtp_by_step) >= 0.5 * base, (family, rtp_by_step)
        # A push must still be a big jump in pot size to feel worth the risk.
        assert ladder[1] >= 3 * ladder[0]


def test_ladder_starts_at_the_existing_payout():
    """The first rung is the advertised ×5 / ×15 — no silent repricing."""
    assert casino_service.CHAIN_LADDERS["dice"][0] == casino_service.DICE_MULTIPLIER
    assert casino_service.CHAIN_LADDERS["slots"][0] == casino_service.SLOTS_MULTIPLIER
    assert casino_service.chain_pot("🎲", 100, 1) == 500
    assert casino_service.chain_pot("🎰", 100, 1) == 1500


@pytest.mark.parametrize("emoji", ["🎲", "🎯", "🎳", "🎰"])
def test_ladder_is_capped_and_bounded(emoji):
    """A deep chain is clamped to its family's own last rung, never extrapolated."""
    family = casino_service.chain_family(emoji)
    depth = len(casino_service.CHAIN_LADDERS[family])
    top = casino_service.chain_pot(emoji, 5000, depth)
    assert casino_service.chain_pot(emoji, 5000, 99) == top
    assert top <= casino_service.CHAIN_POT_CAP
    # One rung past the table is refused outright.
    assert casino_service.chain_available(emoji, 5000, depth) is False
    assert casino_service.chain_available(emoji, 5000, depth - 1) is True


def test_push_is_refused_at_the_top_rung():
    dice_depth = len(casino_service.CHAIN_LADDERS["dice"])
    slots_depth = len(casino_service.CHAIN_LADDERS["slots"])
    assert casino_service.chain_available("🎲", 100, 1) is True
    assert casino_service.chain_available("🎲", 100, dice_depth - 1) is True
    assert casino_service.chain_available("🎲", 100, dice_depth) is False
    # Slots stops sooner: 1-in-16 needs a shorter ladder to stay honest.
    assert casino_service.chain_available("🎰", 100, slots_depth) is False
    assert slots_depth < dice_depth


def test_tiny_pots_are_not_worth_a_prompt():
    """A 10-apple bet cannot reach the 50-apple prompt floor at rung one."""
    assert casino_service.chain_pot("🎲", 10, 1) == 50
    assert casino_service.chain_available("🎲", 9, 1) is False


# --- the prompt ------------------------------------------------------------

def test_prompt_and_keyboard_say_what_will_happen():
    prompt = casino.chain_prompt(500)
    assert "پات فعلی: 500 سیب" in prompt
    assert f"{casino.CHAIN_DECISION_SECONDS} ثانیه" in prompt
    assert "خودکار" in prompt  # the auto-bank promise is explicit
    keyboard = casino.chain_keyboard(TOKEN, 500).inline_keyboard[0]
    assert keyboard[0].style == "success"
    assert keyboard[0].callback_data == f"{casino.CHAIN_BANK_PREFIX}:{TOKEN}"
    assert keyboard[1].style == "danger"
    assert keyboard[1].callback_data == f"{casino.CHAIN_PUSH_PREFIX}:{TOKEN}"


def test_top_rung_offers_no_push_button():
    """The ladder has no rung above the last: do not offer one."""
    keyboard = casino.chain_keyboard(TOKEN, 12000, can_push=False).inline_keyboard[0]
    assert len(keyboard) == 1
    assert keyboard[0].style == "success"
    assert "بالاتر از این پله" in casino.chain_prompt(12000, can_push=False)


def test_countdown_text_keeps_the_contract():
    text = casino.chain_decision_text(500, 4)
    assert "۴" in text or "4" in text
    assert "ثانیه مانده" in text


# --- the four required flows -------------------------------------------------

@pytest.mark.asyncio
async def test_win_then_timeout_auto_banks(db, state, monkeypatch):
    """Win → pot created → nobody answers → the pot is credited anyway."""
    await _enter_waiting_for_bet(state, emoji="🎲")
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    message = _bet_message("100", answer_dice=_dice_returning(6))
    await casino.place_bet(message, state)

    prompt = message.answer.return_value
    assert message.answer.await_args.args[0] == casino.chain_prompt(500)
    assert prompt.edit_text.await_args.args[0] == casino.banked_text(500)
    assert await state.get_state() is None
    assert await _balance(db) == 10000 - 100 + 500


@pytest.mark.asyncio
async def test_win_push_win_then_bank_multiplies_the_pot(db, state, monkeypatch):
    """Win → push → win → bigger pot → bank. The wallet sees one credit."""
    monkeypatch.setattr(casino, "_wait_for_decision", _window_answered)
    await _enter_waiting_for_bet(state, emoji="🎲")
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    message = _bet_message("100", answer_dice=_dice_returning(6, 6))
    await casino.place_bet(message, state)
    assert await _balance(db) == 10000 - 100  # nothing paid yet

    live = (await state.get_data())["chain_token"]
    push = _chain_callback(casino.CHAIN_PUSH_PREFIX, token=live)
    await casino.chain_push(push, state)
    # The second rung is worth ladder[1] = 10x on the original stake, and it is
    # the round source (`push.message`) that answers with it.
    assert casino_service.chain_pot("🎲", 100, 2) == 2800
    assert push.message.answer.await_args.args[0] == casino.chain_prompt(2800)
    data = await state.get_data()
    assert data["chain_step"] == 2 and data["chain_pot"] == 2800
    assert await _balance(db) == 10000 - 100

    bank = _chain_callback(casino.CHAIN_BANK_PREFIX, token=data["chain_token"])
    await casino.chain_bank(bank, state)
    bank.message.edit_text.assert_awaited_once_with(casino.banked_text(2800))
    assert await _balance(db) == 10000 - 100 + 2800
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_win_push_loss_burns_the_pot_and_the_wallet_is_untouched(db, state, monkeypatch):
    """Win → push → loss: the pot is gone, but no second debit happens."""
    monkeypatch.setattr(casino, "_wait_for_decision", _window_answered)
    await _enter_waiting_for_bet(state, emoji="🎲")
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    message = _bet_message("100", answer_dice=_dice_returning(6, 2))
    await casino.place_bet(message, state)

    live = (await state.get_data())["chain_token"]
    push = _chain_callback(casino.CHAIN_PUSH_PREFIX, token=live)
    push.message.answer = AsyncMock(return_value=_sent_message())
    push.message.answer_dice = _dice_returning(2)  # the push roll fails
    await casino.chain_push(push, state)
    # Rung 2 risked rung 1's 500 apples, and the second roll lost.
    burned = push.message.answer.await_args.args[0]
    assert casino.chain_burned_text(500) in burned
    assert "این بار نیامد" in burned
    assert await state.get_state() is None
    # Only the original stake ever left the wallet.
    assert await _balance(db) == 10000 - 100


@pytest.mark.asyncio
async def test_bank_pays_once_and_says_so(db, state, monkeypatch):
    await _arm_chain(state)
    bank = _chain_callback(casino.CHAIN_BANK_PREFIX)
    await casino.chain_bank(bank, state)
    assert await _balance(db) == 10000 + 500
    bank.message.edit_text.assert_awaited_once_with(casino.banked_text(500))
    bank.answer.assert_awaited_once_with("💰 500 سیب واریز شد.")


# --- double clicks, stale rungs, bystanders ---------------------------------

@pytest.mark.asyncio
async def test_double_click_banks_exactly_once(db, state):
    """Two presses of «برداشت» must not mint two credits."""
    await _arm_chain(state)
    first = _chain_callback(casino.CHAIN_BANK_PREFIX)
    second = _chain_callback(casino.CHAIN_BANK_PREFIX)
    await casino.chain_bank(first, state)
    await casino.chain_bank(second, state)
    assert await _balance(db) == 10000 + 500
    second.message.edit_text.assert_not_awaited()
    second.answer.assert_awaited_once()
    assert second.answer.await_args.args[0] == "این دست تموم شد."


@pytest.mark.asyncio
async def test_a_stale_rung_cannot_be_banked(db, state):
    """The button from rung 1 must die as soon as rung 2 is live."""
    await _arm_chain(state, chain_token="game123.2", chain_pot=1000)
    stale = _chain_callback(casino.CHAIN_BANK_PREFIX, token=TOKEN)
    await casino.chain_bank(stale, state)
    assert await _balance(db) == 10000
    # The live rung is untouched: the player can still bank it.
    live = _chain_callback(casino.CHAIN_BANK_PREFIX, token="game123.2")
    await casino.chain_bank(live, state)
    assert await _balance(db) == 10000 + 1000


@pytest.mark.asyncio
async def test_push_twice_rolls_only_once(db, state, monkeypatch):
    """A rapid second «ادامه بده» finds no live token and must not roll again."""
    await _arm_chain(state)
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    live = (await state.get_data())["chain_token"]
    first = _chain_callback(casino.CHAIN_PUSH_PREFIX, token=live)
    second = _chain_callback(casino.CHAIN_PUSH_PREFIX, token=live)
    first.message.answer_dice = _dice_returning(6)
    second.message.answer_dice = _dice_returning(6)
    await casino.chain_push(first, state)
    await casino.chain_push(second, state)
    first.message.answer_dice.assert_awaited_once()
    second.message.answer_dice.assert_not_awaited()
    assert second.answer.await_args.args[0] == "این دست تموم شد."


@pytest.mark.asyncio
async def test_auto_bank_after_an_explicit_bank_is_a_no_op(db, state, monkeypatch):
    """The countdown that was already running when the player tapped must not
    pay a second time: its token is gone, so it simply returns."""
    await _arm_chain(state)
    bank = _chain_callback(casino.CHAIN_BANK_PREFIX)
    await casino.chain_bank(bank, state)
    prompt = _sent_message()
    await casino._auto_bank(state, prompt, user_id=PLAYER.id, token=TOKEN, pot=500)
    prompt.edit_text.assert_not_awaited()
    assert await _balance(db) == 10000 + 500


@pytest.mark.asyncio
async def test_a_bystander_cannot_settle_someone_elses_chain(db, state):
    """Group members can see (and tap) the buttons; only the owner may settle."""
    await _arm_chain(state)
    stranger = SimpleNamespace(id=2, is_bot=False, first_name="other")
    for prefix in (casino.CHAIN_BANK_PREFIX, casino.CHAIN_PUSH_PREFIX):
        callback = _chain_callback(prefix, user=stranger)
        handler = casino.chain_bank if prefix == casino.CHAIN_BANK_PREFIX else casino.chain_push
        await handler(callback, state)
        callback.message.edit_text.assert_not_awaited()
        assert callback.answer.await_args.args[0] == "این دست مال تو نیست."
    assert await _balance(db) == 10000
    assert (await state.get_data())["chain_token"] == TOKEN


@pytest.mark.asyncio
async def test_push_is_refused_when_the_button_should_not_exist(db, state, monkeypatch):
    await _arm_chain(state, chain_step=casino_service.CHAIN_MAX_STEPS)
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    push = _chain_callback(casino.CHAIN_PUSH_PREFIX)
    await casino.chain_push(push, state)
    push.message.answer_dice.assert_not_awaited()
    assert push.answer.await_args.args[0] == "این دست تموم شد."


@pytest.mark.asyncio
async def test_push_with_corrupt_state_clears_and_refuses(db, state, monkeypatch):
    await _arm_chain(state, chain_stake=0)
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    push = _chain_callback(casino.CHAIN_PUSH_PREFIX)
    await casino.chain_push(push, state)
    push.message.answer_dice.assert_not_awaited()
    assert push.answer.await_args.kwargs.get("show_alert") is True
    assert await state.get_state() is None


# --- failure paths ---------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_dice_failure_banks_the_pot_instead_of_losing_it(db, state, monkeypatch):
    """A Telegram failure mid-chain must never vaporise a won pot."""
    await _arm_chain(state)
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    message = _bet_message("100")
    message.answer_dice = AsyncMock(side_effect=RuntimeError("send failed"))
    await casino._chain_round(
        message, state, user_id=PLAYER.id, emoji="🎲", stake=100, game_id="game123", step=2
    )
    # Rung 2 risked rung 1's pot: a failed send must return exactly that.
    assert await _balance(db) == 10000 + 500
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_chain_round_can_credit_an_idempotent_key(db, state, monkeypatch):
    """The bank key is deterministic, so a retried settlement cannot double-pay."""
    key = casino._derive_round_id("game123", 2)
    assert key == casino._derive_round_id("game123", 2)
    assert key != casino._derive_round_id("game123", 3)
    assert casino._derive_round_id("game123", 1) != "game123"
    await casino._credit(1, 1000, key, "casino_payout")
    await casino._credit(1, 1000, key, "casino_payout")
    assert await _balance(db) == 11000


# --- the window itself: a real gate, a real deadline ------------------------

@pytest.mark.asyncio
async def test_window_closes_on_the_clock_and_banks(db, state, monkeypatch):
    """Drive the real `_wait_for_decision`: nobody taps, the deadline fires."""
    # Drive the REAL clock: replace the fixture's instant timeout with one that
    # waits on the (never-opened) gate just like production does.
    monkeypatch.setattr(casino, "_wait_for_decision", casino._REAL_WAIT_FOR_DECISION)
    monkeypatch.setattr(casino, "CHAIN_DECISION_SECONDS", 0.05)
    monkeypatch.setattr(casino, "CHAIN_COUNTDOWN_EDITS", 2)
    await _enter_waiting_for_bet(state, emoji="🎲")
    message = _bet_message("100", answer_dice=_dice_returning(6))
    await casino.place_bet(message, state)
    assert message.answer.return_value.edit_text.await_args.args[0] == casino.banked_text(500)
    assert await _balance(db) == 10000 - 100 + 500


@pytest.mark.asyncio
async def test_window_returns_when_the_player_answers(db, state, monkeypatch):
    """A tap while the clock runs must win: the window returns, the callback pays."""
    monkeypatch.setattr(casino, "CHAIN_DECISION_SECONDS", 0.2)
    await _arm_chain(state)
    source = _sent_message()
    source.chat = SimpleNamespace(id=1)
    source.answer = AsyncMock(return_value=_sent_message())
    window = asyncio.create_task(
        casino._await_chain_decision(
            state, source, PLAYER.id, emoji="🎲", stake=100,
            game_id="game123", step=1, pot=500,
        )
    )
    # Let the window really start waiting on its gate.
    await asyncio.sleep(0.01)
    bank = _chain_callback(casino.CHAIN_BANK_PREFIX, token="game123.1")
    await casino.chain_bank(bank, state)
    await window  # returns immediately: the gate was opened by the bank
    # The bank credited once and the window returned without a second credit.
    assert await _balance(db) == 10000 + 500
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_countdown_edits_are_best_effort(monkeypatch, state):
    """A rate-limited caption edit must never stop the auto-bank."""
    monkeypatch.setattr(casino, "_wait_for_decision", casino._REAL_WAIT_FOR_DECISION)
    monkeypatch.setattr(casino, "CHAIN_DECISION_SECONDS", 0.06)
    monkeypatch.setattr(casino, "CHAIN_COUNTDOWN_EDITS", 2)
    await _arm_chain(state)
    source = _sent_message()
    source.chat = SimpleNamespace(id=1)
    prompt = _sent_message()
    prompt.edit_text = AsyncMock(side_effect=RuntimeError("Too Many Requests"))
    source.answer = AsyncMock(return_value=prompt)
    banked = AsyncMock()
    monkeypatch.setattr(casino, "_credit", banked)
    await casino._await_chain_decision(
        state, source, PLAYER.id, emoji="🎲", stake=100,
        game_id="game123", step=1, pot=500,
    )
    banked.assert_awaited_once()
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_prompt_send_failure_banks_instead_of_stranding_the_pot(db, state, monkeypatch):
    """No prompt means no decision: the already-won pot must still be paid."""
    await _enter_waiting_for_bet(state, emoji="🎲")
    monkeypatch.setattr(casino.asyncio, "sleep", AsyncMock())
    message = _bet_message("100", answer_dice=_dice_returning(6))
    calls = {"n": 0}

    def answer(text, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the anticipation line goes out fine
            return _sent_message()
        raise RuntimeError("chat not found")  # the chain prompt cannot be sent

    message.answer = AsyncMock(side_effect=answer)
    await casino.place_bet(message, state)
    assert await _balance(db) == 10000 - 100 + 500
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_menu_declares_the_chain_rule_before_any_wager(state):
    """The push-your-luck risk is stated in the menu, not discovered mid-round."""
    message = SimpleNamespace(answer_photo=AsyncMock())
    await casino.casino_menu(message, state)
    caption = message.answer_photo.await_args.kwargs["caption"]
    assert "برداشت" in caption and "زنجیره" in caption
    assert "می‌سوزه" in caption  # the loss condition is explicit
    assert "×5" in caption and "×15" in caption  # odds unchanged and still shown
