"""Antidote crafting and use; the caller owns each economy transaction."""

import math
import random
import re
from datetime import date, datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger
from sib.core.text import plain_display_name, unescape_markdown
from sib.database.connection import get_session
from sib.database.models import Group, GroupDoctorState, Inv2GroupMember, User, UserDisease
from sib.keyboards.common import primary_button, success_button
from sib.services import disease_service
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.lock_order import lock_user_row, lock_user_rows_in_order
from sib.services.panel import (
    LOCKED_MESSAGE, PanelAuthorizationError, PanelOwnershipService, resolve_personal_actor,
)
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message

router = Router(name="disease")

PLANT = "disease_plant"
ANTIDOTE = "disease_antidote"
BUFF = "sib_buff"

CRAFT_SUCCESS = "✅ پادزهر آماده ساخته شد!"
MISSING_PLANT = "🌿 گیاه ویژه کافی نداری!"
MISSING_BUFF = "💥 سیب باف کافی نداری!"
MISSING_APPLES = "🍎 سیب کافی برای ساخت پادزهر نداری!"
NOT_SICK = "تو بیمار نیستی!"
MISSING_ANTIDOTE = "💊 پادزهر آماده نداری!"
USE_SUCCESS = "✅ درمان شدی! پادزهر مصرف شد."
UNKNOWN_USER = "ابتدا با /start ثبت‌نام کن."
GENERIC_ERROR = "خطایی رخ داد. لطفاً دوباره تلاش کن."


# ---------------------------------------------------------------------------
# Slash-less aliases for the three disease commands.
#
# Each pattern is anchored to the WHOLE message, like every other wake word in
# this bot, so an ordinary sentence that merely contains one of these words
# stays normal conversation. Arabic/Persian Yeh and Kaf variants and the ZWNJ /
# bidi marks players type are folded, following handlers/start.py and
# handlers/theft.py. The three phrase sets are disjoint from each other and
# from every other text shortcut in the project.
# ---------------------------------------------------------------------------
_YEH = "[\u06cc\u064a]"              # ی / ي
_KEH = "[\u06a9\u0643]"              # ک / ك
_PAD = r"[\s\u200c\u200e\u200f]*"  # whitespace, ZWNJ, bidi marks

#: «بیماری» / «درمان» -> /disease
DISEASE_TEXT_PATTERN = (
    r"^" + _PAD + r"(?:ب" + _YEH + r"مار" + _YEH + r"|درمان)" + _PAD + r"$"
)
#: «دکتر» / «پزشک» -> /doctor
DOCTOR_TEXT_PATTERN = r"^" + _PAD + r"(?:د" + _KEH + r"تر|پزش" + _KEH + r")" + _PAD + r"$"
#: «پادزهر» / «مصرف پادزهر» -> /use_antidote
USE_ANTIDOTE_TEXT_PATTERN = (
    r"^" + _PAD + r"(?:مصرف" + _PAD + r"پادزهر|پادزهر)" + _PAD + r"$"
)


def is_plain_text_trigger(pattern: str, text: str | None) -> bool:
    """Whether ``text`` is exactly one of the configured plain-text aliases."""
    return bool(text) and re.match(pattern, text) is not None


class DiseaseItemError(ValueError):
    """A safe, user-facing refusal made before any resources are changed."""


async def _craft_cost(session: AsyncSession) -> int:
    key = "disease_craft_apple_cost"
    try:
        cost = await ConfigService.get_int(session, key)
    except (TypeError, ValueError, OverflowError):
        cost = int(ConfigService.default(key))
    return max(0, cost)


async def craft_antidote(session: AsyncSession, user_id: int) -> None:
    """Trade one plant, one buff and the configured apples for one antidote.

    Lock the user first (including absent inventory rows), inventory rows in
    key order, then the wallet. All checks precede all writes; exceptions
    after a write must escape the caller's transaction so it rolls back.
    """
    try:
        await lock_user_row(session, user_id)
    except ValueError as exc:
        raise DiseaseItemError(UNKNOWN_USER) from exc

    owned = {}
    for key in sorted((ANTIDOTE, PLANT, BUFF)):
        owned[key] = await ItemService.get_total(session, user_id, key, for_update=True)
    if owned[PLANT] < 1:
        raise DiseaseItemError(MISSING_PLANT)
    if owned[BUFF] < 1:
        raise DiseaseItemError(MISSING_BUFF)

    cost = await _craft_cost(session)
    balance = await AppleWalletService.get_balance(session, user_id, for_update=True)
    if balance < cost:
        raise DiseaseItemError(MISSING_APPLES)

    await ItemService.consume(session, user_id, PLANT, 1)
    await ItemService.consume(session, user_id, BUFF, 1)
    await AppleWalletService.spend(
        session, user_id, cost, reason="disease_antidote_craft", ref_type="user_item"
    )
    await ItemService.grant(session, user_id, ANTIDOTE, 1)


async def use_antidote(session: AsyncSession, user_id: int) -> None:
    """Consume exactly one antidote only if the locked disease state is sick."""
    try:
        await lock_user_row(session, user_id)
    except ValueError as exc:
        # A player with no account cannot have a disease row either.
        raise DiseaseItemError(NOT_SICK) from exc
    disease = await session.scalar(
        select(UserDisease)
        .where(UserDisease.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if disease is None or disease.infection_level <= 0:
        raise DiseaseItemError(NOT_SICK)
    if await ItemService.get_total(session, user_id, ANTIDOTE, for_update=True) < 1:
        raise DiseaseItemError(MISSING_ANTIDOTE)

    await ItemService.consume(session, user_id, ANTIDOTE, 1)
    await disease_service.cure_user(session, user_id)


@router.message(Command("craft_antidote"))
async def handle_craft_antidote(message: Message) -> None:
    if message.from_user is None:
        return
    try:
        async with get_session() as session:
            await craft_antidote(session, message.from_user.id)
    except DiseaseItemError as exc:
        await message.reply(str(exc))
        return
    except Exception:
        logger.exception("antidote crafting failed (user=%s)", message.from_user.id)
        await message.reply(GENERIC_ERROR)
        return
    await message.reply(CRAFT_SUCCESS)


@router.message(Command("use_antidote"))
@router.message(F.text.regexp(USE_ANTIDOTE_TEXT_PATTERN))
async def handle_use_antidote(message: Message) -> None:
    if message.from_user is None:
        return
    try:
        async with get_session() as session:
            await use_antidote(session, message.from_user.id)
    except DiseaseItemError as exc:
        await message.reply(str(exc))
        return
    except Exception:
        logger.exception("antidote use failed (user=%s)", message.from_user.id)
        await message.reply(GENERIC_ERROR)
        return
    await message.reply(USE_SUCCESS)


# Deprecated: global doctor state in system_config superseded by group_doctor_states (migration 080).
DOCTOR_STATE_KEY = "disease_doctor_state"
DOCTOR_STATE_DEFAULT = '{"day":null,"user_id":null,"heals":0}'
DOCTOR_ONLY = "شما پزشک این گروه نیستید."
DOCTOR_LIMIT = "سقف درمان امروز شما تکمیل شده است."
DOCTOR_GROUP_ONLY = "برای درمان، دستور /doctor را در گروه اجرا کنید."
PATIENT_NOT_AVAILABLE = "این بیمار در این گروه قابل درمان نیست."
NO_PATIENTS = "بیمار قابل درمانی در این گروه پیدا نشد."

_DOCTOR_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utc(now: datetime | None = None) -> datetime:
    return disease_service._as_utc(now or datetime.now(timezone.utc))


def _tehran_day(now: datetime) -> str:
    return _utc(now).astimezone(disease_service.TEHRAN).date().isoformat()


def disease_profile_lines(status: dict, *, now: datetime | None = None) -> list[str]:
    """Show disease details only when sick, demon-powered, or still immune."""
    lines = []
    if status.get("is_sick"):
        lines.append(f"🦠 وضعیت: بیمار (آلودگی: {int(status.get('level', 0))}٪)")
    if status.get("has_demon_power"):
        lines.append("😈 قدرت شیطانی: فعال")
    immunity_until = status.get("immunity_until")
    if immunity_until is not None:
        remaining = (disease_service._as_utc(immunity_until) - _utc(now)).total_seconds()
        if remaining > 0:
            lines.append(f"🛡️ ایمنی موقت: تا {math.ceil(remaining / 3600)} ساعت دیگر")
    return lines


async def _locked_group_doctor_state(session: AsyncSession, chat_id: int) -> GroupDoctorState:
    """Upsert the per-group mutex row, then lock it across workers."""
    dialect = session.get_bind().dialect.name
    values = {
        "chat_id": chat_id,
        "doctor_user_id": None,
        "selected_at": _DOCTOR_EPOCH,
        "heals_today": 0,
    }
    if dialect in ("mysql", "mariadb"):
        stmt = mysql.insert(GroupDoctorState).values(**values)
        stmt = stmt.on_duplicate_key_update(chat_id=GroupDoctorState.chat_id)
    elif dialect == "sqlite":
        stmt = sqlite.insert(GroupDoctorState).values(**values).on_conflict_do_nothing(
            index_elements=["chat_id"]
        )
    else:
        raise RuntimeError(f"Unsupported doctor state dialect: {dialect}")
    await session.execute(stmt)
    return await session.scalar(
        select(GroupDoctorState)
        .where(GroupDoctorState.chat_id == chat_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def current_doctor(
    session: AsyncSession, chat_id: int, *, now: datetime | None = None
) -> int | None:
    row = await session.get(GroupDoctorState, chat_id)
    if row is None or row.doctor_user_id is None or row.selected_at is None:
        return None
    return row.doctor_user_id if _tehran_day(row.selected_at) == _tehran_day(_utc(now)) else None


async def select_daily_doctor(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[int, int]:
    """Choose a Doctor of the Day for each active group once per Tehran day.

    Active groups are active ``groups`` rows with at least one eligible member
    observed in the last 24 hours. Locking each group's state row in ``chat_id``
    order makes late maintenance restarts and concurrent workers safe.
    """
    now_utc = _utc(now)
    today = _tehran_day(now_utc)
    today_date = date.fromisoformat(today)

    rows = (
        await session.execute(
            select(Inv2GroupMember.group_id, User.id)
            .join(User, User.id == Inv2GroupMember.user_id)
            .join(Group, Group.id == Inv2GroupMember.group_id)
            .where(
                Inv2GroupMember.last_seen_at >= now_utc - timedelta(hours=24),
                Inv2GroupMember.last_seen_at <= now_utc,
                User.is_active.is_(True),
                User.is_bot.is_(False),
                User.is_banned.is_(False),
                Group.is_active.is_(True),
            )
            .order_by(Inv2GroupMember.group_id, User.id)
        )
    ).all()

    candidates_by_group: dict[int, list[int]] = {}
    for group_id, user_id in rows:
        candidates_by_group.setdefault(group_id, []).append(user_id)

    existing_chat_ids = set(
        (await session.scalars(select(GroupDoctorState.chat_id))).all()
    )
    all_chat_ids = sorted(existing_chat_ids | set(candidates_by_group))

    selected: dict[int, int] = {}
    for chat_id in all_chat_ids:
        candidates = candidates_by_group.get(chat_id)
        row = await _locked_group_doctor_state(session, chat_id)
        row_day_str = _tehran_day(row.selected_at) if row.selected_at else None
        if row_day_str is not None:
            row_date = date.fromisoformat(row_day_str)
            if row_date > today_date:
                continue
            if row_date == today_date and row.doctor_user_id is not None:
                selected[chat_id] = row.doctor_user_id
                continue
            if row_date == today_date and not candidates:
                continue

        if candidates:
            doctor_id = random.choice(candidates)
            row.doctor_user_id = doctor_id
            row.selected_at = now_utc
            row.heals_today = 0
            selected[chat_id] = doctor_id
        elif row.doctor_user_id is not None or row.heals_today != 0:
            row.doctor_user_id = None
            row.selected_at = now_utc
            row.heals_today = 0

    await session.flush()
    return selected


async def _doctor_config_int(session: AsyncSession, key: str) -> int:
    try:
        value = await ConfigService.get_int(session, key)
    except (TypeError, ValueError, OverflowError):
        value = int(ConfigService.default(key))
    return max(0, value)


async def heal_as_doctor(
    session: AsyncSession, doctor_id: int, patient_id: int, group_id: int,
    *, now: datetime | None = None,
) -> str:
    """Cure, infection roll, reward, and daily count in one transaction.

    The caller must verify Telegram group membership before entering. This
    function re-checks the recorded group association and locked disease state.
    """
    state_row = await _locked_group_doctor_state(session, group_id)
    if (
        state_row.doctor_user_id != doctor_id
        or state_row.selected_at is None
        or _tehran_day(state_row.selected_at) != _tehran_day(_utc(now))
    ):
        raise DiseaseItemError(DOCTOR_ONLY)
    limit = await _doctor_config_int(session, "disease_doctor_daily_limit")
    if state_row.heals_today >= limit:
        raise DiseaseItemError(DOCTOR_LIMIT)
    try:
        await lock_user_rows_in_order(session, (doctor_id, patient_id))
    except ValueError as exc:
        raise DiseaseItemError(PATIENT_NOT_AVAILABLE) from exc
    member = await session.scalar(
        select(Inv2GroupMember.id)
        .join(Group, Group.id == Inv2GroupMember.group_id)
        .where(Inv2GroupMember.group_id == group_id,
               Inv2GroupMember.user_id == patient_id, Group.is_active.is_(True))
    )
    patient = await session.scalar(
        select(User).where(User.id == patient_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    disease = await session.scalar(
        select(UserDisease).where(UserDisease.user_id == patient_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if (member is None or patient is None or patient.is_bot or patient.is_banned
            or not patient.is_active or disease is None or disease.infection_level <= 0):
        raise DiseaseItemError(PATIENT_NOT_AVAILABLE)

    chance = min(100, await _doctor_config_int(session, "disease_doctor_infection_chance"))
    reward = await _doctor_config_int(session, "disease_doctor_reward_apples")
    if chance > 0 and random.random() * 100 < chance:
        await disease_service.infect_user(session, doctor_id, "doctor_healing")
    await disease_service.cure_user(session, patient_id)
    await AppleWalletService.earn(
        session, doctor_id, reward, reason="disease_doctor_heal",
        ref_type="disease_patient", ref_id=patient_id,
    )
    state_row.heals_today += 1
    await session.flush()
    return patient.first_name


async def disease_menu_view(session: AsyncSession, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    status = await disease_service.get_status(session, user_id)
    lines = disease_profile_lines(status)
    if not status["is_sick"]:
        lines.insert(0, "✅ وضعیت: سالم")
    cost = await _craft_cost(session)
    text = "🦠 منوی بیماری\n" + "\n".join(lines) + (
        f"\n\nبرای ساخت: ۱ گیاه ویژه، ۱ سیب باف و {cost} سیب لازم است."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [success_button(text="🧪 ساخت پادزهر", callback_data="craft_antidote_menu")],
        [success_button(text="💊 مصرف پادزهر", callback_data="use_antidote")],
    ])
    return text, keyboard


@router.message(Command("disease"))
@router.message(F.text.regexp(DISEASE_TEXT_PATTERN))
async def handle_disease_menu(message: Message) -> None:
    if message.from_user is None:
        return
    async with get_session() as session:
        text, keyboard = await disease_menu_view(session, message.from_user.id)
    sent = await message.reply(text, reply_markup=keyboard, parse_mode=None)
    if sent is not None and sent.message_id and sent.chat:
        async with get_session() as session:
            await PanelOwnershipService.claim(
                session, sent.chat.id, sent.message_id, message.from_user.id,
                panel_type="panel_guard_personal",
            )


async def _menu_actor(callback: CallbackQuery) -> int | None:
    """Refuse unclaimed or other users' menus before any economy operation."""
    user, message = callback.from_user, callback.message
    chat = getattr(message, "chat", None)
    if user is None or chat is None or not getattr(message, "message_id", None):
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return None
    async with get_session() as session:
        owner, panel_type = await PanelOwnershipService.get_panel_context(
            session, chat.id, message.message_id, user.id
        )
    if owner != user.id or panel_type != "panel_guard_personal":
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return None
    try:
        actor, _ = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return None
    if actor != user.id:
        await safe_answer_callback(callback, LOCKED_MESSAGE, show_alert=True)
        return None
    return actor


async def _menu_action(callback: CallbackQuery, bot: Bot, *, craft: bool) -> None:
    actor = await _menu_actor(callback)
    if actor is None:
        return
    try:
        async with get_session() as session:
            if craft:
                await craft_antidote(session, actor)
            else:
                await use_antidote(session, actor)
            text, keyboard = await disease_menu_view(session, actor)
    except DiseaseItemError as exc:
        await safe_answer_callback(callback, str(exc), show_alert=True)
        return
    except Exception:
        logger.exception("disease menu action failed (user=%s)", actor)
        await safe_answer_callback(callback, GENERIC_ERROR, show_alert=True)
        return
    await safe_answer_callback(callback, CRAFT_SUCCESS if craft else USE_SUCCESS)
    await safe_edit_callback_message(callback, bot, text, reply_markup=keyboard, parse_mode=None)


@router.callback_query(F.data == "craft_antidote_menu")
async def handle_craft_antidote_menu(callback: CallbackQuery, bot: Bot) -> None:
    await _menu_action(callback, bot, craft=True)


@router.callback_query(F.data == "use_antidote")
async def handle_use_antidote_menu(callback: CallbackQuery, bot: Bot) -> None:
    await _menu_action(callback, bot, craft=False)


def _doctor_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [primary_button(text="درمان بیمار", callback_data="doc_heal_list")],
    ])


@router.message(Command("doctor"))
@router.message(F.text.regexp(DOCTOR_TEXT_PATTERN))
async def handle_doctor(message: Message) -> None:
    if message.from_user is None or message.chat is None:
        return
    kind = getattr(getattr(message.chat, "type", None), "value", getattr(message.chat, "type", None))
    doctor = None
    if kind in ("group", "supergroup"):
        async with get_session() as session:
            doctor = await current_doctor(session, message.chat.id)
    if doctor != message.from_user.id:
        await message.reply(DOCTOR_ONLY)
        return
    await message.reply("🩺 پنل پزشک روز", reply_markup=_doctor_keyboard())


def _group_id(callback: CallbackQuery) -> int | None:
    chat = getattr(callback.message, "chat", None)
    kind = getattr(getattr(chat, "type", None), "value", getattr(chat, "type", None))
    return chat.id if kind in ("group", "supergroup") else None


async def _present_group_member(bot: Bot, group_id: int, user_id: int) -> bool:
    """The stored observation is not proof that a player has not since left."""
    try:
        member = await bot.get_chat_member(group_id, user_id)
        status = getattr(member.status, "value", member.status)
        return status in ("creator", "administrator", "member") or (
            status == "restricted" and getattr(member, "is_member", False)
        )
    except Exception:
        logger.debug("Could not verify group membership user=%s group=%s", user_id, group_id, exc_info=True)
        return False


@router.callback_query(F.data == "doc_heal_list")
async def handle_doctor_heal_list(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user is None:
        return
    group_id = _group_id(callback)
    if group_id is None:
        await safe_answer_callback(callback, DOCTOR_GROUP_ONLY, show_alert=True)
        return
    async with get_session() as session:
        if await current_doctor(session, group_id) != callback.from_user.id:
            await safe_answer_callback(callback, DOCTOR_ONLY, show_alert=True)
            return
        patients = (await session.execute(
            select(User.id, User.first_name)
            .join(UserDisease, UserDisease.user_id == User.id)
            .join(Inv2GroupMember, Inv2GroupMember.user_id == User.id)
            .join(Group, Group.id == Inv2GroupMember.group_id)
            .where(Inv2GroupMember.group_id == group_id, Group.is_active.is_(True),
                   UserDisease.infection_level > 0, User.is_active.is_(True),
                   User.is_bot.is_(False), User.is_banned.is_(False))
            .order_by(User.id).limit(25)
        )).all()
    if not await _present_group_member(bot, group_id, callback.from_user.id):
        await safe_answer_callback(callback, DOCTOR_GROUP_ONLY, show_alert=True)
        return
    buttons = []
    for patient_id, first_name in patients:
        if await _present_group_member(bot, group_id, patient_id):
            name = unescape_markdown(plain_display_name(first_name, "بازیکن"))
            buttons.append([success_button(text=f"💉 {name}", callback_data=f"doc_heal:{patient_id}")])
            if len(buttons) == 5:
                break
    buttons.append([primary_button(text="🔄 بروزرسانی فهرست", callback_data="doc_heal_list")])
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback, bot, "🩺 بیمار را انتخاب کنید:" if len(buttons) > 1 else NO_PATIENTS,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=None,
    )


@router.callback_query(F.data.startswith("doc_heal:"))
async def handle_doctor_heal(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user is None or not callback.data:
        return
    group_id = _group_id(callback)
    raw = callback.data.removeprefix("doc_heal:")
    if group_id is None:
        await safe_answer_callback(callback, DOCTOR_GROUP_ONLY, show_alert=True)
        return
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
        await safe_answer_callback(callback, PATIENT_NOT_AVAILABLE, show_alert=True)
        return
    patient_id = int(raw)
    if not 0 < patient_id <= 2**63 - 1:
        await safe_answer_callback(callback, PATIENT_NOT_AVAILABLE, show_alert=True)
        return
    # Check before querying Telegram; the transaction rechecks under the mutex.
    async with get_session() as session:
        if await current_doctor(session, group_id) != callback.from_user.id:
            await safe_answer_callback(callback, DOCTOR_ONLY, show_alert=True)
            return
    # Fail closed when Telegram cannot confirm that either party is still here.
    if not await _present_group_member(bot, group_id, callback.from_user.id):
        await safe_answer_callback(callback, DOCTOR_GROUP_ONLY, show_alert=True)
        return
    if not await _present_group_member(bot, group_id, patient_id):
        await safe_answer_callback(callback, PATIENT_NOT_AVAILABLE, show_alert=True)
        return
    try:
        async with get_session() as session:
            patient_name = await heal_as_doctor(session, callback.from_user.id, patient_id, group_id)
    except DiseaseItemError as exc:
        await safe_answer_callback(callback, str(exc), show_alert=True)
        return
    except Exception:
        logger.exception("doctor heal failed (doctor=%s patient=%s)", callback.from_user.id, patient_id)
        await safe_answer_callback(callback, GENERIC_ERROR, show_alert=True)
        return
    await safe_answer_callback(callback, "✅ بیمار درمان شد.")
    doctor_name = unescape_markdown(plain_display_name(callback.from_user.first_name, "پزشک"))
    patient_name = unescape_markdown(plain_display_name(patient_name, "بازیکن"))
    try:
        await callback.message.answer(
            f"💉 {doctor_name} بیمار {patient_name} را درمان کرد.", parse_mode=None
        )
    except Exception:
        logger.exception("Could not announce doctor heal (group=%s)", group_id)
