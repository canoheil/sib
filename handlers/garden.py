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
from sib.services.config_store import ConfigService
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
    from sib.database.models import User
    from sib.services.items import ItemService

    garden = await GardenService.get_or_create(session, user_id)
    views = GardenService.view(garden)
    seedlings = await SeedlingService.get_total(session, user_id)
    apples = await AppleWalletService.get_balance(session, user_id)
    owner = await session.get(User, user_id)
    owner_name = (owner.first_name if owner else None) or "سیبی"

    lines = [f"🌳 **باغ سیبِ {owner_name}** 🍏\n"]
    lines.append(f"🌱 نهال‌ها: `{seedlings}` | 🍎 سیب‌ها: `{apples}`")
    lines.append(f"🎁 ذخیره‌ی برداشت‌ها: {await ItemService.summary_line(session, user_id)}")
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
        [
            InlineKeyboardButton(
                text=(
                    f"➕ خرید {await ConfigService.get_int(session, 'garden_expansion_step')} قطعه جدید"
                    f" — 🍎 {await ConfigService.get_int(session, 'garden_expansion_cost_apples')} سیب"
                ),
                callback_data="garden_expand",
            )
        ]
    )
    buttons.append([InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="garden_refresh")])
    buttons.append([InlineKeyboardButton(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("garden"))
async def cmd_garden(message: Message, bot: Bot):
    """Open the sender's garden (public view + private control)."""
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
    """Re-render the garden panel.

    The garden SUBJECT is resolved from the DB panel owner (authoritative
    state), never from callback/memory state - the shared message can
    never switch identity to whoever pressed a button.
    """
    from sib.services.panel import PanelOwnershipService

    user = callback.from_user
    if not user:
        return
    chat = getattr(callback.message, "chat", None)
    msg_id = getattr(callback.message, "message_id", None)
    async with get_session() as session:
        if chat is not None and msg_id:
            subject_id, _panel_type = await PanelOwnershipService.get_panel_context(
                session, chat.id, msg_id, user.id
            )
        else:
            subject_id = user.id
        text, kb = await _render_garden(subject_id, session)
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
    drop = None
    async with get_session() as session:
        try:
            apples, drop = await GardenService.harvest_with_drop(session, user.id, plot_index)
        except ValueError as e:
            error = str(e)

    if error:
        await _rerender(callback, bot, answer=error)
        return

    answer = f"{apples} سیب برداشت شد! 🍎"
    if drop:
        drop_msg = drop.get("message")
        if drop_msg:
            answer += f"\n{drop_msg}"
    await _rerender(callback, bot, answer=answer)

    # Decorative asset (graceful: missing asset never breaks the flow).
    if drop:
        from sib.services.assets import AssetService

        asset_key = drop.get("asset")
        if asset_key:
            chat = getattr(callback.message, "chat", None)
            if chat is not None:
                async with get_session() as session:
                    await AssetService.send(session, bot, chat.id, asset_key)


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
