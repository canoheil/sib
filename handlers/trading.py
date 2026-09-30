"""Card trading handlers (Phase 13 UI).

Double confirmation, one trade per user per day.
"""

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.keyboards.common import danger_button, primary_button, success_button
from sib.database.connection import get_session
from sib.database.models import Character, PlayerCard, Trade, TradeStatus
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.trading import TradeError, TradeService

router = Router(name="trading")


async def _trade_name(session, card: PlayerCard) -> str:
    character = await session.get(Character, card.character_id)
    return character.name_fa if character else "نامشخص"


async def _pending_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with get_session() as session:
        trades = await TradeService.list_pending(session, user_id)
        can = await TradeService.can_trade_today(session, user_id)

        lines = ["🔁 **مبادله کارت**\n"]
        lines.append(
            "سهمیه امروز: " + ("✅ در دسترس" if can else "❌ استفاده‌شده") + " (۱ مبادله در روز)"
        )
        lines.append("")

        buttons: list[list[InlineKeyboardButton]] = []
        if not trades:
            lines.append("معامله‌ی بازی ندارید.")
        for trade in trades:
            proposer_card = await session.get(PlayerCard, trade.proposer_card_id)
            recipient_card = await session.get(PlayerCard, trade.recipient_card_id)
            if not proposer_card or not recipient_card:
                continue
            p_name = await _trade_name(session, proposer_card)
            r_name = await _trade_name(session, recipient_card)
            side = "پیشنهاد شما" if trade.proposer_id == user_id else "پیشنهاد دریافتی"
            lines.append(
                f"#{trade.id} ({side}): {p_name} ⇄ {r_name} | "
                f"تأیید: {'✅' if trade.proposer_confirmed else '⏳'} / "
                f"{'✅' if trade.recipient_confirmed else '⏳'}"
            )
            buttons.append(
                [
                    success_button(
                        text=f"✅ تأیید #{trade.id}", callback_data=f"trade_ok:{trade.id}"
                    ),
                    danger_button(
                        text=f"❌ لغو #{trade.id}", callback_data=f"trade_cancel:{trade.id}"
                    ),
                ]
            )
        buttons.append([primary_button(text="🔄 بروزرسانی", callback_data="trade_refresh")])
    buttons.append([danger_button(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("trade"))
async def cmd_trade(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return
    text, kb = await _pending_view(user.id)
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "apple_trade")
async def handle_apple_trade(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return

    prev_message_id = getattr(callback.message, "message_id", None)
    text, kb = await _pending_view(user.id)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback,
        bot,
        text,
        reply_markup=kb,
    )
    from sib.services.panel import PanelOwnershipService

    chat = getattr(callback.message, "chat", None)
    msg_id = getattr(callback.message, "message_id", None)
    if chat is not None and msg_id and callback.from_user:
        async with get_session() as session:
            if prev_message_id and prev_message_id != msg_id:
                await PanelOwnershipService.move_claim(
                    session,
                    chat.id,
                    prev_message_id,
                    msg_id,
                    callback.from_user.id,
                    panel_type="trade",
                )
            else:
                await PanelOwnershipService.claim(
                    session, chat.id, msg_id, callback.from_user.id, panel_type="trade"
                )
            await session.commit()


@router.callback_query(F.data == "trade_refresh")
async def handle_trade_refresh(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    text, kb = await _pending_view(user.id)
    await safe_answer_callback(callback, "بروزرسانی شد")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("trade_ok:"))
async def handle_trade_ok(callback: CallbackQuery, bot: Bot):
    trade_id = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        try:
            trade = await TradeService.confirm(session, trade_id, user.id)
        except TradeError as e:
            await safe_answer_callback(callback, str(e), show_alert=True)
            return

    text, kb = await _pending_view(user.id)
    if trade.status == TradeStatus.COMPLETED:
        await safe_answer_callback(callback, "مبادله انجام شد! 🎉")
    else:
        await safe_answer_callback(callback, "تأیید شما ثبت شد؛ منتظر تأیید طرف مقابل.")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("trade_cancel:"))
async def handle_trade_cancel(callback: CallbackQuery, bot: Bot):
    trade_id = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return

    async with get_session() as session:
        try:
            await TradeService.cancel(session, trade_id, user.id)
        except TradeError as e:
            await safe_answer_callback(callback, str(e), show_alert=True)
            return

    text, kb = await _pending_view(user.id)
    await safe_answer_callback(callback, "معامله لغو شد.")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)
