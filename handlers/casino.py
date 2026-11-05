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
"""
import asyncio
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.handlers.rocket import _normalize_rocket_digits
from sib.keyboards.common import primary_button
from sib.services.casino_service import CasinoService
from sib.services.apple_wallet import AppleWalletService
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
    "🎰 اسلات فقط با سه‌تای یکسان برنده می‌شه (×15)."
)


class CasinoStates(StatesGroup):
    waiting_for_bet = State()


def mention(user) -> str:
    """Markdown ``tg://user`` mention so the prompt names its player."""
    user_id = getattr(user, "id", None)
    name = safe_name(getattr(user, "full_name", None), "یک سیبی")
    if user_id is None:
        return name
    return f"[{name}](tg://user?id={int(user_id)})"


def bet_prompt(user) -> str:
    """The prompt text: the player's mention first, then the wager question."""
    return BET_PROMPT_TEMPLATE.format(user_mention=mention(user))


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


#: What the animation actually showed. Slots deliberately never print the raw
#: 1-64 result: the player sees fruit, not a number.
_GAME_LINES = {
    "🎲": "تاس {value} آورد",
    "🎯": "دارت به {value} خورد",
    "🎳": "بولینگ {value} پین انداخت",
}


def win_line(emoji: str, value: int) -> str:
    """Natural one-liner for a winning round."""
    if emoji == "🎰":
        return "🎰 جکپات زدی"
    return _GAME_LINES[emoji].format(value=value)


def loss_line(emoji: str, value: int) -> str:
    """Natural one-liner for a losing round — no rule reminder in the chat."""
    if emoji == "🎰":
        return "اسلات‌ها جور درنیومدن"
    if emoji == "🎯" and value == 1:
        return "دارت به بیرون خورد"
    return _GAME_LINES[emoji].format(value=value)


async def _notify(message, text: str) -> None:
    """Answer the player, threading the notice when the message allows it."""
    reply = getattr(message, "reply", None)
    if reply is not None:
        await reply(text)
        return
    await message.answer(text)


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
        if outcome["is_win"]:
            await _credit(user_id, outcome["payout"], game_id, "casino_payout")
            await message.answer(
                f"🏆 بردی! {win_line(emoji, value)} و {outcome['payout']} سیب گرفتی."
            )
        else:
            await message.answer(f"😭 باختی! {loss_line(emoji, value)}.")
    finally:
        await state.clear()
        _ACTIVE_USERS.discard(user_id)
