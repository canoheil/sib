"""Telegram UX for apple theft, wanted status and the 10-second arrest window.

Everything rules-shaped lives in :mod:`sib.services.theft_service`; this module
only decides what a player sees and when.

Arrest window
-------------
A theft by a wanted thief (1+ stars) is *pending*: no apple moves yet. Only
a clean thief's first theft settles instantly. The handler posts the
robbery message and keeps an :class:`ActiveTheft` entry in memory keyed by
``(chat_id, message_id)`` of that message. Anyone in the group who is neither
the thief nor the victim can reply «دستگیری» to it; the first one wins and sets
``caught_by_id``. After ``theft_arrest_window_seconds`` (10s) the entry is
dropped and the settlement is applied by
:func:`sib.services.theft_service.resolve_pending_theft`.

Why in memory instead of a table (migration 075)?
    The window is at most a few seconds and the bot runs as a single polling
    process (``bot.py`` -> ``dp.start_polling``, one ``sib_bot`` container), so
    the handler that opened the window is the one that settles it. A restart in
    the middle of a window drops the entry, and that is safe by construction:
    no apple has moved at that point and nothing is left half-settled, so the
    worst case is a heist that never happened. A database row would only be
    needed if the bot ever ran several worker processes, where the arresting
    update can be delivered to another process.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from sqlalchemy import select

from sib.core.bidi import fix_bidi
from sib.core.logging import logger
from sib.core.text import format_html_caption, plain_display_name
from sib.database.connection import get_session
from sib.database.models import User, UserWantedState
from sib.services import theft_service
from sib.services.config_store import ConfigService
from sib.services.sib_claim import fa_digits

router = Router(name="theft")

ASSET_DIR = Path(__file__).resolve().parents[1] / "assets"
SUCCESS_IMAGE = ASSET_DIR / "theft_success.jpg"
FAIL_IMAGE = ASSET_DIR / "theft_fail.jpg"
JAIL_IMAGE = ASSET_DIR / "jail.jpg"

# Persian Yeh (U+06CC) / Arabic Yeh (U+064A), the project's trigger convention.
_YEH = "[\u06cc\u064a]"
WANTED_PATTERN = r"^\s*س" + _YEH + r"ب\s+جرم\s*$"
THEFT_PATTERN = r"^\s*دزدی(\s+س" + _YEH + r"بی)?\s*$"
ARREST_PATTERN = r"^\s*دستگیری\s*$"

CLEAN_STATUS_TEXT = "شما هیچ ستاره‌ای ندارید. وضعیت: پاک ⭐0\nشانس دزدی بعدی: 100%"
WANTED_STATUS_TEXT = (
    "وضعیت تعقیب: {stars}\n"
    "شانس موفقیت در دزدی بعدی: {rate}%\n"
    "زمان سوختن ستاره‌ها: {remaining} باقی‌مانده"
)
JAILED_LINE = "⛓️ شما در زندان هستید! زمان آزادی: {remaining}"

JAILED_TEXT = "⛓️ شما در زندان هستید و نمی‌توانید دزدی کنید."
COOLDOWN_TEXT = "⏳ هنوز زمان دزدی بعدی نرسیده است."
NIGHT_LIMIT_TEXT = "🌙 در ساعات شب (۱۲ شب تا ۸ صبح) فقط یک بار می‌توانید دزدی کنید. برای دزدی‌های بیشتر، صبح بیایید!"
INSUFFICIENT_TEXT = "موجودی قربانی برای دزدی کافی نیست."
SELF_TEXT = "😅 نمی‌توانی از خودت دزدی کنی!"
REPLY_REQUIRED_TEXT = "🕵️ برای دزدی روی پیام قربانی ریپلای کن و بنویس «دزدی سیبی»."
NOT_A_USER_TEXT = "🕵️ این پیام مال یه کاربر نیست؛ روی پیام یه بازیکن واقعی ریپلای بزن."
PRIVATE_CHAT_TEXT = "🕵️ دزدی فقط داخل گروه‌های سیب انجام می‌شود."
UNKNOWN_USER_TEXT = "ابتدا با /start ثبت‌نام کن."
GENERIC_ERROR = "خطایی رخ داد. لطفاً دوباره تلاش کن."

INSTANT_SUCCESS_CAPTION = (
    "😈 دزدی موفق! شما {amount} سیب از {victim} دزدیدید. "
    "شما الان {stars} ستاره ⭐ دارید."
)
PENDING_CAPTION = (
    "🚨 **دزدی در جریان است!**\n"
    "{thief} در حال دزدیدن سیب‌های {victim} است.\n\n"
    "⚔️ شما {window} ثانیه فرصت دارید روی پیام من ریپلای کنید و بنویسید "
    "**«دستگیری»** تا دزد را به دام بیندازید! (به جز قربانی)"
)
CAUGHT_CAPTION = (
    "👮‍♂️ {arrester} دزد ({thief}) را دستگیر کرد! "
    "پاداش: {reward} سیب. دزد {hours} ساعت زندانی شد."
)
WINDOW_SUCCESS_CAPTION = "😈 دزدی موفق! {amount} سیب دزدیده شد."
WINDOW_FAIL_CAPTION = "😅 دزدی شکست خورد! دزد دست خالی فرار کرد."

ARREST_REGISTERED_TEXT = "👮 دستگیری ثبت شد! نتیجه به‌زودی اعلام می‌شود."
ALREADY_ARRESTED_TEXT = "👮 یک نفر زودتر از شما دزد را دستگیر کرد."
VICTIM_CANNOT_ARREST_TEXT = "❗️ قربانی نمی‌تواند در این دزدی دستگیری کند."


@dataclass
class ActiveTheft:
    """One robbery waiting out its arrest window."""

    chat_id: int
    message_id: int
    thief_id: int
    victim_id: int
    amount: int
    caught_by_id: int | None = None
    arrested: asyncio.Event = field(default_factory=asyncio.Event)


#: ``(chat_id, bot message id) -> theft``; populated for ≤10 seconds only.
ACTIVE_THEFTS: dict[tuple[int, int], ActiveTheft] = {}


# ---------------------------------------------------------------------------
# Rendering helpers (pure functions, directly unit-tested)
# ---------------------------------------------------------------------------

def next_theft_success_chance(level: int) -> int:
    """Chance the thief's *next* theft pays out, in percent.

    A clean thief (0 stars) settles instantly, so 100 %. From one star on the
    heist waits for the arrest window and the odds drop 15 % per star.
    """
    return theft_service.success_percent(level)


def format_remaining(seconds: float) -> str:
    """«۲۳ ساعت و ۵ دقیقه» style duration with Persian digits."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, remainder = divmod(rest, 60)
    if hours and minutes:
        return f"{fa_digits(hours)} ساعت و {fa_digits(minutes)} دقیقه"
    if hours:
        return f"{fa_digits(hours)} ساعت"
    if minutes:
        return f"{fa_digits(minutes)} دقیقه"
    return f"{fa_digits(remainder)} ثانیه"


def render_status(
    status: dict, *, wanted_expires_at: datetime | None, now: datetime | None = None
) -> str:
    """Wanted-status text; jail time is appended when the thief is locked up."""
    level = int(status.get("wanted_level") or 0)
    if level <= 0:
        text = CLEAN_STATUS_TEXT
    else:
        remaining = 0.0
        if wanted_expires_at is not None:
            expires = wanted_expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            moment = now or datetime.now(timezone.utc)
            remaining = (expires - moment).total_seconds()
        text = WANTED_STATUS_TEXT.format(
            # One ⭐ per star of the wanted level, not a digit.
            stars="⭐" * min(level, theft_service.MAX_WANTED_LEVEL),
            rate=fa_digits(next_theft_success_chance(level)),
            remaining=format_remaining(remaining),
        )
    if status.get("is_jailed"):
        text += "\n" + JAILED_LINE.format(
            remaining=format_remaining(status.get("jail_remaining_seconds") or 0)
        )
    return text


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _is_group(message: Message) -> bool:
    chat = getattr(message, "chat", None)
    return getattr(chat, "type", None) in ("group", "supergroup")


async def _names(session, user_ids) -> dict[int, str]:
    """``user_id -> safe display name`` for the players mentioned in a message."""
    wanted = {int(uid) for uid in user_ids if uid is not None}
    if not wanted:
        return {}
    rows = await session.execute(
        select(User.id, User.first_name).where(User.id.in_(wanted))
    )
    names = {int(uid): first_name for uid, first_name in rows.all()}
    return {
        uid: plain_display_name(names.get(uid), "یک سیبی") for uid in wanted
    }


async def _config_int(session, key: str) -> int:
    try:
        return int(await ConfigService.get_int(session, key))
    except (TypeError, ValueError, OverflowError):
        return int(ConfigService.default(key))


async def _send_photo(message: Message, image: Path, caption: str, *, answer: bool = False):
    """Send an artwork with an HTML caption, falling back to text if missing.

    Every theft caption interpolates a player name, and the thief/victim
    names are ``LRM``-fenced by ``plain_display_name``; the paragraph
    direction is pinned to RTL here because the caption opens with an emoji.
    """
    markup = format_html_caption(fix_bidi(caption))
    send_photo = message.answer_photo if answer else message.reply_photo
    if image.is_file():
        return await send_photo(
            FSInputFile(str(image)), caption=markup, parse_mode="HTML"
        )
    send_text = message.answer if answer else message.reply
    await send_text(markup, parse_mode="HTML")
    return None


# ---------------------------------------------------------------------------
# /wanted and «سیب جرم»
# ---------------------------------------------------------------------------

@router.message(Command("wanted"))
@router.message(F.text.regexp(WANTED_PATTERN))
async def handle_wanted(message: Message) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    try:
        async with get_session() as session:
            status = await theft_service.get_status(session, user_id)
            state = await session.get(UserWantedState, user_id)
            expires_at = state.wanted_expires_at if state is not None else None
    except Exception:
        logger.exception("wanted status failed (user=%s)", user_id)
        await message.reply(GENERIC_ERROR)
        return
    await message.reply(
        render_status(
            status, wanted_expires_at=expires_at, now=theft_service._now()
        )
    )


# ---------------------------------------------------------------------------
# «دزدی» / «دزدی سیبی» as a reply to the victim
# ---------------------------------------------------------------------------

@router.message(F.text.regexp(THEFT_PATTERN))
async def handle_theft(message: Message) -> None:
    sender = message.from_user
    if sender is None or getattr(sender, "is_bot", False):
        return
    if not _is_group(message):
        await message.reply(PRIVATE_CHAT_TEXT)
        return

    reply = message.reply_to_message
    if reply is None:
        await message.reply(REPLY_REQUIRED_TEXT)
        return
    victim = getattr(reply, "from_user", None)
    if victim is None or getattr(victim, "is_bot", False):
        await message.reply(NOT_A_USER_TEXT)
        return
    if victim.id == sender.id:
        await message.reply(SELF_TEXT)
        return

    try:
        async with get_session() as session:
            result = await theft_service.initiate_theft(session, sender.id, victim.id)
            status = result.get("status")
            names = (
                await _names(session, (sender.id, victim.id))
                if status in ("success", "pending")
                else {}
            )
    except Exception:
        logger.exception(
            "theft failed (thief=%s victim=%s)", sender.id, victim.id
        )
        await message.reply(GENERIC_ERROR)
        return

    if status == "jailed":
        await message.reply(JAILED_TEXT)
        return
    if status == "night_limit":
        await message.reply(NIGHT_LIMIT_TEXT)
        return
    if status == "cooldown":
        await message.reply(COOLDOWN_TEXT)
        return
    if status == "insufficient":
        await message.reply(INSUFFICIENT_TEXT)
        return
    if status == "invalid":
        await message.reply(SELF_TEXT)
        return
    if status != "success" and status != "pending":
        await message.reply(UNKNOWN_USER_TEXT)
        return

    amount = int(result.get("amount") or 0)
    thief_name = names.get(int(sender.id), "یک دزد")
    victim_name = names.get(int(victim.id), "یک سیبی")

    if status == "success":
        await _send_photo(
            message,
            SUCCESS_IMAGE,
            INSTANT_SUCCESS_CAPTION.format(
                amount=fa_digits(amount),
                victim=victim_name,
                stars=fa_digits(result.get("wanted_level") or 1),
            ),
        )
        return

    window_seconds = max(0, int(result.get("window_seconds") or 0))
    sent = await _send_photo(
        message,
        SUCCESS_IMAGE,
        PENDING_CAPTION.format(
            thief=thief_name,
            victim=victim_name,
            window=fa_digits(window_seconds),
        ),
    )
    await _settle_after_window(
        message,
        sent,
        thief_id=int(sender.id),
        victim_id=int(victim.id),
        amount=amount,
        window_seconds=window_seconds,
    )


# ---------------------------------------------------------------------------
# «دستگیری» as a reply to the pending-theft message
# ---------------------------------------------------------------------------

@router.message(F.text.regexp(ARREST_PATTERN))
async def handle_arrest(message: Message) -> None:
    sender = message.from_user
    if sender is None or getattr(sender, "is_bot", False):
        return
    reply = message.reply_to_message
    chat = getattr(message, "chat", None)
    if reply is None or chat is None:
        return

    theft = ACTIVE_THEFTS.get((chat.id, getattr(reply, "message_id", None)))
    if theft is None:
        # No robbery is in progress on that message: stay silent in the group.
        return
    if theft.caught_by_id is not None:
        await message.reply(ALREADY_ARRESTED_TEXT)
        return
    if sender.id == theft.victim_id:
        await message.reply(VICTIM_CANNOT_ARREST_TEXT)
        return
    if sender.id == theft.thief_id:
        return  # the thief cannot arrest themselves

    # First arrest wins; no await between the check and the assignment.
    theft.caught_by_id = sender.id
    theft.arrested.set()
    await message.reply(ARREST_REGISTERED_TEXT)


# ---------------------------------------------------------------------------
# The window itself
# ---------------------------------------------------------------------------

async def _arrest_window(theft: ActiveTheft, seconds: int) -> None:
    """Hold the window open for ``seconds``, ending early on a successful arrest.

    This is a plain ``asyncio.sleep`` cancelled by the arrest event, so the
    event loop is never blocked. Tests replace this function to skip the wait.
    """
    if seconds <= 0:
        return
    timer = asyncio.ensure_future(asyncio.sleep(seconds))
    arrest = asyncio.ensure_future(theft.arrested.wait())
    try:
        await asyncio.wait({timer, arrest}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (timer, arrest):
            task.cancel()
        await asyncio.gather(timer, arrest, return_exceptions=True)


async def _settle_after_window(
    message: Message,
    sent,
    *,
    thief_id: int,
    victim_id: int,
    amount: int,
    window_seconds: int,
) -> None:
    """Open the arrest window, then settle the heist and announce the outcome."""
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    # Without artwork the caption is a plain reply, so arrests reply to that.
    message_id = getattr(sent, "message_id", None) or getattr(message, "message_id", None)
    theft = ActiveTheft(
        chat_id=chat_id or 0,
        message_id=message_id or 0,
        thief_id=thief_id,
        victim_id=victim_id,
        amount=amount,
    )
    key = (theft.chat_id, theft.message_id)
    ACTIVE_THEFTS[key] = theft
    try:
        await _arrest_window(theft, window_seconds)
    finally:
        ACTIVE_THEFTS.pop(key, None)

    caught_by_id = theft.caught_by_id
    try:
        async with get_session() as session:
            result = await theft_service.resolve_pending_theft(
                session, thief_id, victim_id, amount, caught_by_id
            )
            names = await _names(session, (thief_id, victim_id, caught_by_id))
            jail_hours = await _config_int(session, "theft_jail_hours")
    except Exception:
        logger.exception(
            "theft settlement failed (thief=%s victim=%s caught_by=%s)",
            thief_id, victim_id, caught_by_id,
        )
        await message.reply(GENERIC_ERROR)
        return

    status = result.get("status")
    if status == "caught":
        await _send_photo(
            message,
            JAIL_IMAGE,
            CAUGHT_CAPTION.format(
                arrester=names.get(int(caught_by_id), "یک بازیکن"),
                thief=names.get(int(thief_id), "یک دزد"),
                reward=fa_digits(result.get("reward") or 0),
                hours=fa_digits(max(0, jail_hours)),
            ),
            answer=True,
        )
        return
    if status == "success":
        await _send_photo(
            message,
            SUCCESS_IMAGE,
            WINDOW_SUCCESS_CAPTION.format(
                amount=fa_digits(result.get("amount") or amount)
            ),
            answer=True,
        )
        return
    if status == "failed":
        await _send_photo(message, FAIL_IMAGE, WINDOW_FAIL_CAPTION, answer=True)
        return
    await message.reply(GENERIC_ERROR)


def is_valid_trigger(pattern: str, text: str | None) -> bool:
    """Whether ``text`` triggers a command pattern (used by tests/docs)."""
    return bool(text) and re.match(pattern, text, flags=re.UNICODE) is not None
