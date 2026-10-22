"""Telegram handlers for the global Halloween event («سیبومورتیس» / Sibomortis)."""

from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message

from sib.admin.auth import is_admin
from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import AdminLog, HalloweenLootDrop
from sib.keyboards.halloween import (
    get_halloween_main_keyboard,
    get_halloween_reward_track_keyboard,
    get_loot_drop_keyboard,
)
from sib.services.assets import AssetService
from sib.services.character_art import photo_arg
from sib.services.config_store import ConfigService
from sib.services.group import GroupService
from sib.services.halloween import (
    AMBER_CARD_IMAGE_PATH,
    EVENT_DISABLED_ALERT,
    LOOT_DROP_VARIANTS,
    SIBOMORTIS_IMAGE_PATH,
    HalloweenBossDefeatedError,
    HalloweenCooldownError,
    HalloweenDisabledError,
    HalloweenError,
    HalloweenEventState,
    HalloweenNoSeedsError,
    HalloweenService,
    MilestoneStatus,
    _utc,
)
from sib.services.items import ItemService
from sib.services.panel import (
    LOCKED_MESSAGE,
    PanelAuthorizationError,
    PanelOwnershipService,
    resolve_personal_actor,
)
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.user import UserService

router = Router(name="halloween")

HALLOWEEN_COMMAND_PATTERN = r"^\s*هالوو[\u06cc\u064a]ن\s*$"


def _group_id_for_chat(chat) -> Optional[int]:
    chat_type = getattr(chat, "type", None)
    chat_type = getattr(chat_type, "value", chat_type)
    if chat_type in ("group", "supergroup"):
        return getattr(chat, "id", None)
    return None


async def _register_actor(session, user, chat) -> Optional[int]:
    await UserService.get_or_create_user(session, user)
    group_id = _group_id_for_chat(chat)
    if group_id is not None:
        group = await GroupService.get_or_create_group(session, chat)
        return group.id
    return None


async def _resolve_boss_photo(session):
    file_id, fs_path = await AssetService.resolve_photo(session, "halloween_boss")
    if file_id or fs_path:
        return photo_arg(file_id, fs_path)
    if SIBOMORTIS_IMAGE_PATH.is_file():
        return FSInputFile(str(SIBOMORTIS_IMAGE_PATH))
    return None


def _format_time_remaining(ends_at: datetime, now: datetime) -> str:
    seconds = max(0, int((_utc(ends_at) - _utc(now)).total_seconds()))
    days, rem = divmod(seconds, 86_400)
    hours = rem // 3600
    return f"⏳ **{fa_digits(days)} روز و {fa_digits(hours)} ساعت**"


def format_boss_menu_caption(
    state: HalloweenEventState,
    seeds: int,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Format the main Boss Menu caption showing Global HP, player seeds, and timer."""
    moment = _utc(now or datetime.now(timezone.utc))
    hp = int(state.boss_hp)
    max_hp = max(1, int(state.max_hp))
    filled = max(0, min(10, round((hp * 10) / max_hp)))
    bar = "█" * filled + "░" * (10 - filled)

    lines = [
        "🎃 **هالووین: سیبومورتیس**",
        "",
        f"💀 جون: **{fa_digits(hp)} از {fa_digits(max_hp)}**",
        f"`{bar}`",
        f"🎃 دونه کدو: **{fa_digits(int(seeds))}**",
        f"{_format_time_remaining(state.ends_at, moment)} باقی‌مانده",
    ]
    if state.last_day_weakened and hp > 0:
        lines.extend(
            [
                "",
                "⚡ **طلسم شکست! وقت ضربه آخره.**",
            ]
        )
    elif hp == 0:
        lines.extend(
            [
                "",
                "🏆 **سیبومورتیس شکست خورد!** جایزه‌ت رو از «مسیر جوایز» بگیر.",
            ]
        )
    return "\n".join(lines)


def format_reward_track_caption(
    state: HalloweenEventState,
    statuses: tuple[MilestoneStatus, ...],
) -> str:
    """Format the 75% / 50% / 25% / 0% global milestone track screen."""
    hp = int(state.boss_hp)
    max_hp = max(1, int(state.max_hp))
    pct = max(0, min(100, (hp * 100) // max_hp))

    reward_descriptions = {
        75: "**۱۵۰ سیب** 🍎 + **۲ دونه کدو** 🎃",
        50: "**۳ پودر جادویی** ✨ + **۲ سیب طلایی** 🌕",
        25: "**کارت امبر** 🎴 + **۱ سیب الماسی** 💎",
        0: "**۵۰۰ سیب** 🍎 + **۱ سیب ابسیدینی** 🌑 (+**۱۰۰۰ سیب ویژهٔ ۳ نفر اول** 🏆)",
    }
    badge_for = {
        "claimed": "✅ دریافت شد",
        "unlocked": "🟢 آمادهٔ دریافت",
        "locked": "🔒 قفل",
    }

    lines = [
        "🎁 **مسیر جوایز هالووین**",
        "",
        f"💀 جون: **{fa_digits(hp)} از {fa_digits(max_hp)}** (**{fa_digits(pct)}٪**)",
        "",
    ]
    for item in statuses:
        pct_fa = fa_digits(item.milestone_pct)
        badge = badge_for.get(item.state, "🔒 قفل")
        desc = reward_descriptions[item.milestone_pct]
        lines.append(f"**{pct_fa}٪** {badge}؛ {desc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry trigger: «هالووین»
# ---------------------------------------------------------------------------


@router.message(F.text.regexp(HALLOWEEN_COMMAND_PATTERN))
async def handle_halloween_command(message: Message, bot: Bot = None) -> None:
    """Open the Halloween Boss Menu ONLY when ``halloween_enabled == True``."""
    user = message.from_user
    if user is None:
        return

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        if not await HalloweenService.is_enabled(session):
            return
        await _register_actor(session, user, message.chat)
        state = await HalloweenService.ensure_active_event(session, now=now)
        seeds = await ItemService.get_total(session, user.id, "pumpkin_seed")
        photo = await _resolve_boss_photo(session)

    caption = format_boss_menu_caption(state, seeds, now=now)
    keyboard = get_halloween_main_keyboard()

    sent = None
    if photo is not None:
        try:
            sent = await message.answer_photo(
                photo=photo,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("Halloween photo send failed; falling back to text")
            sent = None
    if sent is None:
        sent = await message.answer(
            caption, reply_markup=keyboard, parse_mode="Markdown"
        )

    msg_id = getattr(sent, "message_id", None) if sent is not None else None
    chat_id = getattr(getattr(sent, "chat", None), "id", None) or getattr(
        message.chat, "id", None
    )
    if isinstance(msg_id, int) and chat_id:
        async with get_session() as session:
            await PanelOwnershipService.claim(
                session,
                chat_id=chat_id,
                message_id=msg_id,
                user_id=user.id,
                panel_type="halloween",
            )


# ---------------------------------------------------------------------------
# Personal panel callbacks: hw:menu, hw:loot, hw:attack, hw:rewards, hw:claim:<pct>
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "hw:menu")
async def handle_halloween_menu_callback(callback: CallbackQuery, bot: Bot) -> None:
    try:
        actor_id, _ = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        if not await HalloweenService.is_enabled(session):
            await safe_answer_callback(callback, EVENT_DISABLED_ALERT, show_alert=True)
            return
        state = await HalloweenService.ensure_active_event(session, now=now)
        seeds = await ItemService.get_total(session, actor_id, "pumpkin_seed")
        photo = await _resolve_boss_photo(session)

    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback,
        bot,
        format_boss_menu_caption(state, seeds, now=now),
        reply_markup=get_halloween_main_keyboard(),
        photo=photo,
    )


@router.callback_query(F.data == "hw:loot")
async def handle_halloween_loot(callback: CallbackQuery, bot: Bot) -> None:
    try:
        actor_id, _ = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return

    now = datetime.now(timezone.utc)
    try:
        async with get_session() as session:
            if callback.from_user:
                await _register_actor(
                    session, callback.from_user, getattr(callback.message, "chat", None)
                )
            outcome = await HalloweenService.claim_seed_loot(
                session, actor_id, now=now
            )
            state = await HalloweenService.ensure_active_event(session, now=now)
            photo = await _resolve_boss_photo(session)
    except (HalloweenCooldownError, HalloweenDisabledError, HalloweenError) as err:
        await safe_answer_callback(callback, str(err), show_alert=True)
        return

    await safe_answer_callback(
        callback,
        f"🎃 **{fa_digits(outcome.seeds_granted)} دونه کدو** گرفتی!".replace("**", ""),
        show_alert=True,
    )
    if callback.message is not None:
        await safe_edit_callback_message(
            callback,
            bot,
            format_boss_menu_caption(state, outcome.total_seeds, now=now),
            reply_markup=get_halloween_main_keyboard(),
            photo=photo,
        )


@router.callback_query(F.data == "hw:attack")
async def handle_halloween_attack(callback: CallbackQuery, bot: Bot) -> None:
    try:
        actor_id, _ = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return

    now = datetime.now(timezone.utc)
    chat = getattr(callback.message, "chat", None)
    try:
        async with get_session() as session:
            group_id = None
            if callback.from_user:
                group_id = await _register_actor(session, callback.from_user, chat)
            outcome = await HalloweenService.attack_boss(
                session, actor_id, group_id=group_id, now=now
            )
            state = await HalloweenService.ensure_active_event(session, now=now)
            photo = await _resolve_boss_photo(session)
    except (
        HalloweenNoSeedsError,
        HalloweenBossDefeatedError,
        HalloweenDisabledError,
        HalloweenError,
    ) as err:
        await safe_answer_callback(callback, str(err), show_alert=True)
        return

    attack_text = (
        f"⚡ ضربهٔ حیاتی! **{fa_digits(outcome.damage)} آسیب** به **سیبومورتیس** زدی!"
        if outcome.is_crit
        else f"⚔️ **{fa_digits(outcome.damage)} آسیب** به **سیبومورتیس** زدی!"
    )
    await safe_answer_callback(callback, attack_text.replace("**", ""), show_alert=True)
    if callback.message is not None:
        await safe_edit_callback_message(
            callback,
            bot,
            format_boss_menu_caption(state, outcome.remaining_seeds, now=now),
            reply_markup=get_halloween_main_keyboard(),
            photo=photo,
        )


@router.callback_query(F.data == "hw:rewards")
async def handle_halloween_rewards(callback: CallbackQuery, bot: Bot) -> None:
    try:
        actor_id, _ = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        if not await HalloweenService.is_enabled(session):
            await safe_answer_callback(callback, EVENT_DISABLED_ALERT, show_alert=True)
            return
        state, statuses = await HalloweenService.get_reward_track(
            session, actor_id, now=now
        )
        photo = await _resolve_boss_photo(session)

    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback,
        bot,
        format_reward_track_caption(state, statuses),
        reply_markup=get_halloween_reward_track_keyboard(statuses),
        photo=photo,
    )


@router.callback_query(F.data.startswith("hw:claim:"))
async def handle_halloween_claim_milestone(
    callback: CallbackQuery, bot: Bot
) -> None:
    try:
        actor_id, _ = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return

    raw_pct = (callback.data or "").removeprefix("hw:claim:")
    if not raw_pct.isdigit():
        await safe_answer_callback(callback, "مرحله نامعتبر است.", show_alert=True)
        return
    milestone_pct = int(raw_pct)
    now = datetime.now(timezone.utc)

    try:
        async with get_session() as session:
            if callback.from_user:
                await _register_actor(
                    session, callback.from_user, getattr(callback.message, "chat", None)
                )
            outcome = await HalloweenService.claim_milestone(
                session, actor_id, milestone_pct, now=now
            )
            state, statuses = await HalloweenService.get_reward_track(
                session, actor_id, now=now
            )
            photo = (
                FSInputFile(str(AMBER_CARD_IMAGE_PATH))
                if outcome.unlocked_amber and AMBER_CARD_IMAGE_PATH.is_file()
                else await _resolve_boss_photo(session)
            )
    except HalloweenError as err:
        await safe_answer_callback(callback, str(err), show_alert=True)
        return

    reward_text = {
        75: "**۱۵۰ سیب** و **۲ دونه کدو**",
        50: "**۳ پودر جادویی** و **۲ سیب طلایی**",
        25: "**۱ سیب الماسی**",
        0: "**۵۰۰ سیب** و **۱ سیب ابسیدینی**",
    }[milestone_pct]
    if outcome.unlocked_amber:
        reward_text = f"**کارت امبر** و {reward_text}"
    alert_text = f"🎁 جایزه **{fa_digits(milestone_pct)}٪**: {reward_text}!"
    if outcome.summary.get("top3_bonus_apples"):
        alert_text += "\n🏆 جایزهٔ سه نفر اول: **۱۰۰۰ سیب**!"
    await safe_answer_callback(callback, alert_text.replace("**", ""), show_alert=True)
    if callback.message is not None:
        await safe_edit_callback_message(
            callback,
            bot,
            format_reward_track_caption(state, statuses),
            reply_markup=get_halloween_reward_track_keyboard(statuses),
            photo=photo,
        )


# ---------------------------------------------------------------------------
# Public group loot chest claim: hw_drop:<drop_id>
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("hw_drop:"))
async def handle_halloween_group_drop_claim(
    callback: CallbackQuery, bot: Bot
) -> None:
    user = callback.from_user
    if user is None:
        return
    raw_id = (callback.data or "").removeprefix("hw_drop:")
    if not raw_id.isdigit():
        await safe_answer_callback(callback, "این صندوق پیدا نشد.", show_alert=True)
        return
    drop_id = int(raw_id)
    now = datetime.now(timezone.utc)

    try:
        async with get_session() as session:
            await _register_actor(
                session, user, getattr(callback.message, "chat", None)
            )
            outcome = await HalloweenService.claim_group_drop(
                session, drop_id, user.id, now=now
            )
            drop = await session.get(HalloweenLootDrop, drop_id)
            variant_code = drop.variant_code if drop else "pumpkin_chest"
    except HalloweenError as err:
        await safe_answer_callback(callback, str(err), show_alert=True)
        return

    await safe_answer_callback(
        callback,
        f"🎃 **{fa_digits(outcome.seeds_granted)} دونه کدو** گرفتی!".replace("**", ""),
        show_alert=True,
    )
    if callback.message is not None:
        base_caption, image_path = LOOT_DROP_VARIANTS.get(
            variant_code, next(iter(LOOT_DROP_VARIANTS.values()))
        )
        photo = FSInputFile(str(image_path)) if image_path.is_file() else None
        status_line = (
            "\n✅ **صندوق خالی شد!**"
            if outcome.depleted
            else f"\n📦 برداشت: **{fa_digits(outcome.claimed_count)} از {fa_digits(outcome.max_claims)}**"
        )
        await safe_edit_callback_message(
            callback,
            bot,
            base_caption + status_line,
            reply_markup=get_loot_drop_keyboard(
                drop_id, outcome.claimed_count, outcome.max_claims
            ),
            photo=photo,
        )


# Re-export authoritative Halloween admin handlers from ``sib.handlers.admin``
from sib.handlers.admin import (  # noqa: E402, F401
    handle_hw_end,
    handle_hw_start,
    handle_hw_status,
)
