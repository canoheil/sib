"""Telegram handlers for the global Halloween event («سیبومورتیس» / Sibomortis).

Phase HW2 overhaul: the ⚔️ attack button is gone — players attack by
REPLYING to the Boss Menu message with the word «حمله», every hit is
announced publicly to the group, and the boss artwork evolves as its global
HP crosses ``75% / 50% / 25%``.
"""

from datetime import datetime, timezone
from typing import Any, Optional, Sequence, Tuple

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message

from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.database.models import HalloweenLootDrop
from sib.keyboards.halloween import (
    get_halloween_main_keyboard,
    get_halloween_reward_track_keyboard,
    get_loot_drop_keyboard,
)
from sib.services.assets import AssetService
from sib.services.character_art import photo_arg
from sib.services.group import GroupService
from sib.services.halloween import (
    AMBER_CARD_IMAGE_PATH,
    ATTACK_CRIT_CHANCE,
    EVENT_DISABLED_ALERT,
    LOOT_DROP_VARIANTS,
    SIBOMORTIS_IMAGE_PATH,
    SeedLootStatus,
    HalloweenBossDefeatedError,
    HalloweenCooldownError,
    HalloweenDisabledError,
    HalloweenError,
    AttackOutcome,
    HalloweenEventState,
    HalloweenNoSeedsError,
    HalloweenService,
    MilestoneStatus,
    _utc,
    boss_art_for_phase,
    boss_phase,
    boss_taunt_for_phase,
    format_duration_fa,
    milestone_reward_line,
    seed_loot_range_fa,
)
from sib.services.items import ItemService
from sib.services.panel import (
    LOCKED_MESSAGE,
    PanelAuthorizationError,
    PanelOwnershipService,
    resolve_personal_actor,
)
from sib.services.sib_claim import fa_digits
from sib.services.user import UserService
from sib.services.telegram_ui import (
    safe_answer_callback,
    safe_edit_callback_message,
    safe_edit_screen,
    safe_send_screen,
)

router = Router(name="halloween")

HALLOWEEN_COMMAND_PATTERN = r"^\s*هالوو[\u06cc\u064a]ن\s*$"

# --- HW2 TASK 1: the attack is a TEXT trigger, not a button ----------------
# Exact word «حمله», sent as a REPLY to the Boss Menu message of the player.
ATTACK_TRIGGER_WORD = "حمله"
ATTACK_TRIGGER_PATTERN = r"^\s*حمله\s*$"
# The menu's first line doubles as the "this message IS the boss menu" marker
# for replies that predate the ownership registry.
BOSS_MENU_TITLE = "🎃 **هالووین: سیبومورتیس**"
ATTACK_INSTRUCTION = (
    f"⚔️ برای حمله، روی همین پیام ریپلای کنید و بنویسید «{ATTACK_TRIGGER_WORD}»"
)

# --- HW2 TASK 2: every hit is a public group event -------------------------
ATTACK_PUBLIC_LINE = "⚔️ **{name}** به سیبومورتیس **{damage} آسیب** زد!"
ATTACK_CRIT_LINE = "💥 **ضربهٔ حیاتی!**"
ATTACK_EVOLUTION_LINE = "🎃 **سیبومورتیس دگرگون شد!** حالت تازه‌اش را ببینید!"
ATTACK_DEFEAT_LINE = "🏆 **سیبومورتیس سقوط کرد!** جایزه‌ها در «مسیر جوایز»."

_HalloweenMenuRender = Tuple[str, Any]  # (caption, photo)


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


async def _resolve_boss_photo(session, state: Optional[HalloweenEventState] = None):
    """Boss artwork for the CURRENT phase (HW2 boss evolution).

    ``halloween_boss`` / ``halloween_boss_75`` / ``halloween_boss_50`` /
    ``halloween_boss_25`` stay runtime-replaceable through ``/setasset``; the
    bundled ``assets/halloween/*.jpg`` files are the fallback, and a missing
    phase file degrades to the base portrait instead of a broken screen.
    """
    phase = boss_phase(state) if state is not None else 1
    asset_key, bundled_path = boss_art_for_phase(phase)
    file_id, fs_path = await AssetService.resolve_photo(session, asset_key)
    if file_id or fs_path:
        return photo_arg(file_id, fs_path)
    for candidate in (bundled_path, SIBOMORTIS_IMAGE_PATH):
        if candidate is not None and candidate.is_file():
            return FSInputFile(str(candidate))
    return None


# ---------------------------------------------------------------------------
# Screen formatting (Markdown, photo-caption safe)
# ---------------------------------------------------------------------------


def _remaining_seconds(ends_at: datetime, now: datetime) -> int:
    return max(0, int((_utc(ends_at) - _utc(now)).total_seconds()))


def _format_days_hours(seconds: int) -> str:
    """«۶ روز و ۴ ساعت» / «۴ ساعت» / «۱۵ دقیقه» from a raw second count."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86_400)
    if days:
        hours = rem // 3600
        return f"{fa_digits(days)} روز و {fa_digits(hours)} ساعت"
    return format_duration_fa(total)


def _format_time_remaining(ends_at: datetime, now: datetime) -> str:
    """Admin-facing countdown line (kept stable for ``/hw_status``)."""
    seconds = _remaining_seconds(ends_at, now)
    days, rem = divmod(seconds, 86_400)
    hours = rem // 3600
    return f"⏳ **{fa_digits(days)} روز و {fa_digits(hours)} ساعت**"


def _hp_bar(hp: int, max_hp: int) -> str:
    filled = max(0, min(10, round((hp * 10) / max_hp)))
    return "█" * filled + "░" * (10 - filled)


def _format_leaderboard_lines(
    top3: Sequence[Tuple[int, str, int]],
) -> list:
    """Top-3 damage dealers of the whole event (HW2 TASK 4)."""
    medals = ("🥇", "🥈", "🥉")
    lines = ["", "🏆 **برترین آسیب‌زنندگان**"]
    if not top3:
        lines.append("هنوز کسی ضربه نزده؛ اولین ضربه می‌تواند مال تو باشد!")
        return lines
    for idx, (_uid, name, damage) in enumerate(top3[:3]):
        lines.append(
            f"{medals[idx]} {safe_name(name)}: {fa_digits(int(damage))} آسیب"
        )
    return lines


def format_boss_menu_caption(
    state: HalloweenEventState,
    seeds: int,
    *,
    now: Optional[datetime] = None,
    loot: Optional[SeedLootStatus] = None,
    top3: Sequence[Tuple[int, str, int]] = (),
) -> str:
    """Boss Menu caption: live boss, seeds, exact loot timer, Top-3, hints.

    ``loot`` carries the exact per-user cooldown so the menu advertises the
    real «زمان تا غنیمت بعدی» instead of a vague "later" (HW2 TASK 3).
    """
    moment = _utc(now or datetime.now(timezone.utc))
    hp = int(state.boss_hp)
    max_hp = max(1, int(state.max_hp))
    pct = max(0, min(100, (hp * 100) // max_hp))
    phase = boss_phase(state)

    # A dead boss gets no taunt: its own defeat line closes the menu instead.
    taunt = "" if hp == 0 else boss_taunt_for_phase(phase)
    lines = [BOSS_MENU_TITLE, ""]
    if taunt:
        lines.extend([taunt, ""])
    lines.extend(
        [
            f"💀 جون: **{fa_digits(hp)} از {fa_digits(max_hp)}** (**{fa_digits(pct)}٪**)",
            f"`{_hp_bar(hp, max_hp)}`",
            f"🎃 دونه کدو: **{fa_digits(int(seeds))}**",
        ]
    )

    if loot is not None and not loot.ready:
        lines.append(
            f"⏳ زمان تا غنیمت بعدی: {format_duration_fa(loot.remaining_seconds)}"
        )
    else:
        lines.append("✅ غنیمت آماده است؛ بزن روی «دریافت غنیمت».")
    lines.append(
        f"🎁 هر غنیمت: **{seed_loot_range_fa()} دونه کدو** · "
        f"⌛ پایان: {_format_days_hours(_remaining_seconds(state.ends_at, moment))}"
    )

    lines.append("")
    lines.append(
        "💥 آسیب بر پایهٔ **قدرت کارت اصلی** توست؛ "
        f"{fa_digits(int(round(ATTACK_CRIT_CHANCE * 100)))}٪ شانس **ضربهٔ حیاتی** (دو برابر)."
    )
    lines.extend(_format_leaderboard_lines(top3))

    if hp == 0:
        lines.extend(["", "🏆 **سیبومورتیس شکست خورد!** جایزه‌ت رو از «مسیر جوایز» بگیر."])
    else:
        if state.last_day_weakened:
            lines.extend(["", "⚡ **طلسم شکست! وقت ضربه آخره.**"])
        lines.extend(["", ATTACK_INSTRUCTION])
    return "\n".join(lines)


def format_reward_track_caption(
    state: HalloweenEventState,
    statuses: Tuple[MilestoneStatus, ...],
) -> str:
    """The 75% / 50% / 25% / 0% global milestone track screen (HW2 TASK 4)."""
    hp = int(state.boss_hp)
    max_hp = max(1, int(state.max_hp))
    pct = max(0, min(100, (hp * 100) // max_hp))

    badge_for = {
        "unlocked": "🟢 آمادهٔ دریافت",
        "claimed": "✅ دریافت شده",
        "locked": "🔒 قفل",
    }

    lines = [
        "🎁 **مسیر جوایز هالووین**",
        "",
        f"💀 جون سیبومورتیس: **{fa_digits(hp)} از {fa_digits(max_hp)}** (**{fa_digits(pct)}٪**)",
        "",
        "🟢 آمادهٔ دریافت · 🔒 قفل · ✅ دریافت شده",
        "",
    ]
    for item in statuses:
        pct_fa = fa_digits(item.milestone_pct)
        badge = badge_for.get(item.state, badge_for["locked"])
        lines.append(f"**{pct_fa}٪** — {badge}")
        lines.append(f"{milestone_reward_line(item.milestone_pct)}")
        lines.append("")
    lines.append("هر جایزه برای هر بازیکن فقط یک بار قابل دریافت است.")
    return "\n".join(lines)


async def _render_menu(
    session, actor_id: int, moment: datetime
) -> _HalloweenMenuRender:
    """(caption, photo) of the Boss Menu for ``actor_id`` right now."""
    state = await HalloweenService.ensure_active_event(session, now=moment)
    seeds = await ItemService.get_total(session, actor_id, "pumpkin_seed")
    loot = await HalloweenService.get_seed_loot_status(session, actor_id, now=moment)
    top3 = await HalloweenService.get_leaderboard(session, state.id, limit=3)
    caption = format_boss_menu_caption(
        state, seeds, now=moment, loot=loot, top3=top3
    )
    return caption, await _resolve_boss_photo(session, state)


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
        caption, photo = await _render_menu(session, user.id, now)

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
# HW2 TASK 1 — interactive attack: reply «حمله» to the Boss Menu
# ---------------------------------------------------------------------------


async def _resolve_replied_menu(
    session, chat_id: int, replied: Optional[Message]
) -> Tuple[bool, Optional[int]]:
    """``(is_boss_menu, owner_user_id)`` for the message a reply targets.

    The panel registry is authoritative (the Boss Menu is a personal panel);
    the menu title is the fallback so a reply still bites when the registry
    row is missing (menu sent before HW2, edited screen, forwarded copy).
    """
    if replied is None:
        return False, None
    msg_id = getattr(replied, "message_id", None)
    if isinstance(msg_id, int) and chat_id:
        owner_id, panel_type = await PanelOwnershipService.get_panel_context(
            session, int(chat_id), msg_id, 0
        )
        if panel_type == "halloween":
            return True, int(owner_id)
    body = getattr(replied, "caption", None) or getattr(replied, "text", None) or ""
    if BOSS_MENU_TITLE in body:
        return True, None
    return False, None


async def _run_attack(
    *,
    bot: Optional[Bot],
    actor_id: int,
    display_name: Optional[str],
    chat_id: Optional[int],
    group_id: Optional[int],
    menu_message_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Tuple[Optional[AttackOutcome], Optional[str]]:
    """One attack: consume 1 seed, damage the boss, announce it, refresh menu.

    Returns ``(outcome, error_text)`` — exactly one of them is set. The DB
    work commits inside ``get_session()`` BEFORE any Telegram I/O, and a
    rejected hit (no seeds / boss dead / event off) rolls back with the
    consumed seed refunded, because the error is caught outside the session.
    """
    moment = _utc(now or datetime.now(timezone.utc))
    try:
        async with get_session() as session:
            outcome = await HalloweenService.attack_boss(
                session, actor_id, group_id=group_id, now=moment
            )
            caption, photo = await _render_menu(session, actor_id, moment)
    except (
        HalloweenNoSeedsError,
        HalloweenBossDefeatedError,
        HalloweenDisabledError,
        HalloweenError,
    ) as err:
        return None, str(err)

    # --- public hype line in the group (mandatory, HW2 TASK 2) ---
    if bot is not None and chat_id:
        text = ATTACK_PUBLIC_LINE.format(
            name=safe_name(display_name, "یک سیبی"),
            damage=fa_digits(int(outcome.damage)),
        )
        extras = []
        if outcome.is_crit:
            extras.append(ATTACK_CRIT_LINE)
        if outcome.evolved:
            extras.append(ATTACK_EVOLUTION_LINE)
        if int(outcome.boss_hp_after) == 0:
            extras.append(ATTACK_DEFEAT_LINE)
        if extras:
            text = text + "\n" + "\n".join(extras)
        await safe_send_screen(bot, int(chat_id), text, parse_mode="Markdown")

    # --- the attacker's own menu follows the live boss (new art + HP) ---
    if bot is not None and menu_message_id and chat_id:
        await safe_edit_screen(
            bot,
            int(chat_id),
            int(menu_message_id),
            caption,
            reply_markup=get_halloween_main_keyboard(),
            photo=photo,
        )
    return outcome, None


def _is_attack_trigger_word(text: Optional[str]) -> bool:
    """True for the EXACT word «حمله» (never a longer sentence containing it).

    The router filter anchors the pattern, but the handler re-checks so the
    trigger stays correct when it is called directly (tests, future routers).
    """
    return (text or "").strip() == ATTACK_TRIGGER_WORD


@router.message(F.text.regexp(ATTACK_TRIGGER_PATTERN))
async def handle_halloween_attack_trigger(message: Message, bot: Bot = None) -> None:
    """Attack Sibomortis by replying to the Boss Menu with «حمله».

    Silent unless ALL of: the event flag is on, the message is an exact reply
    trigger, and the replied-to message IS a Sibomortis Boss Menu. A player
    always attacks for THEMSELVES (their own seeds, their own card) even when
    they replied to somebody else's menu; only the owner gets the menu edit.
    """
    user = message.from_user
    if user is None:
        return
    if not _is_attack_trigger_word(getattr(message, "text", None)):
        return
    replied = getattr(message, "reply_to_message", None)
    if replied is None:
        return
    chat = message.chat
    chat_id = getattr(chat, "id", None)
    if not chat_id:
        return

    now = datetime.now(timezone.utc)
    async with get_session() as session:
        if not await HalloweenService.is_enabled(session):
            return
        is_menu, owner_id = await _resolve_replied_menu(session, int(chat_id), replied)
        if not is_menu:
            return
        group_id = await _register_actor(session, user, chat)

    _outcome, error = await _run_attack(
        bot=bot,
        actor_id=user.id,
        display_name=getattr(user, "first_name", None),
        chat_id=chat_id,
        group_id=group_id,
        menu_message_id=(
            getattr(replied, "message_id", None) if owner_id == user.id else None
        ),
        now=now,
    )
    if error:
        try:
            await message.answer(error)
        except Exception:  # pragma: no cover - defensive
            logger.warning("Halloween attack trigger error reply failed")


# ---------------------------------------------------------------------------
# Personal panel callbacks: hw:menu, hw:loot, hw:attack (legacy), hw:rewards,
# hw:claim:<pct>
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
        caption, photo = await _render_menu(session, actor_id, now)

    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback,
        bot,
        caption,
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
            top3 = await HalloweenService.get_leaderboard(session, state.id, limit=3)
            photo = await _resolve_boss_photo(session, state)
    except (HalloweenCooldownError, HalloweenDisabledError, HalloweenError) as err:
        await safe_answer_callback(callback, str(err), show_alert=True)
        return

    # Right after a claim the menu shows the FULL fresh cooldown, so the
    # advertised timer keeps ticking down exactly like the enforced one.
    loot = SeedLootStatus(
        ready=False,
        remaining_seconds=_remaining_seconds(outcome.next_claim_at, now),
        next_claim_at=outcome.next_claim_at,
    )
    caption = format_boss_menu_caption(
        state, outcome.total_seeds, now=now, loot=loot, top3=top3
    )
    await safe_answer_callback(
        callback,
        (
            f"🎃 **{fa_digits(outcome.seeds_granted)} دونه کدو** گرفتی! "
            f"غنیمت بعدی: {format_duration_fa(loot.remaining_seconds)}"
        ).replace("**", ""),
        show_alert=True,
    )
    if callback.message is not None:
        await safe_edit_callback_message(
            callback,
            bot,
            caption,
            reply_markup=get_halloween_main_keyboard(),
            photo=photo,
        )


@router.callback_query(F.data == "hw:attack")
async def handle_halloween_attack(callback: CallbackQuery, bot: Bot) -> None:
    """Legacy ⚔️ button on a Boss Menu sent BEFORE the HW2 overhaul.

    HW2 replaced the button with the «حمله» reply trigger, but stale menu
    messages still carry the old button; they run the very same attack flow
    instead of dying with a "message is old" toast.
    """
    try:
        actor_id, _ = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return

    chat = getattr(callback.message, "chat", None)
    chat_id = getattr(chat, "id", None)
    group_id = None
    async with get_session() as session:
        if callback.from_user:
            group_id = await _register_actor(session, callback.from_user, chat)

    _outcome, error = await _run_attack(
        bot=bot,
        actor_id=actor_id,
        display_name=getattr(callback.from_user, "first_name", None),
        chat_id=chat_id,
        group_id=group_id,
        menu_message_id=getattr(callback.message, "message_id", None),
    )
    # Exactly ONE callback answer: a rejected hit is shown as an alert,
    # a landed hit just dismisses the loading spinner (the public group
    # message and the refreshed menu are the real feedback).
    if error:
        await safe_answer_callback(callback, error, show_alert=True)
    else:
        await safe_answer_callback(callback)


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
        photo = await _resolve_boss_photo(session, state)

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
                else await _resolve_boss_photo(session, state)
            )
    except HalloweenError as err:
        await safe_answer_callback(callback, str(err), show_alert=True)
        return

    alert_text = f"🎁 جایزه **{fa_digits(milestone_pct)}٪**: {milestone_reward_line(milestone_pct)}!"
    if int(outcome.summary.get("top3_bonus_apples") or 0) > 0:
        alert_text += (
            f"\n🏆 جایزه سه نفر اول: **{fa_digits(outcome.summary['top3_bonus_apples'])} سیب**!"
        )
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
