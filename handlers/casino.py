"""Interactive Telegram casino.

The casino is played inside groups, so every stage of the flow is bound to the
exact message that started it:

* the "how much do you want to bet?" prompt **mentions** the player and its
  ``message_id`` is stored in the FSM data;
* the bet amount is only accepted as a **reply to that prompt**, otherwise any
  group member's stray number would be taken as the wager;
* clicking a game button while a prompt is already pending is answered
  silently, so button spam cannot queue a pile of prompts;
* the prompt is deleted as soon as the wager is parsed, and the dice
  replies to the player's own bet message.

A round is presented as three beats (CASINO-THRILL idea B):

1. **anticipation** — one short line naming the game, sent before the roll;
2. **the roll** — Telegram's ``answer_dice`` animation, untouched;
3. **the verdict** — one tiered line (fumble / so close / win / jackpot).

A winning round replaces beat 3 with «زنجیره سیبی» (idea A): the pot is
*offered*, not credited, and the player has :data:`CHAIN_DECISION_SECONDS` to
bank it (green) or push it one rung further (red). The rules that keep that
fair all live in this module:

* a push never costs a new stake — the wager was already spent on round one;
* the ladder is owned by :mod:`sib.services.casino_service` and is EV-gated
  there, so pushing can never beat banking;
* the timeout always auto-banks: closing Telegram cannot cost a won pot;
* every rung gets a new token, so a stale or double tap cannot settle twice;
* odds, cooldowns and the advertised multipliers are unchanged — a push roll
  reuses :func:`casino_service.calculate_result` and its win rules.

Because the pot stays out of the wallet until it is banked, the ledger only
ever sees the original stake and one ``casino_payout``; a burned pot pays
nothing at all, which is exactly what makes pushing a real risk.
"""
import asyncio
import hashlib
import weakref
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from sib.core.bidi import fix_bidi
from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.handlers.rocket import _normalize_rocket_digits
from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services import casino_service
from sib.services.apple_wallet import AppleWalletService
from sib.services.casino_service import CasinoService
from sib.services.lock_order import lock_user_row

router = Router(name="casino")
GAMES = {"dice": "🎲", "darts": "🎯", "bowling": "🎳", "slots": "🎰"}
ASSET = Path(__file__).resolve().parents[1] / "assets" / "casino" / "casino_menu.jpg"
_ACTIVE_USERS: set[int] = set()
#: Users whose bet prompt is being sent right now. Two taps that land in the
#: same event loop tick both find an empty FSM state, so the state check alone
#: cannot stop a double prompt; this set closes that window.
_PENDING_PROMPTS: set[int] = set()

#: ``{user_mention}`` is a Markdown mention link of the player.
BET_PROMPT_TEMPLATE = (
    "{user_mention} عزیز، چقدر می‌خوای شرط بندی کنی؟ (حداقل 10، حداکثر 5000)\n"
    "مبلغ رو بنویس."
)
REPLY_TO_PROMPT_REQUIRED = "لطفاً روی پیام بات ریپلای کنید و مبلغ را بنویسید."
#: Shown in the menu so the odds are known before the apples are wagered.
MENU_CAPTION = (
    "🎰 به قمارخانه خوش آمدید! کدوم بازی رو انتخاب می‌کنی؟\n"
    "🎲 تاس، 🎯 دارت و 🎳 بولینگ فقط با ۶ برنده می‌شن (×5).\n"
    "🎰 اسلات فقط با سه‌تای یکسان برنده می‌شه (×15).\n"
    "🍎 بردی؟ می‌تونی همون‌جا برداشت کنی یا ریسک کنی و زنجیره رو ادامه بدی — "
    "ولی ادامه بدهی و ببازی، کل پات می‌سوزه."
)


class CasinoStates(StatesGroup):
    waiting_for_bet = State()
    #: «زنجیره سیبی» is live: the pot is on the table and the player has
    #: :data:`CHAIN_DECISION_SECONDS` to bank it or push it one rung further.
    waiting_for_chain_decision = State()


#: How long the bank/push prompt stays open. The handler owns the countdown
#: and auto-banks when it expires, so a player never loses a won pot by simply
#: closing Telegram.
CHAIN_DECISION_SECONDS = casino_service.CHAIN_DECISION_SECONDS
#: Edit budget for the countdown caption. Every edit is best effort: Telegram
#: rate-limits edits, and a dropped tick must never delay the auto-bank.
CHAIN_COUNTDOWN_EDITS = 3
#: Callback prefixes. The token in the middle is what makes a stale tap
#: impossible: it is regenerated for every rung, and a bank consumes it.
CHAIN_BANK_PREFIX = "casino_chain_bank"
CHAIN_PUSH_PREFIX = "casino_chain_push"
#: Per-user chain ownership, so two taps that land in the same tick cannot both
#: settle the same pot. The FSM state alone is updated too late for that.
#:
#: A WeakValueDictionary so the registry cannot grow for the process lifetime:
#: the only strong reference is the handler currently holding the lock (the
#: rate-limit middleware follows the same "in-memory, one worker" contract).
_CHAIN_LOCKS: "weakref.WeakValueDictionary[int, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)
#: One wake-up gate per live rung. The window sleeps on it; a tap opens it, and
#: that is what lets a player answer *while* the countdown is running (aiogram
#: handles each update in its own task, so the window and the tap are
#: concurrent coroutines).
_CHAIN_GATES: dict[str, asyncio.Event] = {}


def _open_gate(token: str) -> None:
    """Wake the window that is waiting for this rung, if it is still waiting."""
    gate = _CHAIN_GATES.get(token)
    if gate is not None:
        gate.set()


async def _wait_for_decision(gate: asyncio.Event, timeout: float) -> bool:
    """True when the player answered in time, False when the clock ran out.

    Isolated so tests can drive the window deterministically instead of
    racing a real ten-second clock.
    """
    try:
        await asyncio.wait_for(gate.wait(), timeout=timeout)
        return True
    except TimeoutError:  # asyncio.TimeoutError is an alias since 3.11
        return False


#: Kept so a test can restore the production clock after stubbing it out.
_REAL_WAIT_FOR_DECISION = _wait_for_decision


def _chain_lock(user_id: int) -> asyncio.Lock:
    """The caller must keep the returned lock alive for the duration of the
    ``async with``; that strong reference is what keeps the weak entry alive."""
    lock = _CHAIN_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _CHAIN_LOCKS[user_id] = lock
    return lock


def chain_token(game_id: str, step: int) -> str:
    """Opaque per-rung token, echoed in the buttons of one prompt."""
    return f"{game_id}.{step}"


def chain_keyboard(token: str, pot: int, *, can_push: bool = True) -> InlineKeyboardMarkup:
    """The two decisions that make the chain: bank (green) or push (red).

    Green/red carry meaning per ``keyboards.common``: banking is safe and
    final, pushing is the risky action. Callback data stays short because the
    Bot API caps it at 64 bytes and the token already carries the game id.
    """
    buttons = [
        success_button(
            text=f"💰 برداشت {pot} سیب", callback_data=f"{CHAIN_BANK_PREFIX}:{token}"
        )
    ]
    if can_push:
        buttons.append(
            danger_button(
                text="🔥 ادامه بده", callback_data=f"{CHAIN_PUSH_PREFIX}:{token}"
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def chain_prompt(pot: int, *, can_push: bool = True) -> str:
    """The push-your-luck prompt: the number, the clock, and the two ways out.

    At the top rung ``can_push`` is False and the text stops offering a bet the
    ladder cannot pay for.
    """
    lines = [f"🎉 بردی! پات فعلی: {pot} سیب."]
    if can_push:
        lines.append(
            "🔥 ادامه بدی، باید دوباره ببری تا پات بزرگ‌تر شه؛ ببازی، همه‌ش می‌سوزه."
        )
    else:
        lines.append("🏆 به آخرین پله‌ی زنجیره رسیدی؛ بالاتر از این پله‌ای نیست.")
    lines.append(
        f"⏳ {CHAIN_DECISION_SECONDS} ثانیه فرصت داری؛ بعد از آن خودکار واریز می‌شه."
    )
    return fix_bidi("\n".join(lines))


def chain_decision_text(pot: int, remaining: int, *, can_push: bool = True) -> str:
    """The prompt with its countdown folded into the same message.

    Telegram cannot animate a timer, but re-editing one caption a few times
    makes the clock visible without spamming the chat with new messages.
    """
    return f"{chain_prompt(pot, can_push=can_push)}\n⏳ {remaining} ثانیه مانده..."


def banked_text(pot: int) -> str:
    return f"💰 {pot} سیب به حسابت واریز شد."


def push_accepted_text(risked: int) -> str:
    """Replaces the bank/push prompt the moment the player pushes."""
    return fix_bidi(f"🔥 ادامه دادی! {risked} سیب روی میز موند...")


def chain_burned_text(pot: int) -> str:
    """Everything that was on the table, gone — no wallet movement involved."""
    return f"💥 ای وای! این بار نیامد. کل {pot} سیب سوخت!"


def mention(user) -> str:
    """Markdown ``tg://user`` mention so the prompt names its player."""
    user_id = getattr(user, "id", None)
    name = safe_name(getattr(user, "full_name", None), "یک سیبی")
    if user_id is None:
        return name
    return f"[{name}](tg://user?id={int(user_id)})"


def bet_prompt(user) -> str:
    """The prompt text: the player's mention first, then the wager question.

    The line opens with the player's mention, which may be a Latin or
    stylized name, so the paragraph direction is pinned to RTL explicitly.
    """
    return fix_bidi(BET_PROMPT_TEMPLATE.format(user_mention=mention(user)))


def replied_to_prompt(message, prompt_message_id) -> bool:
    """Strict reply-to check for :class:`CasinoStates.waiting_for_bet`.

    The FSM state is per-user but the chat is shared, so the wager must arrive
    as a reply to the exact prompt that asked for it. When the prompt id was
    never recorded (a state created before this guard existed), any reply is
    accepted but a free-floating message is still refused.
    """
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return False
    if prompt_message_id is None:
        return True
    try:
        return int(getattr(replied, "message_id", 0)) == int(prompt_message_id)
    except (TypeError, ValueError):
        return False


def _is_bot_message(replied) -> bool:
    """True when the replied-to message was sent by a bot account."""
    sender = getattr(replied, "from_user", None)
    return bool(getattr(sender, "is_bot", False))


async def _delete_prompt(message) -> None:
    """Delete the bot prompt the player replied to (best effort).

    Only a message actually written by the bot is deleted, so a stale FSM
    state (no recorded prompt id) can never wipe a player's own message.
    """
    replied = getattr(message, "reply_to_message", None)
    delete = getattr(replied, "delete", None)
    if delete is None or not _is_bot_message(replied):
        return
    try:
        await delete()
    except Exception:  # already deleted / too old / permissions
        logger.debug("Casino prompt cleanup failed", exc_info=True)


#: Beat 1 — the anticipation line sent just before the animation. It keeps the
#: round a three-beat scene (anticipation → roll → verdict) instead of a silent
#: wait, and it names the game so a group reading mid-scroll knows what is live.
ANTICIPATION = {
    "🎲": "🎲 تاس‌ها در حال چرخش...",
    "🎯": "🎯 دارت پرتاب شد...",
    "🎳": "🎳 توپ در حال غلتیدن...",
    "🎰": "🎰 اسلات‌ها می‌چرخند...",
}
#: A short pause so the anticipation line is read before the dice lands on it.
#: Presentation only: it never gates the wager, the roll or the payout.
ANTICIPATION_PAUSE = 1.0

#: What the animation actually showed. Slots deliberately never print the raw
#: 1-64 result: the player sees fruit, not a number.
_GAME_LINES = {
    "🎲": "تاس {value} آورد",
    "🎯": "دارت به {value} خورد",
    "🎳": "بولینگ {value} پین انداخت",
}
#: Beat 3, tier «fumble»: nothing was close. Values 1-2 of the six faces.
FUMBLE_VALUES = frozenset({1, 2})
_FUMBLE_LINES = {
    "🎲": "تاس {value} آورد. زمین خوردی!",
    "🎯": "دارت به {value} خورد. دستت لرزید!",
    "🎳": "بولینگ {value} پین انداخت. پرت شد!",
}
#: Beat 3, tier «so close»: a losing roll that still raised hope (3-5 of 6).
#: Keyed by how many faces the roll was short of the winning 6, so a 3 never
#: pretends to have been "one short": the tier is dramatic, the number is honest.
_NEAR_MISS_LINES = {
    "🎲": {
        1: "تاس {value} آورد. فقط یه دونه کم آوردی!",
        2: "تاس {value} آورد. دو تا کم آوردی!",
        3: "تاس {value} آورد. سه تا کم آوردی!",
    },
    "🎯": {
        1: "دارت به {value} خورد. یک خط تا هدف فاصله داشتی!",
        2: "دارت به {value} خورد. نزدیک هدف بود، ولی نه روی هدف!",
        3: "دارت به {value} خورد. لبه‌ی تخته بود!",
    },
    "🎳": {
        1: "بولینگ {value} پین انداخت. یکی مونده بود!",
        2: "بولینگ {value} پین انداخت. دو تا مونده بود!",
        3: "بولینگ {value} پین انداخت. نصف لین رو زدی!",
    },
}


def win_line(emoji: str, value: int) -> str:
    """Natural one-liner for a winning round."""
    if emoji == "🎰":
        return "🎰 جکپات زدی"
    return _GAME_LINES[emoji].format(value=value)


def fumble_line(emoji: str, value: int) -> str:
    """The «fumble» tier: the roll never had a chance."""
    if emoji == "🎯" and value == 1:
        return "دارت به بیرون خورد. دستت لرزید!"
    return _FUMBLE_LINES[emoji].format(value=value)


def near_miss_line(emoji: str, value: int) -> str:
    """The «so close» tier: a real, honest near miss."""
    lines = _NEAR_MISS_LINES[emoji]
    return lines.get(6 - value, lines[1]).format(value=value)


def _slot_reels(value: int) -> tuple[int, int, int]:
    """Decode a slot result into its three reels (0-3 each).

    Read as three base-4 digits plus one, ``1 + 16*a + 4*b + c``. The winning
    set in :mod:`sib.services.casino_service` is the product rule; this
    decoding is what lets the *presentation* tell a genuine two-of-a-kind
    "one fruit short" apart from three different fruits, without ever
    printing the raw 1-64 value.
    """
    offset = value - 1
    return offset // 16, (offset // 4) % 4, offset % 4


def slots_two_of_a_kind(value: int) -> bool:
    """True when exactly two reels match — the real «one fruit short» case."""
    return len(set(_slot_reels(value))) == 2


def verdict_text(emoji: str, value: int, payout: int) -> str:
    """Beat 3: one dramatic verdict per tier. Never leaks a slot result.

    Wins carry the payout (and the slots multiplier) so the celebration line
    doubles as the receipt; losses only ever describe the animation the player
    just watched.
    """
    if emoji == "🎰":
        if payout:
            return (
                f"💥 جکپات! سه‌تای یکسان! {casino_service.SLOTS_MULTIPLIER} برابر! "
                f"· {payout} سیب گرفتی."
            )
        if slots_two_of_a_kind(value):
            return "🍒 خیلی نزدیک بود! فقط یه میوه کم داشتی... باختی."
        return "🎰 اسلات‌ها جور درنیومدن... باختی."
    if payout:
        return (
            f"🎉 بردی! {win_line(emoji, value)}. ضربه‌ی کاری! "
            f"· {payout} سیب گرفتی."
        )
    if value in FUMBLE_VALUES:
        return f"😭 باختی! {fumble_line(emoji, value)}"
    return f"😱 نزدیک بود! {near_miss_line(emoji, value)}"


async def _notify(message, text: str) -> None:
    """Answer the player, threading the notice when the message allows it."""
    reply = getattr(message, "reply", None)
    if reply is not None:
        await reply(text)
        return
    await message.answer(text)


async def _await_chain_decision(
    state: FSMContext,
    source,
    user_id: int,
    *,
    emoji: str,
    stake: int,
    game_id: str,
    step: int,
    pot: int,
) -> None:
    """Open one bank/push window: prompt, arm the FSM, run the clock.

    The prompt goes out before the state is armed, so a fast tap can never hit
    a handler while the FSM still says «waiting for a bet». Anything arriving
    later is arbitrated by the token, which is new for every rung: both
    callbacks refuse a token that is no longer the live one.
    """
    token = chain_token(game_id, step)
    can_push = casino_service.chain_available(emoji, stake, step)
    try:
        prompt = await source.answer(
            chain_prompt(pot, can_push=can_push),
            reply_markup=chain_keyboard(token, pot, can_push=can_push),
        )
    except Exception:
        # Without a prompt there is no decision to make, so the pot is paid
        # rather than stranded: the player already won it.
        logger.exception(
            "Casino chain prompt failed (game=%s step=%s); banking the pot", game_id, step
        )
        await state.clear()
        key = _derive_round_id(game_id, step) if step > 1 else game_id
        await _credit(user_id, pot, key, "casino_payout")
        await _notify(source, f"پیام بازی ارسال نشد؛ {banked_text(pot)}")
        return
    await state.set_state(CasinoStates.waiting_for_chain_decision)
    await state.update_data(
        emoji=emoji,
        chain_owner=user_id,
        chain_token=token,
        chain_pot=pot,
        chain_stake=stake,
        chain_step=step,
        chain_game_id=game_id,
    )
    gate = _CHAIN_GATES[token] = asyncio.Event()
    try:
        tick = CHAIN_DECISION_SECONDS / (CHAIN_COUNTDOWN_EDITS + 1)
        for tick_index in range(1, CHAIN_COUNTDOWN_EDITS + 1):
            if await _wait_for_decision(gate, tick):
                return  # answered: the callback owns the settlement
            remaining = max(1, CHAIN_DECISION_SECONDS - round(tick * tick_index))
            try:
                await prompt.edit_text(
                    chain_decision_text(pot, remaining, can_push=can_push),
                    reply_markup=chain_keyboard(token, pot, can_push=can_push),
                )
            except Exception:
                # A dropped tick is cosmetic; the deadline is the gate's.
                logger.debug("Casino chain countdown edit failed", exc_info=True)
        if await _wait_for_decision(gate, tick):
            return
        await _auto_bank(state, prompt, user_id=user_id, token=token, pot=pot)
    finally:
        # Whoever woke us (or the timeout) closes the gates for this rung.
        _CHAIN_GATES.pop(token, None)


async def _auto_bank(state: FSMContext, prompt, *, user_id: int, token: str, pot: int) -> None:
    """Close the window by banking: the promise that a won pot is never lost.

    Scope of that promise: it holds while the process is alive (the FSM is
    aiogram's in-memory storage). A restart inside the 10-second window loses
    the window with it — but not any apples, because an unclaimed pot was never
    debited or credited: the ledger still shows only the original wager.
    """
    async with _chain_lock(user_id):
        data = await state.get_data()
        if data.get("chain_token") != token:
            return  # a tap settled this rung first; its answer already shipped
        await state.clear()
        await _credit(user_id, pot, token, "casino_payout")
        try:
            await prompt.edit_text(banked_text(pot))
        except Exception:
            logger.debug("Casino chain auto-bank edit failed", exc_info=True)


def _derive_round_id(game_id: str, step: int) -> str:
    """A stand-in id for a later rung of the same chain.

    Only the FIRST round keeps the id its wager was debited under; every push
    is a fresh roll with no new stake, so it needs its own idempotency
    namespace. Deterministic, because a retried settlement must reuse the same
    key rather than mint a second credit.
    """
    digest = hashlib.blake2s(f"{game_id}:{step}".encode(), digest_size=8).hexdigest()
    return f"{game_id}.{step}.{int(digest, 16) % 1_000_000_007}"


async def _chain_round(
    source,
    state: FSMContext,
    *,
    user_id: int,
    emoji: str,
    stake: int,
    game_id: str,
    step: int,
) -> None:
    """Roll one more rung of the chain, then prompt again or burn the pot.

    No wallet movement happens here: the stake was spent when the chain began,
    so a push is always free and a loss only forfeits a pot nobody was paid.
    ``source`` only has to answer like a Message (``answer`` and
    ``answer_dice``), which keeps this testable and keeps the round in the chat
    where the prompt was.
    """
    pot = casino_service.chain_pot(emoji, stake, step - 1)  # what is on the table
    anticipation = ANTICIPATION.get(emoji)
    if anticipation:
        try:
            await source.answer(anticipation)
        except Exception:
            logger.debug(
                "Casino chain anticipation failed (user=%s)", user_id, exc_info=True
            )
    await asyncio.sleep(ANTICIPATION_PAUSE)
    try:
        dice = await source.answer_dice(
            emoji=emoji, reply_to_message_id=getattr(source, "message_id", None)
        )
    except Exception:
        # Never strand apples the player already won: settle the pot instead.
        logger.exception("Casino chain dice failed (user=%s game=%s)", user_id, game_id)
        await state.clear()
        await _credit(user_id, pot, _derive_round_id(game_id, step), "casino_payout")
        await _notify(source, f"ارسال بازی ناموفق بود؛ {banked_text(pot)}")
        return
    await asyncio.sleep(4.0 if emoji == "🎰" else 3.5)
    outcome = CasinoService.calculate_result(emoji, dice.dice.value, stake)
    if not outcome["is_win"]:
        await state.clear()
        await source.answer(
            f"{verdict_text(emoji, outcome['value'], 0)}\n{chain_burned_text(pot)}"
        )
        return
    # This roll WAS rung ``step``, so a win offers rung ``step``'s pot: the
    # next push is what moves to ``step + 1``.
    await _await_chain_decision(
        state,
        source,
        user_id,
        emoji=emoji,
        stake=stake,
        game_id=game_id,
        step=step,
        pot=casino_service.chain_pot(emoji, stake, step),
    )


@router.message(Command("casino"))
@router.message(F.text.in_({"قمارخانه", "قمارخونه", "قمار خانه", "قمار خونه", "قمار", "کازینو"}))
async def casino_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        primary_button(text=label, callback_data=f"casino_game:{key}")
        for key, label in [("dice", "🎲 تاس"), ("darts", "🎯 دارت"), ("bowling", "🎳 بولینگ"), ("slots", "🎰 اسلات")]
    ]])
    await message.answer_photo(FSInputFile(str(ASSET)), caption=MENU_CAPTION, reply_markup=keyboard)


@router.callback_query(F.data.startswith("casino_game:"))
async def choose_game(callback: CallbackQuery, state: FSMContext) -> None:
    emoji = GAMES.get(callback.data.partition(":")[2])
    if not emoji or not callback.message:
        await callback.answer("بازی نامعتبر است.", show_alert=True)
        return
    sender = getattr(callback, "from_user", None)
    user_id = getattr(sender, "id", None)
    # Anti-spam click: a prompt is already waiting for this player. Answer the
    # tap silently instead of sending a second (or tenth) prompt.
    if await state.get_state() == CasinoStates.waiting_for_bet.state:
        await callback.answer()
        return
    if user_id is not None:
        if user_id in _PENDING_PROMPTS:
            await callback.answer()
            return
        _PENDING_PROMPTS.add(user_id)
    try:
        await state.set_state(CasinoStates.waiting_for_bet)
        await state.update_data(emoji=emoji, prompt_message_id=None)
        await callback.answer()
        try:
            prompt = await callback.message.answer(bet_prompt(sender))
        except Exception:
            # No prompt was delivered: never leave the player stuck in a state
            # that would silently swallow their next tap.
            logger.exception("Casino prompt send failed (user=%s)", user_id)
            await state.clear()
            return
        prompt_message_id = getattr(prompt, "message_id", None)
        if prompt_message_id is not None:
            await state.update_data(prompt_message_id=prompt_message_id)
    finally:
        if user_id is not None:
            _PENDING_PROMPTS.discard(user_id)


async def _credit(user_id: int, amount: int, game_id: str, reason: str) -> None:
    async with get_session() as session:
        await lock_user_row(session, user_id)
        await AppleWalletService.earn(session, user_id, amount, reason=reason,
                                      ref_type="casino_round", idempotency_key=f"{reason}:{game_id}")


@router.message(CasinoStates.waiting_for_bet, F.text)
async def place_bet(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        await state.clear()
        return
    user_id = message.from_user.id
    data = await state.get_data()
    # The wager must be a reply to THIS bot prompt; a group message that just
    # happens to be a number is never a bet. The state is kept so the player
    # can reply to the prompt properly.
    if not replied_to_prompt(message, data.get("prompt_message_id")):
        await _notify(message, REPLY_TO_PROMPT_REQUIRED)
        return
    if user_id in _ACTIVE_USERS:
        return
    _ACTIVE_USERS.add(user_id)
    try:
        emoji = data.get("emoji")
        text = _normalize_rocket_digits(message.text.strip())
        if not text.isascii() or not text.isdecimal() or len(text) > 5 or emoji not in GAMES.values():
            await message.answer("مبلغ شرط باید عددی بین 10 تا 5000 سیب باشد.")
            return
        bet = int(text)
        async with get_session() as session:
            result = await CasinoService.validate_bet(session, user_id, bet)
        if result["status"] != "started":
            # A cooldown / low balance is retryable, so the prompt stays.
            await message.answer(result["error"])
            return
        # The wager is parsed and accepted: the prompt has done its job.
        await _delete_prompt(message)
        game_id = result["game_id"]
        # Beat 1 — anticipation is best effort: a group whose bot lost the
        # right to post must still get its round, so this never raises.
        anticipation = ANTICIPATION.get(emoji)
        if anticipation:
            try:
                await message.answer(anticipation)
            except Exception:
                logger.debug(
                    "Casino anticipation send failed (user=%s)", user_id, exc_info=True
                )
        await asyncio.sleep(ANTICIPATION_PAUSE)
        try:
            # The animation replies to the player's own bet message, so the
            # round stays attached to the person who placed it.
            dice = await message.answer_dice(
                emoji=emoji, reply_to_message_id=getattr(message, "message_id", None)
            )
        except Exception:
            logger.exception("Casino dice send failed (user=%s game=%s)", user_id, game_id)
            await _credit(user_id, bet, game_id, "casino_refund")
            await message.answer("ارسال بازی ناموفق بود؛ شرط به کیف پولت برگشت.")
            return
        await asyncio.sleep(4.0 if emoji == "🎰" else 3.5)
        outcome = CasinoService.calculate_result(emoji, dice.dice.value, bet)
        value = outcome["value"]
        if outcome["is_win"] and casino_service.chain_available(emoji, bet, 1):
            # Beat 3 becomes a decision: the pot is OFFERED, not credited, so
            # nothing is paid until the player banks — explicitly or by timeout.
            await _await_chain_decision(
                state,
                message,
                user_id,
                emoji=emoji,
                stake=bet,
                game_id=game_id,
                step=1,
                pot=casino_service.chain_pot(emoji, bet, 1),
            )
            return
        # A plain round keeps the idea-B behaviour: settle first, then narrate,
        # so a message can never promise a payout the ledger did not make.
        if outcome["is_win"]:
            await _credit(user_id, outcome["payout"], game_id, "casino_payout")
        await message.answer(verdict_text(emoji, value, outcome["payout"]))
    finally:
        # A live chain keeps its own state: `waiting_for_chain_decision` must
        # survive this handler, and only an unfinished one is cleared here.
        if await state.get_state() != CasinoStates.waiting_for_chain_decision.state:
            await state.clear()
        _ACTIVE_USERS.discard(user_id)


@router.callback_query(F.data.startswith(f"{CHAIN_BANK_PREFIX}:"))
async def chain_bank(callback: CallbackQuery, state: FSMContext) -> None:
    """«💰 برداشت»: credit the pot exactly once and close the prompt."""
    token = callback.data.partition(":")[2]
    sender = getattr(callback, "from_user", None)
    user_id = getattr(sender, "id", None)
    if user_id is None or callback.message is None:
        await callback.answer()
        return
    async with _chain_lock(user_id):
        data = await state.get_data()
        if data.get("chain_token") != token:
            # Already settled: auto-bank, this button twice, or an older rung.
            await callback.answer("این دست تموم شد.", show_alert=False)
            return
        owner = data.get("chain_owner")
        if owner is not None and int(owner) != user_id:
            # A bystander tapping the group's buttons can never bank for us.
            await callback.answer("این دست مال تو نیست.", show_alert=True)
            return
        pot = int(data.get("chain_pot") or 0)
        await state.clear()
        # Wake the window first: from here on this coroutine owns the rung and
        # the waiting one must simply return.
        _open_gate(token)
        await _credit(user_id, pot, token, "casino_payout")
        await callback.answer(f"💰 {pot} سیب واریز شد.")
        try:
            await callback.message.edit_text(banked_text(pot))
        except Exception:
            logger.debug("Casino chain bank edit failed", exc_info=True)


@router.callback_query(F.data.startswith(f"{CHAIN_PUSH_PREFIX}:"))
async def chain_push(callback: CallbackQuery, state: FSMContext) -> None:
    """«🔥 ادامه بده»: roll again for a bigger pot, never for a new stake."""
    token = callback.data.partition(":")[2]
    sender = getattr(callback, "from_user", None)
    user_id = getattr(sender, "id", None)
    if user_id is None or callback.message is None:
        await callback.answer()
        return
    # The lock is taken only long enough to claim the rung; the animation runs
    # outside it so a slow send cannot block anything else the player does.
    async with _chain_lock(user_id):
        data = await state.get_data()
        if data.get("chain_token") != token:
            await callback.answer("این دست تموم شد.", show_alert=False)
            return
        owner = data.get("chain_owner")
        if owner is not None and int(owner) != user_id:
            await callback.answer("این دست مال تو نیست.", show_alert=True)
            return
        emoji = data.get("emoji")
        stake = int(data.get("chain_stake") or 0)
        step = int(data.get("chain_step") or 0) + 1
        game_id = data.get("chain_game_id")
        if emoji not in GAMES.values() or stake <= 0 or not game_id:
            await state.clear()
            await callback.answer("این دست معتبر نیست.", show_alert=True)
            return
        if not casino_service.chain_available(emoji, stake, step - 1):
            # The button should not exist here; refuse rather than invent a rung.
            await callback.answer("این دست تموم شد.", show_alert=False)
            return
        risked = casino_service.chain_pot(emoji, stake, step - 1)
        # Clearing first retires the token, so a second tap finds nothing live;
        # the data is then re-armed for the round that is about to roll.
        await state.clear()
        await state.update_data(
            emoji=emoji,
            chain_token=None,
            chain_stake=stake,
            chain_step=step,
            chain_game_id=game_id,
            # The risked pot is recorded under the rung that is LIVE, so if the
            # send fails the settlement refunds exactly what was on the table.
            chain_pot=risked,
        )
        _open_gate(token)
        await callback.answer("🔥 ادامه!")
        try:
            await callback.message.edit_text(push_accepted_text(risked))
        except Exception:
            logger.debug("Casino chain push edit failed", exc_info=True)
    await _chain_round(
        callback.message,
        state,
        user_id=user_id,
        emoji=emoji,
        stake=stake,
        game_id=game_id,
        step=step,
    )
    if await state.get_state() != CasinoStates.waiting_for_chain_decision.state:
        await state.clear()
