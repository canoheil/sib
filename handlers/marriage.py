"""Telegram entry point for the marriage PROPOSAL flow (Phase 7.1).

Scope: proposal CREATION only. The wake phrase is typed in a group as a
REPLY to the intended partner's message; the domain creates the proposal
and the bot posts the «قبول / رد» card.

Deliberately NOT wired up here (later phases): accept/reject callbacks,
divorce, flowers, love, profile, shop and marriage history screens.

Design rules this module follows:
- Identity is NEVER taken from the text or from callback data. The
  proposer is ``message.from_user`` and the target is
  ``message.reply_to_message.from_user`` - both Telegram-authenticated.
- Business rules are NOT re-implemented here. The handler gathers the two
  identities plus the chat id and lets ``MarriageProposalService`` decide;
  domain errors are mapped to short Persian sentences.
- The database transaction closes BEFORE any Telegram send, so a row lock
  is never held across network I/O.
"""

from typing import Optional

from aiogram import F, Router
from aiogram.types import Message

from sib.core.logging import Events, log_event, logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.keyboards.marriage import get_marriage_proposal_keyboard
from sib.services.config_store import ConfigService
from sib.services.group import GroupService
from sib.services.marriage import MarriageCooldownActiveError
from sib.services.marriage_proposal import (
    DEFAULT_PROPOSAL_TIMEOUT_SECONDS,
    MarriageProposalService,
    ProposalError,
    ProposalInvalidReason,
)
from sib.services.sib_claim import fa_digits
from sib.services.user import UserService

router = Router(name="marriage")

PROPOSAL_TIMEOUT_CONFIG_KEY = "marriage_proposal_timeout_seconds"

# Persian Yeh (U+06CC) / Arabic Yeh (U+064A) and Persian Keheh (U+06A9) /
# Arabic Kaf (U+0643) are interchangeable in typed Persian; ZWNJ and the
# bidi marks appear (or do not) inside «می‌کنی» depending on the keyboard.
_YEH = "[\\u06cc\\u064a]"
_KEH = "[\\u06a9\\u0643]"
_OPTIONAL_SEP = "[\\s\\u200c\\u200e\\u200f]*"

#: Accepted wake phrases, anchored so an ordinary sentence that merely
#: contains «ازدواج» never hits this handler:
#:   «ازدواج سیبی» | «با من ازدواج می‌کنی؟» (with/without ZWNJ, spacing,
#:   trailing question mark and Arabic Yeh/Kaf variants).
PROPOSAL_TRIGGER_PATTERN = (
    r"^\s*(?:"
    rf"ازدواج\s+س{_YEH}ب{_YEH}"
    rf"|با\s+من\s+ازدواج\s+م{_YEH}{_OPTIONAL_SEP}{_KEH}ن{_YEH}"
    r")\s*[؟?]?\s*$"
)

# ---------------------------------------------------------------------------
# User-facing copy (short, friendly, Sib voice)
# ---------------------------------------------------------------------------

REPLY_REQUIRED_TEXT = (
    "💍 برای پیشنهاد ازدواج باید روی پیام مخاطبت ریپلای بزنی و بعد "
    "«ازدواج سیبی» رو بنویسی."
)

TARGET_NOT_A_USER_TEXT = (
    "💍 پیامی که ریپلای کردی از یک کاربر واقعی نیست؛ "
    "روی پیام خودِ مخاطبت ریپلای بزن."
)

PRIVATE_CHAT_TEXT = "💍 پیشنهاد ازدواج فقط داخل گروه‌های سیب معتبره. 🍏"

PROPOSAL_SEND_FAILED_TEXT = (
    "💍 نتونستم پیام پیشنهاد رو تو گروه بفرستم؛ پیشنهاد ثبت نشد. "
    "یه لحظه‌ی دیگه دوباره امتحان کن."
)

GENERIC_FAILURE_TEXT = "💍 پیشنهاد ازدواج ثبت نشد. یه لحظه‌ی دیگه دوباره امتحان کن.🍏"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_duration(seconds: int) -> str:
    """Human Persian duration with Persian digits («۵ دقیقه»)."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        if minutes:
            return f"{fa_digits(hours)} ساعت و {fa_digits(minutes)} دقیقه"
        return f"{fa_digits(hours)} ساعت"
    if minutes:
        if secs:
            return f"{fa_digits(minutes)} دقیقه و {fa_digits(secs)} ثانیه"
        return f"{fa_digits(minutes)} دقیقه"
    return f"{fa_digits(secs)} ثانیه"


def _cooldown_text(error: MarriageCooldownActiveError) -> str:
    wait = _format_duration(error.remaining_seconds)
    return (
        "💍 هنوز زوده 🍏\n\n"
        f"بعد از طلاق باید {wait} صبر کنی تا بتونی دوباره پیشنهاد ازدواج بدی."
    )


def _user_message_for(error: Exception) -> str:
    """Map a domain exception to a concise Persian sentence.

    Domain errors already carry the canonical Persian wording in
    ``.message``; the cooldown gets a friendlier dynamic sentence. Anything
    unexpected falls back to a generic line so no exception name, code or
    stack trace can ever reach the chat.
    """
    if isinstance(error, MarriageCooldownActiveError):
        return _cooldown_text(error)

    message = getattr(error, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return GENERIC_FAILURE_TEXT


def _proposal_text(proposer, target, timeout_seconds: int) -> str:
    """The group card announcing the proposal (Markdown-safe names)."""
    who = safe_name(getattr(proposer, "first_name", None), "یک سیبی")
    whom = safe_name(getattr(target, "first_name", None), "یک سیبی")
    expiry = _format_duration(timeout_seconds)
    return (
        "💍 **پیشنهاد ازدواج** 🍏\n\n"
        f"{who} از {whom} خواستگاری کرده!\n\n"
        f"{whom} جان، اگه موافقی دکمه‌ی «قبول» رو بزن و اگه نه «رد» رو.\n"
        f"⏳ این پیشنهاد فقط {expiry} اعتبار داره و بعدش خودبه‌خود منقضی می‌شه."
    )


def _coerce_timeout(raw_value: object) -> int:
    """Normalize the configured proposal lifetime (never <= 0)."""
    try:
        timeout = int(raw_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_PROPOSAL_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_PROPOSAL_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Cleanup / best-effort helpers
# ---------------------------------------------------------------------------


async def _safe_reply(message: Message, text: str) -> None:
    """Reply, but never let a presentation failure escape the handler."""
    try:
        await message.reply(text)
    except Exception:  # pragma: no cover - presentation must not break
        logger.warning("Failed to reply to a marriage proposal message")


async def _discard_undelivered_proposal(proposal_id: int) -> None:
    """Invalidate a proposal whose announcement could not be delivered.

    A PENDING proposal with no message on screen is unusable: the target
    cannot see it and it would still block the proposer's next proposal
    until it expires. ``invalidate_proposal`` is the domain's existing
    terminal transition for exactly this, and it never touches the ring.
    """
    try:
        async with get_session() as session:
            await MarriageProposalService.invalidate_proposal(
                session,
                proposal_id,
                ProposalInvalidReason.CANCELLED,
            )
    except Exception as error:  # pragma: no cover - best-effort cleanup
        logger.warning(
            f"Could not roll back undelivered marriage proposal {proposal_id}: {error}"
        )
        return

    log_event(
        Events.MARRIAGE_PROPOSAL_EVENT,
        {
            "action": "telegram_send_failed",
            "proposal_id": proposal_id,
            "reason": ProposalInvalidReason.CANCELLED,
        },
    )


async def _persist_message_id(proposal_id: int, message_id: int) -> None:
    """Attach the Telegram message id to the proposal (best effort)."""
    try:
        async with get_session() as session:
            await MarriageProposalService.set_proposal_message_id(
                session, proposal_id, message_id
            )
    except Exception as error:
        # The card is already on screen and its buttons carry the proposal
        # id, so the proposal stays usable - only later edits lose the
        # handle. Never surface this to the group.
        logger.warning(
            f"Could not persist message id {message_id} on marriage proposal "
            f"{proposal_id}: {error}"
        )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@router.message(F.text.regexp(PROPOSAL_TRIGGER_PATTERN))
async def handle_marriage_proposal(message: Message) -> None:
    """Create a marriage proposal from a REPLY to the partner's message."""
    if getattr(message.chat, "type", None) not in ("group", "supergroup"):
        await message.reply(PRIVATE_CHAT_TEXT)
        return

    proposer = message.from_user
    if proposer is None:
        return

    reply = message.reply_to_message
    if reply is None:
        await message.reply(REPLY_REQUIRED_TEXT)
        return

    target = getattr(reply, "from_user", None)
    if target is None:
        # Channel posts and anonymous admins have no user identity to
        # propose to - there is nothing valid to hand to the domain.
        await message.reply(TARGET_NOT_A_USER_TEXT)
        return

    error_message: Optional[str] = None
    proposal_id: Optional[int] = None
    timeout_seconds = DEFAULT_PROPOSAL_TIMEOUT_SECONDS

    async with get_session() as session:
        await UserService.get_or_create_user(session, proposer)
        await UserService.get_or_create_user(session, target)
        await GroupService.get_or_create_group(session, message.chat)

        timeout_seconds = _coerce_timeout(
            await ConfigService.get(session, PROPOSAL_TIMEOUT_CONFIG_KEY)
        )

        try:
            proposal = await MarriageProposalService.create_proposal(
                session,
                proposer_id=proposer.id,
                target_id=target.id,
                group_id=message.chat.id,
            )
        except ProposalError as error:
            await session.rollback()
            error_message = _user_message_for(error)
            proposal = None
        except Exception as error:
            await session.rollback()
            logger.warning(f"Unexpected failure creating a marriage proposal: {error}")
            error_message = GENERIC_FAILURE_TEXT
            proposal = None

        if proposal is not None:
            proposal_id = int(proposal.id)

    # The transaction is closed: no row lock is held across the send below.
    if error_message is not None:
        await message.reply(error_message)
        return
    if proposal_id is None:  # pragma: no cover - defensive
        await message.reply(GENERIC_FAILURE_TEXT)
        return

    text = _proposal_text(proposer, target, timeout_seconds)
    keyboard = get_marriage_proposal_keyboard(proposal_id)

    try:
        sent = await message.reply(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as error:
        logger.warning(
            f"Failed to send marriage proposal {proposal_id} to chat "
            f"{message.chat.id}: {error}"
        )
        sent = None

    sent_message_id = getattr(sent, "message_id", None) if sent is not None else None
    if not sent_message_id:
        await _discard_undelivered_proposal(proposal_id)
        await _safe_reply(message, PROPOSAL_SEND_FAILED_TEXT)
        return

    await _persist_message_id(proposal_id, int(sent_message_id))

    log_event(
        Events.MARRIAGE_PROPOSAL_EVENT,
        {
            "action": "telegram_announced",
            "proposal_id": proposal_id,
            "proposer_id": proposer.id,
            "target_id": target.id,
            "group_id": message.chat.id,
            "message_id": int(sent_message_id),
        },
    )
