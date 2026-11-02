"""Interactive Telegram casino."""
import asyncio
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message

from sib.core.logging import logger
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


class CasinoStates(StatesGroup):
    waiting_for_bet = State()


@router.message(Command("casino"))
@router.message(F.text.in_({"قمارخانه", "قمارخونه", "قمار خانه", "قمار خونه", "قمار", "کازینو"}))
async def casino_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        primary_button(text=label, callback_data=f"casino_game:{key}")
        for key, label in [("dice", "🎲 تاس"), ("darts", "🎯 دارت"), ("bowling", "🎳 بولینگ"), ("slots", "🎰 اسلات")]
    ]])
    await message.answer_photo(FSInputFile(str(ASSET)), caption="🎰 به قمارخانه خوش آمدید! کدوم بازی رو انتخاب می‌کنی؟", reply_markup=keyboard)


@router.callback_query(F.data.startswith("casino_game:"))
async def choose_game(callback: CallbackQuery, state: FSMContext) -> None:
    emoji = GAMES.get(callback.data.partition(":")[2])
    if not emoji or not callback.message:
        await callback.answer("بازی نامعتبر است.", show_alert=True)
        return
    await state.set_state(CasinoStates.waiting_for_bet)
    await state.update_data(emoji=emoji)
    await callback.answer()
    await callback.message.answer("چقدر می‌خوای شرط بندی کنی؟ (حداقل 10، حداکثر 5000)\nمبلغ رو بنویس.")


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
    if user_id in _ACTIVE_USERS:
        return
    _ACTIVE_USERS.add(user_id)
    try:
        data = await state.get_data()
        emoji = data.get("emoji")
        text = _normalize_rocket_digits(message.text.strip())
        if not text.isascii() or not text.isdecimal() or len(text) > 5 or emoji not in GAMES.values():
            await message.answer("مبلغ شرط باید عددی بین 10 تا 5000 سیب باشد.")
            return
        bet = int(text)
        async with get_session() as session:
            result = await CasinoService.validate_bet(session, user_id, bet)
        if result["status"] != "started":
            await message.answer(result["error"])
            return
        game_id = result["game_id"]
        try:
            dice = await message.answer_dice(emoji=emoji)
        except Exception:
            logger.exception("Casino dice send failed (user=%s game=%s)", user_id, game_id)
            await _credit(user_id, bet, game_id, "casino_refund")
            await message.answer("ارسال بازی ناموفق بود؛ شرط به کیف پولت برگشت.")
            return
        await asyncio.sleep(4.0 if emoji == "🎰" else 3.5)
        outcome = CasinoService.calculate_result(emoji, dice.dice.value, bet)
        value = outcome["value"]
        if emoji == "🎰":
            description = "🎰 جکپات زدی!" if outcome["is_win"] else "🎰 اسلات باختی!"
        else:
            description = {
                "🎲": f"تاس {value} آورد",
                "🎯": f"دارت به {value} خورد",
                "🎳": f"بولینگ {value} پین انداخت",
            }[emoji]
        if outcome["is_win"]:
            await _credit(user_id, outcome["payout"], game_id, "casino_payout")
            separator = " " if emoji == "🎰" else " و "
            await message.answer(f"🏆 بردی! {description}{separator}{outcome['payout']} سیب گرفتی.")
        else:
            punctuation = "" if emoji == "🎰" else "."
            await message.answer(f"😭 باختی! {description}{punctuation}")
    finally:
        await state.clear()
        _ACTIVE_USERS.discard(user_id)
