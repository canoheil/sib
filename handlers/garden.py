"""Personal garden panel handlers (باغ من 🌳).

Private-chat interactive panel. Owner-scoped: only the player who opened
the panel may plant, harvest or expand through it.
"""

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.database.connection import get_session
from sib.database.models import PlotState
from sib.services.apple_wallet import AppleWalletService
from sib.services.garden import GardenService
from sib.services.panel import PanelOwnershipService
from sib.services.seedling import SeedlingService
from sib.services.telegram_ui import (
    safe_answer_callback,
    safe_edit_callback_message,
)

router = Router(name="garden")

PLOT_EMOJI = {
    PlotState.EMPTY: "⬜",
    PlotState.GROWING: "🌱",
    PlotState.MATURE: "🌳",
    PlotState.FRUIT_READY: "🍎",
}


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "آماده"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours} ساعت و {minutes} دقیقه"
    return f"{minutes} دقیقه"


async def _render_garden(user_id: int, session) -> tuple[str, InlineKeyboardMarkup]:
    garden = await GardenService.get_or_create(session, user_id)
    views = GardenService.view(garden)
    seedlings = await SeedlingService.get_total(session, user_id)
    apples = await AppleWalletService.get_balance(session, user_id)

    lines = ["🌳 **باغ من** 🍏\n"]
    lines.append(f"🌱 نهال‌ها: `{seedlings}` | 🍎 سیب‌ها: `{apples}`")
    lines.append("")

    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for view in views:
        emoji = PLOT_EMOJI.get(view.state, "⬜")
        line = f"{emoji} قطعه {view.plot_index + 1}"
        if view.state == PlotState.GROWING:
            line += f" — رشد تا درخت: {format_duration(view.seconds_to_next_stage)}"
        elif view.state == PlotState.MATURE:
            line += f" — میوه تا: {format_duration(view.seconds_to_next_stage)}"
        elif view.state == PlotState.FRUIT_READY:
            line += " — **آماده برداشت!**"
        else:
            line += " — خالی"
        lines.append(line)

        if view.state == PlotState.EMPTY:
            row.append(
                InlineKeyboardButton(
                    text=f"🌱 کاشت در قطعه {view.plot_index + 1}",
                    callback_data=f"garden_plant:{view.plot_index}",
                )
            )
        elif view.state == PlotState.FRUIT_READY:
            row.append(
                InlineKeyboardButton(
                    text=f"🍎 برداشت قطعه {view.plot_index + 1}",
                    callback_data=f"garden_harvest:{view.plot_index}",
                )
            )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append(
        [InlineKeyboardButton(text="➕ خرید ۵ قطعه جدید (با سیب)", callback_data="garden_expand")]
    )
    buttons.append([InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="garden_refresh")])
    buttons.append([InlineKeyboardButton(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("garden"))
async def cmd_garden(message: Message, bot: Bot):
    """Open the personal garden panel in private chat."""
    if message.chat.type != "private":
        await message.reply("🍏 باغ شخصی فقط در چت خصوصی قابل مدیریت است؛ به پیوی سیب بیایید!")
        return
    user = message.from_user
    if not user:
        return

    async with get_session() as session:
        text, kb = await _render_garden(user.id, session)
        sent = await message.reply(text, reply_markup=kb, parse_mode="Markdown")
        if sent is not None and sent.message_id:
            await PanelOwnershipService.claim(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                user_id=user.id,
                panel_type="garden",
            )


async def _rerender(callback: CallbackQuery, bot: Bot, *, answer: str = "بروزرسانی شد 🍏") -> None:
    user = callback.from_user
    if not user:
        return
    async with get_session() as session:
        text, kb = await _render_garden(user.id, session)
    await safe_answer_callback(callback, answer)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data == "garden_refresh")
async def handle_garden_refresh(callback: CallbackQuery, bot: Bot):
    await _rerender(callback, bot)


@router.callback_query(F.data.startswith("garden_plant:"))
async def handle_garden_plant(callback: CallbackQuery, bot: Bot):
    plot_index = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return

    # State first, presentation second: the re-render runs AFTER the
    # service transaction is closed (never hold FOR UPDATE locks while
    # opening another session - that self-deadlocks).
    error = None
    async with get_session() as session:
        try:
            await GardenService.plant(session, user.id, plot_index)
        except ValueError as e:
            error = str(e)

    if error:
        await _rerender(callback, bot, answer=error)
        return
    await _rerender(callback, bot, answer="نهال کاشته شد! 🌱")


@router.callback_query(F.data.startswith("garden_harvest:"))
async def handle_garden_harvest(callback: CallbackQuery, bot: Bot):
    plot_index = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return

    error = None
    apples = 0
    async with get_session() as session:
        try:
            apples = await GardenService.harvest(session, user.id, plot_index)
        except ValueError as e:
            error = str(e)

    if error:
        await _rerender(callback, bot, answer=error)
        return
    await _rerender(callback, bot, answer=f"{apples} سیب برداشت شد! 🍎")


@router.callback_query(F.data == "garden_expand")
async def handle_garden_expand(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return

    error = None
    capacity = 0
    async with get_session() as session:
        try:
            capacity = await GardenService.expand(session, user.id)
        except ValueError as e:
            error = str(e)

    if error:
        await _rerender(callback, bot, answer=error)
        return
    await _rerender(callback, bot, answer=f"باغ گسترش یافت! ظرفیت جدید: {capacity} قطعه 🌳")
