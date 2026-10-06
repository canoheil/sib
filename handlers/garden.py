"""Personal garden panel handlers (باغ من 🌳) + magical tree panel.

Private-chat interactive panel. Owner-scoped: only the player who opened
the panel may plant, harvest, unlock or enchant through it.

Screens:
- garden panel: compact plot list; each plot opens its own tree panel
- tree panel: per-tree state, magical level/height, drop chances and
  actions (plant / harvest / magic powder)
"""

from typing import Optional

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.database.connection import get_session
from sib.database.models import PlotState
from sib.services.apple_wallet import AppleWalletService
from sib.services.garden import GardenService, magic_height_label
from sib.services.items import ItemService, DropService, item_emoji, item_label
from sib.services.panel import (
    LOCKED_MESSAGE,
    PanelAuthorizationError,
    PanelOwnershipService,
    resolve_personal_actor,
)
from sib.services.seedling import SeedlingService
from sib.keyboards.common import danger_button, primary_button, success_button
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

# Display metadata for the four harvest outcomes (drop table keys).
DROP_DISPLAY = [
    ("apple_normal", "🍎", "سیب معمولی"),
    ("apple_gold", "🪙", "سیب طلایی"),
    ("apple_diamond", "💎", "سیب الماسی"),
    ("apple_obsidian", "🌑", "سیب ابسیدینی"),
]


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "آماده"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours} ساعت و {minutes} دقیقه"
    return f"{minutes} دقیقه"


def format_percent(weight: float, total: float) -> str:
    if total <= 0:
        return "0%"
    value = weight / total * 100
    return f"{round(value)}%" if abs(value - round(value)) < 0.05 else f"{value:.1f}%"


def _capture_xp_notification_chat(session, callback: CallbackQuery) -> None:
    from sib.services.player_xp import PlayerXPService

    chat_id = getattr(getattr(callback.message, "chat", None), "id", None)
    PlayerXPService.set_notification_chat(session, chat_id)


async def _deliver_xp_notifications(
    bot: Bot, callback: CallbackQuery, *, fallback_chat_id: Optional[int] = None
) -> None:
    from sib.services.player_xp import PlayerXPService

    chat_id = fallback_chat_id or getattr(
        getattr(callback.message, "chat", None), "id", None
    )
    await PlayerXPService.deliver_pending_notifications(bot, fallback_chat_id=chat_id)


async def _render_garden(user_id: int, session) -> tuple[str, InlineKeyboardMarkup]:
    from sib.database.models import User

    # Read-only render: no row lock (mutations lock separately).
    garden = await GardenService.get_or_create(session, user_id)
    views = GardenService.view(garden)
    seedlings = await SeedlingService.get_total(session, user_id)
    apples = await AppleWalletService.get_balance(session, user_id)
    items = await ItemService.list_items(session, user_id)
    owner = await session.get(User, user_id)
    owner_name = (owner.first_name if owner else None) or "سیبی"

    next_price = await GardenService.next_unlock_price(session, garden)
    max_level = int(await _config(session, "garden_max_magic_level", 5))
    ready_count = sum(1 for v in views if v.state == PlotState.FRUIT_READY)

    lines = [f"🌳 **باغ سیبِ {owner_name}** 🍏\n"]
    lines.append(f"🌱 نهال‌ها: `{seedlings}` | 🍎 سیب‌ها: `{apples}`")
    if items:
        summary = " · ".join(f"{item_emoji(k)}×{q}" for k, q in items.items())
        lines.append(f"🎁 ذخیره‌ی برداشت‌ها: {summary}")
    lines.append("")

    buttons: list[list[InlineKeyboardButton]] = []
    for view in views:
        emoji = PLOT_EMOJI.get(view.state, "⬜")
        magic = f" ✨{view.magic_level}" if view.magic_level > 0 else ""
        if view.state == PlotState.GROWING:
            hint = f" — رشد: {format_duration(view.seconds_to_next_stage)}"
        elif view.state == PlotState.MATURE:
            hint = f" — میوه تا: {format_duration(view.seconds_to_next_stage)}"
        elif view.state == PlotState.FRUIT_READY:
            hint = " — **آماده برداشت!**"
        else:
            hint = " — خالی"
        lines.append(f"{emoji} قطعه {view.plot_index + 1}{magic}{hint}")

        label = f"{emoji} قطعه {view.plot_index + 1}{magic}"
        if view.state == PlotState.FRUIT_READY:
            label = f"{emoji} قطعه {view.plot_index + 1}{magic} — برداشت!"
        buttons.append([primary_button(text=label, callback_data=f"garden_tree:{view.plot_index}")])

    lines.append("")
    lines.append(f"✨ برای جزئیات، جادوی درخت و شانس سیب‌ها، قطعه را انتخاب کن. (حداکثر سطح جادویی: {max_level})")

    if next_price is not None:
        buttons.append(
            [success_button(
                text=f"➕ باز کردن قطعه بعدی — 🍎 {next_price} سیب",
                callback_data="garden_unlock",
            )]
        )
    if ready_count > 1:
        buttons.append(
            [success_button(text=f"🍇 برداشت همه ({ready_count} قطعه)", callback_data="garden_harvest_all")]
        )
    buttons.append([primary_button(text="🔄 بروزرسانی", callback_data="garden_refresh")])
    buttons.append([danger_button(text="↩️ بازگشت به منوی اصلی", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _config(session, key: str, fallback):
    from sib.services.config_store import ConfigService

    value = await ConfigService.get(session, key)
    return fallback if value is None else value


@router.message(Command("garden"))
async def cmd_garden(message: Message, bot: Bot):
    """Open the sender's garden (public view + private control)."""
    user = message.from_user
    if not user:
        return

    async with get_session() as session:
        from sib.services.banner import send_screen_reply

        text, kb = await _render_garden(user.id, session)
        sent = await send_screen_reply(message, "garden", text, kb)
        if sent is not None and sent.message_id:
            await PanelOwnershipService.claim(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                user_id=user.id,
                panel_type="garden",
            )


# ---------------------------------------------------------------------------
# Panel subject resolution + re-render helpers
# ---------------------------------------------------------------------------


async def _panel_subject(callback: CallbackQuery) -> int:
    """The garden panel's SUBJECT from the DB panel owner (authoritative).

    Uses the shared ``resolve_personal_actor`` boundary so that the presser
    must match the panel owner — defense in depth beyond the middleware.
    Returns 0 only when there is no acting user (should never happen for
    callback queries, but keeps renders safe).
    """
    user = callback.from_user
    if user is None:
        return 0
    try:
        subject_id, _msg_id = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        # Middleware should have answered and blocked; refuse to act.
        try:
            await callback.answer(LOCKED_MESSAGE, show_alert=True)
        except Exception:  # pragma: no cover - defensive
            pass
        # Return the would-be violator's id so the *render* below uses their
        # own view instead of leaking the owner's; any mutation in the same
        # handler already did not run because we raise.
        return user.id
    return subject_id


async def _rerender(callback: CallbackQuery, bot: Bot, *, answer: str = "بروزرسانی شد 🍏") -> None:
    """Re-render the garden panel (canonical banner path)."""
    from sib.services.banner import render_banner

    user = callback.from_user
    if not user:
        return
    msg_id = getattr(callback.message, "message_id", None)
    subject_id = await _panel_subject(callback)
    async with get_session() as session:
        text, kb = await _render_garden(subject_id, session)
    await safe_answer_callback(callback, answer)
    if callback.message:
        await render_banner(callback, bot, "garden", text, kb)
        new_msg_id = getattr(callback.message, "message_id", None)
        if (
            isinstance(msg_id, int)
            and isinstance(new_msg_id, int)
            and new_msg_id != msg_id
        ):
            async with get_session() as session:
                await PanelOwnershipService.move_claim(
                    session,
                    chat_id=callback.message.chat.id,
                    old_message_id=msg_id,
                    new_message_id=new_msg_id,
                    user_id=subject_id,
                    panel_type="garden",
                )


# ---------------------------------------------------------------------------
# Tree panel (per-plot details + magic)
# ---------------------------------------------------------------------------


async def _tree_view(user_id: int, plot_index: int) -> tuple[str, InlineKeyboardMarkup]:
    """One tree's screen: state, magic level/height, drop chances, actions."""
    async with get_session() as session:
        garden = await GardenService.get_or_create(session, user_id)
        views = GardenService.view(garden)
        view = next((v for v in views if v.plot_index == plot_index), None)
        if view is None:
            return (
                "این قطعه در باغ شما وجود ندارد.",
                InlineKeyboardMarkup(
                    inline_keyboard=[[danger_button(text="↩️ بازگشت به باغ", callback_data="garden_refresh")]]
                ),
            )

        max_level = int(await _config(session, "garden_max_magic_level", 5))
        weights = await DropService.weights_for_level(session, view.magic_level)
        powder = await ItemService.get_total(session, user_id, "magic_powder")
        cost = await GardenService.magic_upgrade_cost(session, view.magic_level)
        seedlings = await SeedlingService.get_total(session, user_id)

    total_weight = sum(weights.values()) or 1.0
    title = "🌳 **درخت جادویی سیب**" if view.has_tree else "⬜ **قطعه خالی**"
    lines = [
        f"{title} — قطعه {plot_index + 1}",
        "",
    ]

    if not view.has_tree:
        lines.append("این قطعه خالی است. با کاشت یک نهال 🌱 شروع کن.")
        lines.append(f"🌱 نهال‌های شما: `{seedlings}`")
    else:
        if view.state == PlotState.GROWING:
            lines.append(f"🌱 در حال رشد — تا درخت شدن: {format_duration(view.seconds_to_next_stage)}")
        elif view.state == PlotState.MATURE:
            lines.append(f"🌳 درخت بالغ — میوه تا: {format_duration(view.seconds_to_next_stage)}")
        else:
            lines.append("🍎 **میوه آماده برداشت است!**")
        lines.append("")
        lines.append(f"✨ سطح جادویی: `{view.magic_level} / {max_level}`")
        lines.append(f"📏 ارتفاع: **{magic_height_label(view.magic_level, max_level)}**")
        lines.append(f"🔄 دوره‌های برداشت: `{view.cycle_count}`")
        lines.append("")
        lines.append("**شانس برداشت این درخت:**")
        for key, emoji, label in DROP_DISPLAY:
            weight = weights.get(key, 0.0)
            lines.append(f"{emoji} {label}: `{format_percent(weight, total_weight)}`")
        lines.append("")
        lines.append(f"✨ پودر جادویی شما: `{powder}`")
        if view.magic_level >= max_level:
            lines.append("🏆 این درخت به بالاترین سطح جادویی رسیده است!")
        elif cost is not None:
            lines.append(
                f"ارتقا به سطح {view.magic_level + 1}: `{cost}` پودر جادویی "
                f"(شانس سیب‌های ویژه بیشتر می‌شه!)"
            )

    buttons: list[list[InlineKeyboardButton]] = []
    if not view.has_tree:
        buttons.append([success_button(text="🌱 کاشت نهال", callback_data=f"garden_plant:{plot_index}")])
    elif view.state == PlotState.FRUIT_READY:
        buttons.append([success_button(text="🍎 برداشت", callback_data=f"garden_harvest:{plot_index}")])
    if view.has_tree and view.magic_level < max_level and cost is not None:
        buttons.append(
            [success_button(
                text=f"✨ جادویی‌سازی ({cost} پودر)",
                callback_data=f"garden_magic:{plot_index}",
            )]
        )
    buttons.append([danger_button(text="↩️ بازگشت به باغ", callback_data="garden_refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _rerender_tree(
    callback: CallbackQuery, bot: Bot, plot_index: int, *, answer: str
) -> None:
    """Re-render the tree panel after an action on it."""
    user = callback.from_user
    if not user:
        return
    subject_id = await _panel_subject(callback)
    text, kb = await _tree_view(subject_id, plot_index)
    await safe_answer_callback(callback, answer)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("garden_tree:"))
async def handle_garden_tree(callback: CallbackQuery, bot: Bot):
    plot_index = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return
    subject_id = await _panel_subject(callback)
    text, kb = await _tree_view(subject_id, plot_index)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


@router.callback_query(F.data.startswith("garden_magic:"))
async def handle_garden_magic(callback: CallbackQuery, bot: Bot):
    plot_index = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return

    subject_id = await _panel_subject(callback)
    if subject_id != user.id:
        # Authorization already answered via _panel_subject; do not mutate.
        return

    error = None
    level = 0
    async with get_session() as session:
        try:
            _capture_xp_notification_chat(session, callback)
            level = await GardenService.apply_magic(session, subject_id, plot_index)
        except ValueError as e:
            error = str(e)

    if error:
        await _rerender_tree(callback, bot, plot_index, answer=error)
        return
    await _rerender_tree(
        callback, bot, plot_index,
        answer=f"درخت جادویی‌تر شد! سطح جدید: {level} ✨",
    )
    await _deliver_xp_notifications(bot, callback)


# ---------------------------------------------------------------------------
# Plant / harvest / harvest-all
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "garden_refresh")
async def handle_garden_refresh(callback: CallbackQuery, bot: Bot):
    await _rerender(callback, bot)


@router.callback_query(F.data.startswith("garden_plant:"))
async def handle_garden_plant(callback: CallbackQuery, bot: Bot):
    plot_index = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return

    subject_id = await _panel_subject(callback)
    if subject_id != user.id:
        return

    # State first, presentation second: the re-render runs AFTER the
    # service transaction is closed (never hold FOR UPDATE locks while
    # opening another session - that self-deadlocks).
    error = None
    async with get_session() as session:
        try:
            _capture_xp_notification_chat(session, callback)
            await GardenService.plant(session, subject_id, plot_index)
        except ValueError as e:
            error = str(e)

    if error:
        await _rerender_tree(callback, bot, plot_index, answer=error)
        return
    await _rerender_tree(callback, bot, plot_index, answer="نهال کاشته شد! 🌱")
    await _deliver_xp_notifications(bot, callback)


def _harvest_answer(outcome) -> str:
    """Toast text for one harvest outcome."""
    if outcome.is_special:
        base = f"{item_emoji(outcome.item_key)} {outcome.item_label or ''} برداشت شد!"
    else:
        base = f"{outcome.apples} سیب برداشت شد! 🍎"
    message = (outcome.message or "").strip()
    return f"{base}\n{message}" if message else base


@router.callback_query(F.data.startswith("garden_harvest:"))
async def handle_garden_harvest(callback: CallbackQuery, bot: Bot):
    plot_index = int(callback.data.split(":")[1])
    user = callback.from_user
    if not user:
        return

    subject_id = await _panel_subject(callback)
    if subject_id != user.id:
        return

    # Capture the live chat BEFORE any render: the transition engine may
    # replace the message (text<->photo), which retargets callback.message.
    chat = getattr(callback.message, "chat", None)
    chat_id = getattr(chat, "id", None)

    error = None
    outcome = None
    async with get_session() as session:
        try:
            _capture_xp_notification_chat(session, callback)
            outcome = await GardenService.harvest(session, subject_id, plot_index)
        except ValueError as e:
            error = str(e)

    if error:
        await _rerender_tree(callback, bot, plot_index, answer=error)
        return

    await _rerender_tree(callback, bot, plot_index, answer=_harvest_answer(outcome))
    await _deliver_xp_notifications(bot, callback)

    # Celebration art for special Apples (graceful: missing art never
    # breaks the flow; the toast above already carried the news). The
    # caption is the configured, rarity-escalating event text.
    if outcome.is_special and outcome.asset and chat_id:
        await _send_asset(chat_id, bot, outcome.asset, caption=outcome.caption)


async def _send_asset(
    chat_id: int,
    bot: Bot,
    asset_key: str,
    caption: Optional[str] = None,
) -> None:
    from sib.services.assets import AssetService

    async with get_session() as session:
        await AssetService.send(session, bot, chat_id, asset_key, caption=caption)


@router.callback_query(F.data == "garden_harvest_all")
async def handle_garden_harvest_all(callback: CallbackQuery, bot: Bot):
    """Harvest every ripe plot in ONE transaction (one re-render)."""
    user = callback.from_user
    if not user:
        return

    subject_id = await _panel_subject(callback)
    if subject_id != user.id:
        return

    # Capture the live chat before the render (see handle_garden_harvest).
    chat = getattr(callback.message, "chat", None)
    chat_id = getattr(chat, "id", None)

    error = None
    outcomes = []
    async with get_session() as session:
        try:
            _capture_xp_notification_chat(session, callback)
            outcomes = await GardenService.harvest_all(session, subject_id)
        except ValueError as e:
            error = str(e)

    if error:
        await _rerender(callback, bot, answer=error)
        return

    apples = sum(o.apples for o in outcomes)
    items: dict[str, int] = {}
    for o in outcomes:
        if o.item_key:
            items[o.item_key] = items.get(o.item_key, 0) + 1
    parts = [f"{apples} سیب 🍎"] if apples else []
    parts += [f"{item_emoji(k)}×{q}" for k, q in items.items()]
    answer = "برداشت همه: " + " + ".join(parts)
    await _rerender(callback, bot, answer=answer)
    await _deliver_xp_notifications(bot, callback)

    # One celebration per distinct special Apple (never spam duplicates).
    # Each asset keeps its own (rarity-escalating) caption.
    captions: dict[str, Optional[str]] = {}
    for o in outcomes:
        if o.is_special and o.asset:
            captions[o.asset] = o.caption
    if chat_id:
        for asset, caption in captions.items():
            await _send_asset(chat_id, bot, asset, caption=caption)


# ---------------------------------------------------------------------------
# Plot unlocks
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "garden_unlock")
async def handle_garden_unlock(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return

    subject_id = await _panel_subject(callback)
    if subject_id != user.id:
        return

    error = None
    capacity = 0
    async with get_session() as session:
        try:
            _capture_xp_notification_chat(session, callback)
            capacity = await GardenService.unlock_plot(session, subject_id)
        except ValueError as e:
            error = str(e)

    if error:
        await _rerender(callback, bot, answer=error)
        return
    await _rerender(
        callback, bot, answer=f"قطعه جدید باز شد! ظرفیت باغ: {capacity} قطعه 🌳"
    )
    await _deliver_xp_notifications(bot, callback)
