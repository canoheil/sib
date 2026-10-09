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

Failure policy (same as Sib Hug): ``get_session()`` rolls the transaction
back before any error reply, so a failed open never leaves a consumed claim
or a half-delivered reward. Stale/duplicate presses are answered by the
already-claimed screen — never a second reward.
"""

import re
from datetime import datetime, timezone
from typing import Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.database.models import DailyChestClaim
from sib.keyboards.common import danger_button, primary_button
from sib.services.daily_chest import (
    ChestClaimResult,
    ChestConfigError,
    DailyChestError,
    DailyChestService,
    format_claimed_line,
    format_next_reset,
    format_open_result,
    format_reward_line,
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
    "🍎 سیب معمولی · 🌱 نهال · 🪙 سیب طلایی · "
    "💎 سیب الماسی · 🌑 سیب ابسیدینی · 🎴 کارت شخصیت"
)
CHEST_AVAILABLE_TEXT = (
    f"{CHEST_TITLE}\n\n"
    "هر روز یک بار بازش کن؛ یک جایزه‌ی تصادفی مال توئه! 🍏\n"
    f"{CHEST_POOL_LINE}\n\n"
    "صندوقچه‌ی امروز آماده‌ست. 👇"
)
CHEST_DISABLED_TEXT = f"{CHEST_TITLE}\n\nصندوقچه‌ی گنج فعلاً بسته‌ست. بعداً دوباره سر بزن! 🍏"
CHEST_CONFIG_ERROR_TEXT = "پیکربندی صندوقچه موقتاً مشکل داره؛ بعداً دوباره تلاش کن. 🍏"
CHEST_FAILURE_TEXT = "صندوقچه باز نشد. یه لحظه‌ی دیگه دوباره امتحان کن. 🍏"
OPEN_TOAST = "صندوقچه باز شد! 🎁"
ALREADY_TOAST = "این یکی رو امروز باز کردی! ⏳"
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
    "هر عضو گروه می‌تونه امروز یک بار صندوقچه رو باز کنه؛ جایزه‌ی هر نفر مال خودشه! 🍏\n"
    f"{CHEST_POOL_LINE}\n\n"
    "صندوقچه‌ی امروز آماده‌ست و تا نیمه‌شب گرینویچ معتبره. 👇"
)
GROUP_ONLY_TEXT = "این صندوقچه فقط داخل گروه‌ها باز می‌شه. 🍏"
GROUP_INVALID_TEXT = "این دکمه معتبر نیست؛ صندوقچه‌ی تازه رو از منوی گروه باز کن. 🍏"
GROUP_EXPIRED_TEXT = (
    "این صندوقچه مال روز قبله و منقضی شده؛ از منوی گروه یه صندوقچه‌ی تازه بگیر. 🍏"
)


def _keyboard(*, can_open: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_open:
        rows.append([primary_button(OPEN_BUTTON_TEXT, "daily_chest:open")])
    rows.append([danger_button(BACK_BUTTON_TEXT, "main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _claimed_screen(claim: DailyChestClaim, now: datetime) -> str:
    return (
        f"{CHEST_TITLE}\n\n"
        "صندوقچه‌ی امروز رو باز کردی! ✅\n"
        f"جایزه‌ی امروز: {format_claimed_line(claim)}\n\n"
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
        return _claimed_screen(claim, now), _keyboard(can_open=False)
    return CHEST_AVAILABLE_TEXT, _keyboard(can_open=True)


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
@router.message(Command("chest"))
async def cmd_chest(message: Message) -> None:
    """/chest — open the sender's daily chest screen."""
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
    return f"🎁 {_mention(user.id, user.first_name)}، صندوقچه‌ی امروزت باز شد!\n{reward}"


async def _post_group_chest(callback: CallbackQuery, bot: Bot, chat_id: int) -> None:
    """Group menu press: post the shared closed-chest message in this group."""
    try:
        async with get_session() as session:
            config = await DailyChestService.load_config(session)
    except ChestConfigError:
        await safe_answer_callback(callback, CHEST_CONFIG_ERROR_TEXT, show_alert=True)
        return
    except Exception:
        logger.exception("daily chest group screen failed")
        await safe_answer_callback(callback, CHEST_FAILURE_TEXT, show_alert=True)
        return
    if not config.enabled:
        await safe_answer_callback(callback, CHEST_DISABLED_TEXT, show_alert=True)
        return

    # Photo first. safe_send_screen degrades to the same caption and button as
    # plain text when the artwork is missing or the upload fails.
    day = utc_day(_utc_now())
    sent = await safe_send_screen(
        bot,
        chat_id,
        GROUP_CHEST_TEXT,
        photo=scene_photo(GROUP_CHEST_SCENE),
        reply_markup=_group_open_keyboard(day),
        parse_mode=None,
    )
    if sent is None:
        logger.warning(f"daily chest post could not be sent in chat {chat_id}")
        await safe_answer_callback(callback, CHEST_FAILURE_TEXT, show_alert=True)
        return
    remember_scene_file_id(GROUP_CHEST_SCENE, sent)
    await safe_answer_callback(callback)


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
