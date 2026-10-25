"""Telegram entry point for the Daily Sib Treasure Chest («صندوقچه گنج روزانه»).

Entry points:

* ``/chest`` — open the chest screen (command pattern, like ``/garden``).
* «🎁 صندوقچه گنج روزانه» on the private main menu (``apple_chest``); text-only.
* «🎁 صندوقچه گنج روزانه» on the GROUP main menu (``group_chest``): posts ONE
  shared closed-chest message (1:1 artwork ``assets/daily_chest/chest.jpg``)
  in the same group. Its «🔓 باز کردن صندوقچه» button (``group_chest:open:
  <UTC day>``) is public, never owner-scoped: every member who presses it gets
  THEIR OWN once-per-UTC-day reward, announced in the group after the commit.
  Refusals (already claimed, expired, malformed, disabled, failed) are private
  alerts. A post belongs to the UTC day it was made on, so an older post is
  "expired" and grants nothing.

The private screen shows whether today's chest is available, the exact reward
of an already-made claim and the next UTC reset. «باز کردن صندوقچه» (``daily_chest:
open``) is OWNER-SCOPED through the shared panel guard and delegates every
rule to ``DailyChestService``; the handler never rolls rewards itself.

Group shortcut «سیب صندوق» (text, exact phrase): in a group it runs the menu
button's flow, so both share ONE cooldown per group (``group_chest_cooldown_seconds``,
default 30 s). The slot is a compare-and-swap row in ``system_config``, so it
holds across workers and restarts. The cooldown limits only creating a post;
opening is never throttled. In private chat the shortcut opens the private
chest exactly like ``/chest``.

Failure policy (same as Sib Hug): ``get_session()`` rolls the transaction
back before any error reply, so a failed open never leaves a consumed claim
or a half-delivered reward. Stale/duplicate presses are answered by the
already-claimed screen — never a second reward.
"""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError, OperationalError

from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.database.models import DailyChestClaim
from sib.keyboards.common import danger_button, primary_button
from sib.services.daily_chest import (
    GROUP_POST_COOLDOWN,
    GROUP_POST_DISABLED,
    ChestClaimResult,
    ChestConfigError,
    DailyChestError,
    DailyChestService,
    GroupPostSlot,
    GroupSlotContention,
    format_claimed_line,
    format_next_reset,
    format_open_result,
    format_reward_line,
    format_streak_line,
    next_reset_at,
    utc_day,
)
from sib.services.marriage_art import remember_scene_file_id, scene_photo
from sib.services.panel import (
    LOCKED_MESSAGE,
    PanelAuthorizationError,
    PanelOwnershipService,
    resolve_personal_actor,
)
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import (
    safe_answer_callback,
    safe_edit_callback_message,
    safe_send_screen,
)
from sib.services.user import UserService

router = Router(name="daily_chest")

PANEL_TYPE = "chest"

CHEST_TITLE = "🎁 صندوقچه گنج روزانه سیب"
CHEST_POOL_LINE = (
    "🍎 سیب معمولی · 🌱 نهال · 🌕 سیب طلایی · "
    "💎 سیب الماسی · 🌑 سیب ابسیدینی · 🎴 کارت شخصیت"
)
CHEST_AVAILABLE_TEXT = (
    f"{CHEST_TITLE}\n\n"
    "هر روز یه جایزه‌ی تصادفی بردار!\n"
    f"{CHEST_POOL_LINE}\n\n"
    "صندوق امروز آماده‌ست. 👇"
)
CHEST_DISABLED_TEXT = f"{CHEST_TITLE}\n\nفعلاً بسته‌ست؛ بعداً سر بزن!"
CHEST_CONFIG_ERROR_TEXT = "🎁 تنظیمات صندوقچه مشکل داره؛ بعداً دوباره تلاش کن."
CHEST_FAILURE_TEXT = "🎁 صندوقچه باز نشد؛ دوباره امتحان کن."
OPEN_TOAST = "🎁 صندوقچه باز شد!"
ALREADY_TOAST = "⏳ صندوق امروز رو باز کردی!"
OPEN_BUTTON_TEXT = "🎁 باز کردن صندوقچه"
BACK_BUTTON_TEXT = "🏠 منوی اصلی"

# --- Group chest (shared post) ---------------------------------------------
GROUP_CHEST_CALLBACK = "group_chest"  # group main-menu button
GROUP_OPEN_CALLBACK_PREFIX = "group_chest:open:"  # + UTC day of the post
GROUP_OPEN_BUTTON_TEXT = "🔓 باز کردن صندوقچه"
GROUP_CHEST_SCENE = "daily_chest"  # marriage_art.GROUP_SCENES key (1:1 art)
GROUP_CHAT_TYPES = ("group", "supergroup")
_GROUP_DAY_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # ASCII digits only

GROUP_CHEST_TEXT = (
    f"{CHEST_TITLE}\n\n"
    "هر عضو می‌تونه یه بار بازش کنه؛ جایزه‌اش مال خودشه!\n"
    f"{CHEST_POOL_LINE}\n\n"
    "صندوق امروز تا نیمه‌شب گرینویچ بازه. 👇"
)
GROUP_ONLY_TEXT = "🎁 این صندوقچه فقط توی گروه باز میشه."
GROUP_INVALID_TEXT = "🎁 این دکمه معتبر نیست؛ یه صندوق تازه از منوی گروه بگیر."
GROUP_EXPIRED_TEXT = "⏳ این صندوقچه منقضی شده! یه صندوق تازه از منوی گروه بگیر."


# «سیب صندوق» (text shortcut). Persian «ی» and Arabic «ي» both match.
_YEH = "[\\u06cc\\u064a]"
GROUP_SHORTCUT_PATTERN = r"^\s*س" + _YEH + r"ب\s+صندوق\s*$"
GROUP_COOLDOWN_TEXT = "⏳ صندوق تازه پست شده؛ {seconds} ثانیه‌ی دیگه دوباره امتحان کن."
# A slot race is re-read in a fresh transaction. A request that keeps losing
# fails safely (no post, no reward) instead of waiting.
GROUP_SLOT_ATTEMPTS = 5
GROUP_OUTCOME_POSTED = "posted"
GROUP_OUTCOME_COOLDOWN = "cooldown"
GROUP_OUTCOME_DISABLED = "disabled"
GROUP_OUTCOME_CONFIG_ERROR = "config_error"
GROUP_OUTCOME_FAILED = "failed"


def _keyboard(*, can_open: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_open:
        rows.append([primary_button(OPEN_BUTTON_TEXT, "daily_chest:open")])
    rows.append([danger_button(BACK_BUTTON_TEXT, "main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _streak_screen_line(session, user_id: int) -> str:
    """Read-only streak line for the chest screen (never pays anything)."""
    from sib.services.daily_streak import DailyStreakService

    try:
        streak = await DailyStreakService.summary(session, user_id)
    except Exception:  # pragma: no cover - the screen must always render
        logger.exception("daily streak screen line failed user=%s", user_id)
        return ""
    return format_streak_line(
        streak.days,
        next_milestone=streak.next_milestone,
        days_to_next=streak.days_to_next,
    )


def _claimed_screen(
    claim: DailyChestClaim, now: datetime, *, streak_line: str = ""
) -> str:
    streak_block = f"{streak_line}\n\n" if streak_line else ""
    return (
        f"{CHEST_TITLE}\n\n"
        f"✅ امروز {format_claimed_line(claim)} گرفتی.\n\n"
        f"{streak_block}"
        f"{format_next_reset(next_reset_at(now), now)}"
    )


def _result_screen(result: ChestClaimResult, now: datetime) -> str:
    return format_open_result(result, now)


async def _render_screen(
    user_id: int, session
) -> Tuple[str, InlineKeyboardMarkup]:
    """Chest screen for one player: available / claimed / disabled."""
    config = await DailyChestService.load_config(session)
    now = datetime.now(timezone.utc)
    if not config.enabled:
        return CHEST_DISABLED_TEXT, _keyboard(can_open=False)

    claim = await DailyChestService.get_today_claim(session, user_id, now=now)
    if claim is not None:
        return (
            _claimed_screen(
                claim, now, streak_line=await _streak_screen_line(session, user_id)
            ),
            _keyboard(can_open=False),
        )

    # Not claimed yet: show the streak that is on the line today, so the
    # "don't lose it" pressure is visible BEFORE the player opens the chest.
    streak_line = await _streak_screen_line(session, user_id)
    text = CHEST_AVAILABLE_TEXT
    if streak_line:
        text = f"{CHEST_AVAILABLE_TEXT}\n\n{streak_line}"
    return text, _keyboard(can_open=True)


async def _claim_panel_for(
    callback: CallbackQuery, user_id: int, prev_message_id: Optional[int]
) -> None:
    """Bind the chest panel message to its opener (DB, authoritative)."""
    chat = getattr(callback.message, "chat", None)
    msg_id = getattr(callback.message, "message_id", None)
    if chat is None or not isinstance(msg_id, int):
        return
    async with get_session() as session:
        if isinstance(prev_message_id, int) and prev_message_id != msg_id:
            await PanelOwnershipService.move_claim(
                session,
                chat_id=chat.id,
                old_message_id=prev_message_id,
                new_message_id=msg_id,
                user_id=user_id,
                panel_type=PANEL_TYPE,
            )
        else:
            await PanelOwnershipService.claim(
                session,
                chat_id=chat.id,
                message_id=msg_id,
                user_id=user_id,
                panel_type=PANEL_TYPE,
            )


async def _panel_subject(callback: CallbackQuery) -> Optional[int]:
    """Authorized actor for a mutating chest press (defense in depth)."""
    user = callback.from_user
    if user is None:
        return None
    try:
        subject_id, _msg_id = await resolve_personal_actor(callback)
    except PanelAuthorizationError:
        try:
            await callback.answer(LOCKED_MESSAGE, show_alert=True)
        except Exception:  # pragma: no cover - defensive
            pass
        return None
    return subject_id


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
async def _send_private_chest(message: Message) -> None:
    """The sender's private chest screen: ``/chest`` and «سیب صندوق» in private."""
    user = message.from_user
    if user is None:
        return
    try:
        async with get_session() as session:
            await UserService.get_or_create_user(session, user)
            text, kb = await _render_screen(user.id, session)
    except ChestConfigError:
        await message.reply(CHEST_CONFIG_ERROR_TEXT)
        return
    except Exception:
        logger.exception("daily chest screen failed")
        await message.reply(CHEST_FAILURE_TEXT)
        return
    await message.reply(text, reply_markup=kb)


@router.message(Command("chest"))
async def cmd_chest(message: Message) -> None:
    """/chest — open the sender's daily chest screen."""
    await _send_private_chest(message)


@router.callback_query(F.data == "apple_chest")
async def handle_apple_chest(callback: CallbackQuery, bot: Bot) -> None:
    """Open the chest screen from the private main menu button."""
    user = callback.from_user
    if user is None:
        return
    prev_message_id = getattr(callback.message, "message_id", None)
    try:
        async with get_session() as session:
            await UserService.get_or_create_user(session, user)
            text, kb = await _render_screen(user.id, session)
    except ChestConfigError:
        await safe_answer_callback(callback, CHEST_CONFIG_ERROR_TEXT, show_alert=True)
        return
    except Exception:
        logger.exception("daily chest screen failed")
        await safe_answer_callback(callback, CHEST_FAILURE_TEXT, show_alert=True)
        return
    await safe_answer_callback(callback)
    if callback.message:
        await safe_edit_callback_message(callback, bot, text, reply_markup=kb)
        await _claim_panel_for(callback, user.id, prev_message_id)


@router.callback_query(F.data == "daily_chest:open")
async def handle_chest_open(callback: CallbackQuery, bot: Bot) -> None:
    """Open today's chest (owner-scoped; exactly one reward per day)."""
    subject_id = await _panel_subject(callback)
    if subject_id is None:
        return

    user = callback.from_user
    now = datetime.now(timezone.utc)
    result: Optional[ChestClaimResult] = None
    try:
        async with get_session() as session:
            await UserService.get_or_create_user(session, user)
            result = await DailyChestService.claim(session, subject_id, now=now)
            if result.status == "claimed":
                text = _result_screen(result, now)
            elif result.claim is not None:
                text = _claimed_screen(result.claim, now)
            else:  # pragma: no cover - claim() always yields one of the two
                text = CHEST_FAILURE_TEXT
    except ChestConfigError:
        await safe_answer_callback(callback, CHEST_CONFIG_ERROR_TEXT, show_alert=True)
        return
    except DailyChestError as error:
        logger.warning(f"daily chest refused for {subject_id}: {error}")
        await safe_answer_callback(callback, str(error), show_alert=True)
        return
    except Exception:
        logger.exception("daily chest open failed")
        await safe_answer_callback(callback, CHEST_FAILURE_TEXT, show_alert=True)
        return

    claimed_now = result.status == "claimed"
    await safe_answer_callback(
        callback, OPEN_TOAST if claimed_now else ALREADY_TOAST, show_alert=not claimed_now
    )
    if callback.message:
        # After any open attempt the daily claim is consumed: the screen
        # keeps only navigation until the next UTC reset.
        await safe_edit_callback_message(
            callback, bot, text, reply_markup=_keyboard(can_open=False)
        )


# ---------------------------------------------------------------------------
# Group chest: one shared post; every reward goes to the presser
# ---------------------------------------------------------------------------
def _utc_now() -> datetime:
    """Clock seam for the group chest (tests pin the UTC day through it)."""
    return datetime.now(timezone.utc)


def parse_group_chest_callback(data: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    """Strict parse of the group chest callbacks.

    ``group_chest`` -> ``("menu", None)``; ``group_chest:open:<YYYY-MM-DD>`` ->
    ``("open", day)``. Anything else (missing or extra segments, non-ASCII
    digits, an impossible date) is ``None`` and never reaches a reward.
    """
    if data == GROUP_CHEST_CALLBACK:
        return "menu", None
    if not isinstance(data, str) or not data.startswith(GROUP_OPEN_CALLBACK_PREFIX):
        return None
    day = data[len(GROUP_OPEN_CALLBACK_PREFIX) :]
    if _GROUP_DAY_PATTERN.fullmatch(day) is None:
        return None
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    return "open", day


def _group_chat_id(callback: CallbackQuery) -> Optional[int]:
    """Id of the group the pressed button lives in, or None outside groups."""
    message = callback.message
    chat = getattr(message, "chat", None) if message is not None else None
    if getattr(chat, "type", None) not in GROUP_CHAT_TYPES:
        return None
    chat_id = getattr(chat, "id", None)
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        return None
    return chat_id


def _group_open_keyboard(day: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [primary_button(GROUP_OPEN_BUTTON_TEXT, f"{GROUP_OPEN_CALLBACK_PREFIX}{day}")]
        ]
    )


def _mention(user_id: int, first_name: Optional[str]) -> str:
    """Markdown mention link with a Markdown-safe display name."""
    return f"[{safe_name(first_name, 'یک سیبی')}](tg://user?id={int(user_id)})"


def _group_reward_notice(user, result: ChestClaimResult) -> str:
    """Public group line naming the presser: their own reward, nobody else's."""
    reward = format_reward_line(
        result.reward_type or "",
        result.quantity,
        level_bonus=result.level_bonus,
        player_level=result.player_level,
        character=result.character,
        card=result.card,
    )
    return f"🎁 {_mention(user.id, user.first_name)} صندوقچه رو باز کرد!\n{reward}"


@dataclass(frozen=True)
class _GroupPostOutcome:
    status: str
    remaining: float = 0.0
    notify: bool = False


def _group_cooldown_text(remaining: float) -> str:
    return GROUP_COOLDOWN_TEXT.format(seconds=fa_digits(max(1, math.ceil(remaining))))


async def _reserve_group_slot(chat_id: int, *, now: datetime, notify: bool) -> GroupPostSlot:
    """Reserve in a short transaction of its own; a race is re-read in a fresh one."""
    for attempt in range(1, GROUP_SLOT_ATTEMPTS + 1):
        try:
            async with get_session() as session:
                return await DailyChestService.reserve_group_post(
                    session, chat_id, now=now, notify=notify
                )
        except (IntegrityError, OperationalError, GroupSlotContention):
            if attempt == GROUP_SLOT_ATTEMPTS:
                raise
            logger.info(f"group chest slot raced in chat {chat_id} (attempt {attempt})")
    raise GroupSlotContention(str(chat_id))  # unreachable: the loop returns or raises


async def _release_group_slot(chat_id: int, token: Optional[str]) -> None:
    if token is None:
        return
    try:
        async with get_session() as session:
            await DailyChestService.release_group_post(session, chat_id, token)
    except Exception:
        # Not fatal: the cooldown simply runs out on its own.
        logger.exception("daily chest group slot release failed")


async def _post_group_interface(bot: Bot, chat_id: int, *, notify: bool) -> _GroupPostOutcome:
    """Post the group's shared chest, at most once per cooldown.

    The reservation commits before any Telegram call, so no database lock is
    held across a send. If nothing reaches the group, the reservation is
    released, so a transient failure cannot lock the group. The menu button
    (``notify=False``) and «سیب صندوق» (``notify=True``) both come through here.
    """
    now = _utc_now()
    try:
        slot = await _reserve_group_slot(chat_id, now=now, notify=notify)
    except ChestConfigError:
        return _GroupPostOutcome(GROUP_OUTCOME_CONFIG_ERROR)
    except Exception:
        logger.exception("daily chest group slot reservation failed")
        return _GroupPostOutcome(GROUP_OUTCOME_FAILED)
    if slot.status == GROUP_POST_DISABLED:
        return _GroupPostOutcome(GROUP_OUTCOME_DISABLED)
    if slot.status == GROUP_POST_COOLDOWN:
        return _GroupPostOutcome(
            GROUP_OUTCOME_COOLDOWN, remaining=slot.remaining, notify=slot.notify
        )

    # Photo first. safe_send_screen degrades to the same caption and button as
    # plain text when the artwork is missing or the upload fails. Any other
    # error still reaches the release below, so it can never hold the group.
    try:
        sent = await safe_send_screen(
            bot,
            chat_id,
            GROUP_CHEST_TEXT,
            photo=scene_photo(GROUP_CHEST_SCENE),
            reply_markup=_group_open_keyboard(utc_day(now)),
            parse_mode=None,
        )
    except Exception:
        logger.exception(f"daily chest group post raised in chat {chat_id}")
        sent = None
    if sent is None:
        logger.warning(f"daily chest post could not be sent in chat {chat_id}")
        await _release_group_slot(chat_id, slot.token)
        return _GroupPostOutcome(GROUP_OUTCOME_FAILED)
    remember_scene_file_id(GROUP_CHEST_SCENE, sent)
    return _GroupPostOutcome(GROUP_OUTCOME_POSTED)


async def _post_group_chest(callback: CallbackQuery, bot: Bot, chat_id: int) -> None:
    """Group menu press: the shared post, with the outcome answered privately."""
    outcome = await _post_group_interface(bot, chat_id, notify=False)
    if outcome.status == GROUP_OUTCOME_POSTED:
        await safe_answer_callback(callback)
    elif outcome.status == GROUP_OUTCOME_COOLDOWN:
        await safe_answer_callback(callback, _group_cooldown_text(outcome.remaining))
    elif outcome.status == GROUP_OUTCOME_DISABLED:
        await safe_answer_callback(callback, CHEST_DISABLED_TEXT, show_alert=True)
    elif outcome.status == GROUP_OUTCOME_CONFIG_ERROR:
        await safe_answer_callback(callback, CHEST_CONFIG_ERROR_TEXT, show_alert=True)
    else:
        await safe_answer_callback(callback, CHEST_FAILURE_TEXT, show_alert=True)


async def _safe_reply(message: Message, text: str) -> None:
    """Reply in the chat without letting a Telegram error escape the handler."""
    try:
        await message.reply(text)
    except Exception as error:
        logger.warning(f"group chest reply failed: {error}")


@router.message(F.text.regexp(GROUP_SHORTCUT_PATTERN))
async def handle_chest_shortcut(message: Message, bot: Bot) -> None:
    """«سیب صندوق»: the group's shared chest, or the private chest in private.

    In a group it runs the menu button's flow, so both share one cooldown.
    Only refusals are voiced, and a cooldown refusal at most once per window.
    """
    if getattr(message.chat, "type", None) not in GROUP_CHAT_TYPES:
        await _send_private_chest(message)
        return
    outcome = await _post_group_interface(bot, message.chat.id, notify=True)
    if outcome.status == GROUP_OUTCOME_COOLDOWN:
        if outcome.notify:
            await _safe_reply(message, _group_cooldown_text(outcome.remaining))
    elif outcome.status == GROUP_OUTCOME_DISABLED:
        await _safe_reply(message, CHEST_DISABLED_TEXT)
    elif outcome.status == GROUP_OUTCOME_CONFIG_ERROR:
        await _safe_reply(message, CHEST_CONFIG_ERROR_TEXT)
    elif outcome.status != GROUP_OUTCOME_POSTED:
        await _safe_reply(message, CHEST_FAILURE_TEXT)


async def _open_group_chest(
    callback: CallbackQuery, bot: Bot, chat_id: int, day: str
) -> None:
    """Open press on a shared post: the PRESSER's own daily reward, never the opener's."""
    user = callback.from_user
    if user is None:
        await safe_answer_callback(callback, GROUP_INVALID_TEXT, show_alert=True)
        return

    now = _utc_now()
    if day != utc_day(now):
        await safe_answer_callback(callback, GROUP_EXPIRED_TEXT, show_alert=True)
        return

    notice: Optional[str] = None
    refusal = CHEST_FAILURE_TEXT
    try:
        async with get_session() as session:
            await UserService.get_or_create_user(session, user)
            # The same atomic claim as the private chest: a per-user row lock
            # plus the unique (user_id, claim_day) gate. A repeated press or a
            # concurrent one returns already_claimed and grants nothing.
            result = await DailyChestService.claim(session, user.id, now=now)
            if result.status == "claimed":
                notice = _group_reward_notice(user, result)
            else:
                refusal = f"{ALREADY_TOAST}\n{format_next_reset(result.next_reset, now)}"
    except ChestConfigError:
        refusal = CHEST_CONFIG_ERROR_TEXT
    except DailyChestError as error:
        logger.warning(f"daily chest group open refused for {user.id}: {error}")
        refusal = str(error)
    except Exception:
        logger.exception("daily chest group open failed")

    # The transaction is closed (committed or rolled back) before any Telegram
    # call below, so no row lock is held across the answer or the group notice.
    if notice is None:
        await safe_answer_callback(callback, refusal, show_alert=True)
        return
    await safe_answer_callback(callback, OPEN_TOAST)
    # Best-effort public line: a failed send never undoes the committed reward.
    await safe_send_screen(bot, chat_id, notice, parse_mode="Markdown")


@router.callback_query(F.data.startswith(GROUP_CHEST_CALLBACK))
async def handle_group_chest(callback: CallbackQuery, bot: Bot) -> None:
    """Group menu button and the shared chest post's open button.

    Deliberately public (never owner-scoped): one post serves every member,
    the reward always goes to the presser, and every refusal is answered
    privately as an alert.
    """
    chat_id = _group_chat_id(callback)
    if chat_id is None:
        await safe_answer_callback(callback, GROUP_ONLY_TEXT, show_alert=True)
        return
    parsed = parse_group_chest_callback(callback.data)
    if parsed is None:
        await safe_answer_callback(callback, GROUP_INVALID_TEXT, show_alert=True)
        return
    action, day = parsed
    if action == "menu":
        await _post_group_chest(callback, bot, chat_id)
    elif day is not None:
        await _open_group_chest(callback, bot, chat_id, day)
