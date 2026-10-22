"""Telegram entry point for the marriage PROPOSAL flow (Phases 7.1 + 7.2).

Phase 7.1 - CREATION: the wake phrase is typed in a group as a REPLY to the
intended partner's message; the domain creates the proposal and the bot
posts the «قبول / رد» card.

Phase 7.2 - RESOLUTION: the two buttons on that card. Both call straight
into the domain (``MarriageService.accept_proposal`` /
``MarriageProposalService.reject_proposal``) and render whatever the
domain returns. Nothing about rings, marriage state, cooldowns, Love or
the invalidation of rival proposals is decided here.

Phase 7.3 - DIVORCE: «طلاق سیبی» as a REPLY to the spouse's message only
*opens a confirmation card*; it never divorces anybody. The divorce itself
happens on «💔 بله، طلاق», through ``MarriageService.divorce``, which also
owns the Love reset, the proposal/flower invalidation and the 24-hour
remarriage cooldown.

Phase 7.6 - FLOWER: «گل سیبی» as a REPLY to the spouse's message creates a
pending FlowerGift and posts the «💐 قبول گل» / «❌ رد گل» card; the two
buttons call ``LoveService.accept_flower`` / ``reject_flower``, which own
the Love change, the clamping, the timestamps and the refund on rejection.
The handler never computes Love itself and never rolls back a resolution
because a Telegram edit failed.

Deliberately NOT wired up here (later phases): daily Love decay,
MaintenanceService, marriage history and profile/shop screens.

Design rules this module follows:
- Identity is NEVER taken from the text or from callback data. The
  proposer is ``message.from_user`` and the target is
  ``message.reply_to_message.from_user`` - both Telegram-authenticated;
  the callback actor is always ``callback.from_user.id``. Callback data
  only ever names a proposal, never a person, so a forged or replayed
  button cannot act as anybody else.
- Business rules are NOT re-implemented here. The handler gathers the two
  identities plus the chat id and lets the domain decide; domain errors
  are mapped to short Persian sentences.
- The database transaction closes BEFORE any Telegram send, so a row lock
  is never held across network I/O.
- The handler adds no locking of its own: concurrency is the domain's job.
"""

from datetime import datetime, timezone
from typing import List, Optional

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.core.text import plain_display_name, safe_name
from sib.database.connection import get_session
from sib.database.models import FlowerGift
from sib.keyboards.marriage import (
    DIVORCE_PANEL_TYPE,
    LOVE_FLOWER_ACCEPT,
    LOVE_FLOWER_REJECT,
    MARRIAGE_ACCEPT,
    MARRIAGE_DIVORCE_CANCEL,
    MARRIAGE_DIVORCE_CONFIRM,
    MARRIAGE_REJECT,
    get_divorce_confirmation_keyboard,
    get_flower_gift_keyboard,
    get_marriage_proposal_keyboard,
    parse_love_flower_callback,
    parse_marriage_divorce_callback,
    parse_marriage_proposal_callback,
)
from sib.services.config_store import ConfigService
from sib.services.group import GroupService
from sib.services.love import (
    FlowerDailyLimitError,
    FlowerGiftAlreadyResolvedError,
    FlowerGiftMarriageInactiveError,
    FlowerGiftNotFoundError,
    FlowerLoveGainNotConfiguredError,
    FlowerRequiredError,
    LoveService,
    NotFlowerGiftReceiverError,
    NotMarriedError,
    NotSpouseError,
    SelfFlowerGiftError,
)
from sib.services.marriage import (
    MarriageAcceptanceRaceLostError,
    MarriageCooldownActiveError,
    MarriageDivorceRaceLostError,
    MarriageNotFoundError,
    MarriageNotActiveError,
    MarriageParticipantAlreadyMarriedError,
    MarriageService,
    NotMarriageSpouseError,
    ProposerAlreadyMarriedOnAcceptError,
    ProposalNotAcceptableError,
    TargetAlreadyMarriedOnAcceptError,
    WeddingRingMissingAtAcceptanceError,
)
from sib.services.marriage_proposal import (
    DEFAULT_PROPOSAL_TIMEOUT_SECONDS,
    MarriageProposalService,
    NotProposalTargetError,
    ProposalError,
    ProposalExpiredError,
    ProposalInvalidReason,
    ProposalNotFoundError,
    ProposalNotPendingError,
)
from sib.services.marriage_art import send_scene_answer, send_scene_reply
from sib.services.panel import PanelOwnershipService
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
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

#: Divorce wake phrase, anchored the same way so an ordinary sentence that
#: merely contains «طلاق» never hits this handler:
#:   «طلاق سیبی» (with/without a trailing question mark and with Arabic
#:   Yeh variants of «سیبی»). It only ever OPENS a confirmation card.
DIVORCE_TRIGGER_PATTERN = (
    r"^\s*طلاق\s+س" rf"{_YEH}ب{_YEH}" r"\s*[؟?]?\s*$"
)

#: Flower wake phrase, anchored the same way so an ordinary sentence that
#: merely contains «گل» never hits this handler:
#:   «گل سیبی» (with/without a trailing question mark and with Arabic Yeh
#:   variants of «سیبی»). Must be a REPLY to the spouse's message.
FLOWER_TRIGGER_PATTERN = (
    r"^\s*گل\s+س" rf"{_YEH}ب{_YEH}" r"\s*[؟?]?\s*$"
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

INVALID_BUTTON_TEXT = "💍 این دکمه معتبر نیست 🍏"

# --- Resolution (7.2) -------------------------------------------------------

# Product copy for the ring being sold between proposal and acceptance.
RING_SOLD_TEXT = (
    "💍 حلقه غیبش زد! 🍏\n\n"
    "طرف حلقه رو فروخت قبل از اینکه تو بگی بله؛ این پیشنهاد باطل شد. 😄"
)

RACE_LOST_TEXT = (
    "💍 یه لحظه دیر رسیدی 🍏\n\n"
    "این پیشنهاد همین‌جا و همین الان از طرف یه عمل دیگه بسته شد."
)

EXPIRED_TEXT = "💍 مهلت این پیشنهاد ازدواج به پایان رسیده 🍏"

NOT_ACCEPTABLE_TEXT = "💍 این پیشنهاد ازدواج دیگه قابل قبول نیست 🍏"

ALREADY_RESOLVED_TEXT = "💍 این پیشنهاد ازدواج قبلاً بسته شده 🍏"

TARGET_MARRIED_TEXT = "💍 شما الان متأهل هستید و نمی‌تونید این پیشنهاد رو قبول کنید 🍏"

PROPOSER_MARRIED_TEXT = "💍 خواستگار تو این فاصله متأهل شده 🍏"

SOMEONE_MARRIED_TEXT = "💍 یکی از طرفین الان متأهله 🍏"

NOT_YOUR_PROPOSAL_TEXT = "💍 فقط مخاطب این پیشنهاد ازدواج می‌تونه به اون پاسخ بده 🍏"

NOT_FOUND_TEXT = "💍 این پیشنهاد ازدواج پیدا نشد 🍏"

ACCEPT_ANSWER = "ازدواج ثبت شد! 🎉💍"

REJECT_ANSWER = "پیشنهاد رد شد."

# --- Divorce (7.3) ----------------------------------------------------------

DIVORCE_REPLY_REQUIRED_TEXT = (
    "💔 برای طلاق باید روی پیام همسرت ریپلای بزنی و بعد «طلاق سیبی» رو بنویسی."
)

DIVORCE_PRIVATE_CHAT_TEXT = "💔 طلاق سیبی فقط داخل گروه‌های سیب انجام می‌شه. 🍏"

NOT_MARRIED_TEXT = "💔 شما در حال حاضر متأهل نیستید که بخواید طلاق بگیرید 🍏"

NOT_YOUR_SPOUSE_TEXT = "💔 این کاربر همسر شما نیست 🍏"

DIVORCE_TARGET_NOT_A_USER_TEXT = (
    "💔 پیامی که ریپلای کردی از یک کاربر واقعی نیست؛ روی پیام همسرت ریپلای بزن."
)

DIVORCE_ANSWER = "طلاق ثبت شد."

DIVORCE_CANCEL_ANSWER = "از طلاق منصرف شدی 🍏"

DIVORCE_CANCEL_CARD = "💔 **طلاق لغو شد** 🍏\n\nازدواجت سر جاشه."

# A divorce card is stale once the marriage behind it is gone or already
# over - the buttons must not survive a settled marriage.
DIVORCE_GONE_TEXT = "💔 این ازدواج دیگه وجود نداره 🍏"

DIVORCE_ALREADY_ENDED_TEXT = "💔 این ازدواج قبلاً به پایان رسیده 🍏"

DIVORCE_RACE_LOST_TEXT = "💔 همسرت زودتر از تو طلاق رو ثبت کرد 🍏"

DIVORCE_NOT_YOURS_TEXT = "💔 شما همسر این ازدواج نیستید 🍏"

DIVORCE_CANCEL_FORBIDDEN_TEXT = "💔 فقط کسی که این درخواست رو باز کرده می‌تونه لغوش کنه 🍏"

# --- Profile (7.4) ----------------------------------------------------------

#: Cells in the profile Love gauge. The project had no progress-bar helper
#: before this, so the gauge follows the shape used in the profile spec:
#: ten cells, filled + empty, followed by the percentage.
LOVE_BAR_CELLS = 10
LOVE_FILLED_CELL = "█"
LOVE_EMPTY_CELL = "░"

# --- Flower (7.6) -----------------------------------------------------------

FLOWER_REPLY_REQUIRED_TEXT = (
    "💐 برای هدیه دادن گل باید روی پیام همسرت ریپلای بزنی و بعد "
    "«گل سیبی» رو بنویسی."
)

FLOWER_PRIVATE_CHAT_TEXT = "💐 هدیه دادن گل فقط داخل گروه‌های سیب معتبره. 🍏"

FLOWER_TARGET_NOT_A_USER_TEXT = (
    "💐 پیامی که ریپلای کردی از یک کاربر واقعی نیست؛ "
    "روی پیام خودِ همسرت ریپلای بزن."
)

FLOWER_SELF_TEXT = "💐 نمی‌تونی به خودت گل هدیه بدی! روی پیام همسرت ریپلای بزن. 🍏"

FLOWER_TARGET_IS_BOT_TEXT = "💐 گل فقط برای همسر سیبیه؛ ربات‌ها که عشق سیبی ندارن! 🍏"

FLOWER_NOT_MARRIED_TEXT = "💐 فقط همسر سیبی می‌تونه گل هدیه بگیره؛ تو فعلاً ازدواج نکردی. 🍏"

FLOWER_NOT_SPOUSE_TEXT = "💐 گل فقط برای همسر سیبی خودته؛ این سیبی همسرت نیست! 🍏"

FLOWER_NO_FLOWER_TEXT = (
    "💐 گلی تو موجودی‌ت نیست! از فروشگاه «💐 گل» بخر و دوباره امتحان کن. 🍏"
)

FLOWER_DAILY_LIMIT_TEXT = (
    "💐 امروز قبلاً به همسرت گل هدیه دادی؛ فردا دوباره امتحان کن! 🍏"
)

FLOWER_SEND_FAILED_TEXT = (
    "💐 نتونستم پیام گل رو تو گروه بفرستم؛ گل هدیه نشد و بهت برگشت. "
    "یه لحظه‌ی دیگه دوباره امتحان کن."
)

FLOWER_GENERIC_FAILURE_TEXT = "💐 گل هدیه نشد. یه لحظه‌ی دیگه دوباره امتحان کن. 🍏"

FLOWER_INVALID_BUTTON_TEXT = "💐 این دکمه معتبر نیست 🍏"

FLOWER_NOT_YOUR_GIFT_TEXT = (
    "💐 فقط کسی که گل براش فرستاده شده می‌تونه اونو قبول یا رد کنه 🍏"
)

FLOWER_ALREADY_RESOLVED_TEXT = "💐 این گل قبلاً قبول یا رد شده 🍏"

FLOWER_MARRIAGE_INACTIVE_TEXT = "💐 این گل دیگه اعتباری نداره؛ ازدواج به پایان رسیده 🍏"

FLOWER_GONE_TEXT = "💐 این گل دیگه وجود نداره 🍏"

FLOWER_ACCEPT_ANSWER = "💐 گل قبول شد!"
FLOWER_REJECT_ANSWER = "💐 گل رد شد."


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


def _cooldown_text(
    error: MarriageCooldownActiveError, *, tail: str = "دوباره پیشنهاد ازدواج بدی"
) -> str:
    wait = _format_duration(error.remaining_seconds)
    return (
        "💍 هنوز زوده 🍏\n\n"
        f"بعد از طلاق باید {wait} صبر کنی تا بتونی {tail}."
    )


def _accept_cooldown_text(error: MarriageCooldownActiveError) -> str:
    """Cooldown copy for the RESOLUTION path.

    The actor is the target, so a proposer-side cooldown must be described
    in the third person instead of telling the presser to wait.
    """
    wait = _format_duration(error.remaining_seconds)
    if error.role == "proposer":
        return (
            "💍 هنوز زوده 🍏\n\n"
            f"خواستگار تا {wait} دیگه نمی‌تونه ازدواج کنه؛ "
            "این پیشنهاد فعلاً قابل قبول نیست."
        )
    return _cooldown_text(error, tail="دوباره ازدواج کنی")


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


def _resolve_error_message(error: Exception) -> str:
    """Persian copy for an ACCEPT/REJECT failure.

    Several acceptance errors carry a technical English ``.message``
    (``"Proposer 42 already has an active marriage."``), so the resolution
    path cannot fall back to ``.message`` the way creation can - every
    branch is mapped explicitly and the fallback stays generic.

    Order matters: the subclasses are tested before their bases.
    """
    if isinstance(error, WeddingRingMissingAtAcceptanceError):
        return RING_SOLD_TEXT
    if isinstance(error, MarriageAcceptanceRaceLostError):
        return RACE_LOST_TEXT
    if isinstance(error, ProposalExpiredError):
        return EXPIRED_TEXT
    if isinstance(error, ProposalNotAcceptableError):
        return NOT_ACCEPTABLE_TEXT
    if isinstance(error, ProposalNotPendingError):
        return ALREADY_RESOLVED_TEXT
    if isinstance(error, MarriageCooldownActiveError):
        return _accept_cooldown_text(error)
    if isinstance(error, TargetAlreadyMarriedOnAcceptError):
        return TARGET_MARRIED_TEXT
    if isinstance(error, ProposerAlreadyMarriedOnAcceptError):
        return PROPOSER_MARRIED_TEXT
    if isinstance(error, MarriageParticipantAlreadyMarriedError):
        return SOMEONE_MARRIED_TEXT
    if isinstance(error, NotProposalTargetError):
        return NOT_YOUR_PROPOSAL_TEXT
    if isinstance(error, ProposalNotFoundError):
        return NOT_FOUND_TEXT
    return _user_message_for(error)


def _should_clear_buttons(error: Exception) -> bool:
    """True when the buttons are stale and should be stripped from the card.

    Only a proposal that is definitively gone (or already settled) makes the
    card stale. A live proposal keeps its buttons: an unauthorized presser
    must not be able to deface somebody else's pending card, and a refusal
    caused by a cooldown or a missing ring may well be retried.
    """
    return isinstance(
        error,
        (
            ProposalNotFoundError,
            ProposalExpiredError,
            ProposalNotAcceptableError,
            ProposalNotPendingError,
        ),
    )


def _remaining_seconds(moment: datetime) -> int:
    """Seconds from now until ``moment`` (never negative).

    SQLite hands back naive datetimes and MariaDB aware ones, so the
    comparison is normalized instead of assuming either. This only RENDERS
    a value the domain already computed - the cooldown itself belongs to
    ``MarriageService``.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int((moment - datetime.now(timezone.utc)).total_seconds()))


def _divorce_error_message(error: Exception) -> str:
    """Persian copy for a failed divorce confirmation.

    Mapped explicitly rather than falling back to ``error.message``: the
    divorce errors carry technical English text (``"Marriage 7 is not
    ACTIVE (status=DIVORCED)."``) that must never reach a chat.
    """
    if isinstance(error, MarriageDivorceRaceLostError):
        return DIVORCE_RACE_LOST_TEXT
    if isinstance(error, MarriageNotFoundError):
        return DIVORCE_GONE_TEXT
    if isinstance(error, MarriageNotActiveError):
        return DIVORCE_ALREADY_ENDED_TEXT
    if isinstance(error, NotMarriageSpouseError):
        return DIVORCE_NOT_YOURS_TEXT
    return GENERIC_FAILURE_TEXT


def _should_clear_divorce_buttons(error: Exception) -> bool:
    """True once the marriage behind the card is gone or already over.

    ``NotMarriageSpouseError`` deliberately does NOT clear: a stranger
    pressing the card must not be able to strip the buttons the two
    spouses still need.
    """
    return isinstance(error, (MarriageNotFoundError, MarriageNotActiveError))


def _proposal_text(proposer, target, timeout_seconds: int) -> str:
    """The group card announcing the proposal (Markdown-safe names)."""
    who = safe_name(getattr(proposer, "first_name", None), "یک سیبی")
    whom = safe_name(getattr(target, "first_name", None), "یک سیبی")
    expiry = _format_duration(timeout_seconds)
    return (
        "💍 **پیشنهاد ازدواج** 🍏\n\n"
        f"**{who}** به **{whom}** پیشنهاد ازدواج داد!\n"
        f"**{whom}** جان، «قبول» یا «رد» رو بزن؛ فقط **{expiry}** وقت داری."
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
# Resolution helpers (Phase 7.2)
# ---------------------------------------------------------------------------


async def _clear_proposal_buttons(callback: CallbackQuery, bot: Optional[Bot]) -> None:
    """Strip the «قبول / رد» pair from a stale card, leaving its text alone.

    The text is deliberately NOT rewritten: an already-ACCEPTED proposal
    must keep showing «عشق برنده شد», so only the dead buttons go.
    """
    if bot is None or callback.message is None:
        return
    chat = getattr(callback.message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(callback.message, "message_id", None)
    if not chat_id or not message_id:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception as error:  # pragma: no cover - presentation must not break
        logger.warning(f"Could not clear stale proposal buttons: {error}")


async def _notify_group(
    callback: CallbackQuery, text: str, scene: Optional[str] = None
) -> None:
    """One short follow-up in the group so nobody misses the outcome.

    ``scene`` optionally decorates the follow-up with committed scene art.
    Presentation only: the text is posted with or without the art, and a
    failed upload degrades to the plain text follow-up.
    """
    if callback.message is None:
        return
    try:
        await send_scene_answer(callback.message, scene, text)
    except Exception as error:  # pragma: no cover - presentation must not break
        logger.warning(f"Could not post the marriage outcome to the group: {error}")


def _accept_card(snapshot: dict) -> str:
    lines = [
        "💍 **عشق برنده شد** 🎉",
        "",
        f"**{snapshot['proposer_name']}** و **{snapshot['accepter_name']}** "
        "از امروز همسرن! 🍏",
        f"❤️ عشق سیبی: **{fa_digits(snapshot['love_score'])}**",
    ]
    if snapshot["invalidated_count"]:
        lines.append(
            f"**{fa_digits(snapshot['invalidated_count'])}** پیشنهاد دیگه باطل شد."
        )
    return "\n".join(lines)


def _reject_card(snapshot: dict) -> str:
    return (
        "💍 **پیشنهاد رد شد** 🍏\n\n"
        f"**{snapshot['target_name']}** پیشنهاد **{snapshot['proposer_name']}** "
        "رو رد کرد؛ حلقه سر جاش موند."
    )


async def _resolve_proposal_callback(
    callback: CallbackQuery, bot: Optional[Bot], action: str
) -> None:
    """Shared body of the «قبول» / «رد» handlers.

    A thin Telegram adapter: parse -> call the domain -> render. No
    business rule is decided here and no lock is taken by the handler.
    """
    parsed = parse_marriage_proposal_callback(callback.data)
    if parsed is None or parsed[0] != action:
        await safe_answer_callback(callback, INVALID_BUTTON_TEXT, show_alert=True)
        return

    parsed_action, proposal_id = parsed
    actor = callback.from_user
    if actor is None:
        return

    from sib.services.player_xp import PlayerXPService

    error: Optional[Exception] = None
    outcome = None
    snapshot: Optional[dict] = None

    async with get_session() as session:
        PlayerXPService.set_notification_chat(
            session,
            getattr(getattr(callback.message, "chat", None), "id", None),
        )
        try:
            if parsed_action == MARRIAGE_ACCEPT:
                outcome = await MarriageService.accept_proposal(
                    session, proposal_id, actor.id
                )
            else:
                outcome = await MarriageProposalService.reject_proposal(
                    session, proposal_id, actor.id
                )
        except ProposalError as domain_error:
            # Deliberately NO rollback: the domain owns atomicity (all
            # acceptance mutations live inside a savepoint, so a refusal
            # leaves nothing partial behind) and some refusals WRITE ON
            # PURPOSE - an overdue proposal is transitioned to EXPIRED and
            # *then* raises ProposalExpiredError. Rolling back here would
            # silently undo that transition and leave the row PENDING
            # forever. ``get_session()`` commits on normal exit.
            error = domain_error
        except Exception as unexpected:
            # Unknown failure: nothing is known to be safe to keep.
            await session.rollback()
            logger.warning(
                f"Unexpected failure resolving marriage proposal "
                f"{proposal_id}: {unexpected}"
            )
            error = unexpected

        if error is None and outcome is not None:
            if parsed_action == MARRIAGE_ACCEPT:
                snapshot = {
                    "proposal_id": int(outcome.proposal_id),
                    "marriage_id": int(outcome.marriage_id),
                    "proposer_name": safe_name(outcome.proposer.display_name),
                    "accepter_name": safe_name(outcome.accepter.display_name),
                    "love_score": int(outcome.love_score),
                    "invalidated_count": int(outcome.invalidated_count),
                }
            else:
                snapshot = {
                    "proposal_id": int(outcome.id),
                    "proposer_name": safe_name(outcome.proposer.display_name),
                    "target_name": safe_name(outcome.target.display_name),
                }

    # The transaction is closed: no row lock is held across the renders below.
    if error is not None:
        await safe_answer_callback(
            callback, _resolve_error_message(error), show_alert=True
        )
        if _should_clear_buttons(error):
            await _clear_proposal_buttons(callback, bot)
        return

    if snapshot is None:  # pragma: no cover - defensive
        await safe_answer_callback(callback, GENERIC_FAILURE_TEXT, show_alert=True)
        return

    if parsed_action == MARRIAGE_ACCEPT:
        await safe_answer_callback(callback, ACCEPT_ANSWER)
        await safe_edit_callback_message(
            callback, bot, _accept_card(snapshot), reply_markup=None
        )
        await _notify_group(
            callback,
            f"🎉 **{snapshot['proposer_name']}** و **{snapshot['accepter_name']}** "
            "ازدواج کردن!",
            scene="wedding",
        )
        await PlayerXPService.deliver_pending_notifications(
            bot,
            fallback_chat_id=getattr(getattr(callback.message, "chat", None), "id", None),
        )
        log_event(
            Events.MARRIAGE_EVENT,
            {
                "action": "telegram_accepted",
                "proposal_id": snapshot["proposal_id"],
                "marriage_id": snapshot["marriage_id"],
                "actor_id": actor.id,
            },
        )
        return

    await safe_answer_callback(callback, REJECT_ANSWER)
    await safe_edit_callback_message(
        callback, bot, _reject_card(snapshot), reply_markup=None
    )
    await _notify_group(
        callback,
        f"🔔 **{snapshot['proposer_name']}**، **{snapshot['target_name']}** "
        "پیشنهادت رو رد کرد.",
    )
    log_event(
        Events.MARRIAGE_PROPOSAL_EVENT,
        {
            "action": "telegram_rejected",
            "proposal_id": snapshot["proposal_id"],
            "actor_id": actor.id,
        },
    )


@router.callback_query(F.data.startswith(f"{MARRIAGE_ACCEPT}:"))
async def handle_marriage_accept(callback: CallbackQuery, bot: Bot) -> None:
    """«✅ قبول» - accept a pending proposal (target only, by domain rule).

    Marriage creation, ring consumption, Love initialisation and the
    invalidation of rival proposals are all ``MarriageService``'s job.
    """
    await _resolve_proposal_callback(callback, bot, MARRIAGE_ACCEPT)


@router.callback_query(F.data.startswith(f"{MARRIAGE_REJECT}:"))
async def handle_marriage_reject(callback: CallbackQuery, bot: Bot) -> None:
    """«❌ رد» - reject a pending proposal (target only, by domain rule).

    The Wedding Ring is never touched: ``reject_proposal`` only flips the
    proposal to its terminal REJECTED state.
    """
    await _resolve_proposal_callback(callback, bot, MARRIAGE_REJECT)


# ---------------------------------------------------------------------------
# Profile block (Phase 7.4)
# ---------------------------------------------------------------------------


def love_progress_bar(
    percent: int, *, cells: int = LOVE_BAR_CELLS
) -> str:
    """The compact Love gauge, e.g. ``██████░░░░`` for 60%.

    Pure presentation: it renders a percentage the domain already owns.
    """
    bounded = min(100, max(0, int(percent)))
    filled = int(round(bounded * cells / 100))
    filled = min(cells, max(0, filled))
    return LOVE_FILLED_CELL * filled + LOVE_EMPTY_CELL * (cells - filled)


def _love_percentage(score: object, minimum: int, maximum: int) -> Optional[int]:
    """Normalize the domain Love score to 0..100, or ``None`` if unusable.

    A stale or inconsistent row (``None``, a non-numeric value, or an
    inverted range) yields ``None`` so the profile simply omits the gauge
    instead of crashing or printing a nonsense number.
    """
    try:
        value = int(score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if maximum <= minimum:
        return None
    clamped = min(maximum, max(minimum, value))
    return int(round((clamped - minimum) * 100 / (maximum - minimum)))


def spouse_display_name(user) -> str:
    """The spouse's PLAIN first name for the profile - never a handle or link.

    Privacy rule: a spouse's ``@username``, Telegram ID or ``tg://`` link must
    never reach another player's screen (it would let anyone DM them). Only
    the sanitized first name is shown, as plain text: no ``@``, no HTML, no
    link. A user without a usable first name gets the neutral fallback - the
    username is deliberately NOT used, not even as plain text.
    """
    return plain_display_name(getattr(user, "first_name", None), "یک سیبی")


def _spouse_of(marriage, user_id: int):
    spouse_id = marriage.spouse_of(int(user_id))
    if spouse_id is None:
        return None
    if int(getattr(marriage, "user1_id", 0) or 0) == spouse_id:
        return marriage.user1
    if int(getattr(marriage, "user2_id", 0) or 0) == spouse_id:
        return marriage.user2
    return None


async def profile_marriage_lines(
    session: AsyncSession, user_id: int
) -> List[str]:
    """The marriage block for the EXISTING profile screen.

    Returns ``[]`` for a user with no ACTIVE marriage, so the profile keeps
    its current shape for single users and never mentions history, divorced
    spouses or the remarriage cooldown.

    Call this from inside the profile's own ``get_session()`` block: it adds
    a single marriage query (plus two cheap config lookups) and reuses the
    eagerly-loaded spouse row instead of issuing its own.

    The Love score is READ from the shared ``Marriage`` row - the same field
    ``LoveService.get_love_score`` returns - never recomputed here.
    """
    try:
        marriage = await MarriageService.get_active_marriage_for_user(
            session, int(user_id)
        )
    except Exception as error:
        logger.warning(f"Could not load the marriage for profile {user_id}: {error}")
        return []

    if marriage is None:
        return []

    spouse = _spouse_of(marriage, int(user_id))
    if spouse is None:
        # Stale/inconsistent record: omit the block rather than guess.
        return []

    lines = [f"💍 همسر: {spouse_display_name(spouse)}"]

    minimum = await ConfigService.get(session, "love_min")
    maximum = await ConfigService.get(session, "love_max")
    percent = _love_percentage(
        getattr(marriage, "love_score", None),
        _as_configured_int(minimum, "love_min"),
        _as_configured_int(maximum, "love_max"),
    )
    if percent is not None:
        lines.append(f"❤️ عشق سیبی: {love_progress_bar(percent)} {percent}%")
    return lines


def _as_configured_int(value: object, key: str) -> int:
    """Configured Love bound, falling back to the shipped default."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        fallback = ConfigService.default(key)
        try:
            return int(fallback)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0 if key == "love_min" else 100


# ---------------------------------------------------------------------------
# Divorce (Phase 7.3)
# ---------------------------------------------------------------------------


def _divorce_card(snapshot: dict) -> str:
    """The resolved confirmation card."""
    lines = [
        "💔 **طلاق ثبت شد**",
        "",
        f"**{snapshot['actor_name']}** و **{snapshot['spouse_name']}** "
        "دیگه همسر نیستن. 🍏",
        f"❤️ عشق سیبی: **{fa_digits(snapshot['love_score'])}**",
    ]
    if snapshot["cooldown_until"] is not None:
        lines.append(
            f"⏳ **{snapshot['cooldown_text']}** دیگه می‌تونی دوباره ازدواج کنی."
        )
    return "\n".join(lines)


async def _claim_divorce_card(chat_id: int, message_id: int, user_id: int) -> None:
    """Record who opened the confirmation card (best effort).

    «❌ انصراف» is initiator-only, and that fact is resolved from THIS
    server-side registry - never from the callback payload. The panel type
    is deliberately not a personal one, so the panel guard still lets the
    other spouse press «💔 بله، طلاق».
    """
    try:
        async with get_session() as session:
            await PanelOwnershipService.claim(
                session,
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                panel_type=DIVORCE_PANEL_TYPE,
            )
    except Exception as error:  # pragma: no cover - registration must not break
        logger.warning(f"Could not claim the divorce confirmation card: {error}")


async def _divorce_card_initiator(
    callback: CallbackQuery,
) -> Optional[int]:
    """The user who opened this confirmation card, or ``None`` if unknown.

    ``None`` means "cannot prove who initiated it", and callers MUST fail
    closed rather than fall back to the presser.
    """
    if callback.message is None:
        return None
    chat = getattr(callback.message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(callback.message, "message_id", None)
    if not chat_id or not message_id:
        return None
    try:
        async with get_session() as session:
            return await PanelOwnershipService.get_owner(session, chat_id, message_id)
    except Exception as error:  # pragma: no cover - lookup must not break
        logger.warning(f"Could not resolve the divorce card initiator: {error}")
        return None


@router.message(F.text.regexp(DIVORCE_TRIGGER_PATTERN))
async def handle_marriage_divorce_request(message: Message) -> None:
    """«طلاق سیبی» as a REPLY to the spouse - opens a confirmation card.

    NOBODY IS DIVORCED HERE. This only resolves which marriage the reply is
    about and renders the «💔 بله، طلاق» / «❌ انصراف» prompt; the divorce
    itself happens in :func:`handle_marriage_divorce_confirm`.
    """
    if getattr(message.chat, "type", None) not in ("group", "supergroup"):
        await message.reply(DIVORCE_PRIVATE_CHAT_TEXT)
        return

    actor = message.from_user
    if actor is None:
        return

    reply = message.reply_to_message
    if reply is None:
        await message.reply(DIVORCE_REPLY_REQUIRED_TEXT)
        return

    target = getattr(reply, "from_user", None)
    if target is None:
        await message.reply(DIVORCE_TARGET_NOT_A_USER_TEXT)
        return

    if target.id == actor.id:
        await message.reply(NOT_YOUR_SPOUSE_TEXT)
        return

    # Which marriage is this reply about? That is a routing question, not a
    # business rule: the handler needs the id to build the buttons, while
    # every actual precondition (is it ACTIVE, is the actor a spouse, ...)
    # is re-checked by MarriageService.divorce on confirmation.
    async with get_session() as session:
        await UserService.get_or_create_user(session, actor)
        await UserService.get_or_create_user(session, target)
        marriage = await MarriageService.get_active_marriage_for_user(session, actor.id)

        if marriage is None:
            rejection = NOT_MARRIED_TEXT
        elif marriage.spouse_of(int(target.id)) is None:
            rejection = NOT_YOUR_SPOUSE_TEXT
        else:
            rejection = None
            snapshot = {
                "marriage_id": int(marriage.id),
                "spouse_id": int(marriage.spouse_of(int(actor.id))),
            }

    if rejection is not None:
        await message.reply(rejection)
        return

    text = (
        "💔 **درخواست طلاق**\n\n"
        f"**{safe_name(actor.first_name)}** می‌خواد از "
        f"**{safe_name(getattr(target, 'first_name', None))}** جدا بشه؛ "
        "عشق صفر می‌شه و ازدواج بعدی موقتاً قفل می‌شه.\n"
        "اگه مطمئنی «بله، طلاق» رو بزن، وگرنه «انصراف»."
    )
    keyboard = get_divorce_confirmation_keyboard(snapshot["marriage_id"])

    try:
        sent = await send_scene_reply(
            message,
            "divorce",
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as error:
        logger.warning(f"Failed to send the divorce confirmation card: {error}")
        await _safe_reply(message, GENERIC_FAILURE_TEXT)
        return

    sent_message_id = getattr(sent, "message_id", None) if sent is not None else None
    if not sent_message_id:
        await _safe_reply(message, GENERIC_FAILURE_TEXT)
        return

    await _claim_divorce_card(message.chat.id, int(sent_message_id), actor.id)

    log_event(
        Events.MARRIAGE_EVENT,
        {
            "action": "telegram_divorce_requested",
            "marriage_id": snapshot["marriage_id"],
            "actor_id": actor.id,
            "spouse_id": snapshot["spouse_id"],
            "group_id": message.chat.id,
            "message_id": int(sent_message_id),
        },
    )


@router.callback_query(F.data.startswith(f"{MARRIAGE_DIVORCE_CONFIRM}:"))
async def handle_marriage_divorce_confirm(callback: CallbackQuery, bot: Bot) -> None:
    """«💔 بله، طلاق» - end the marriage through ``MarriageService.divorce``.

    The domain owns everything: the ACTIVE check, the spouse check, the
    Love reset, proposal/flower invalidation, the remarriage cooldown and
    the CAS that decides who wins a concurrent divorce.
    """
    parsed = parse_marriage_divorce_callback(callback.data)
    if parsed is None or parsed[0] != MARRIAGE_DIVORCE_CONFIRM:
        await safe_answer_callback(callback, INVALID_BUTTON_TEXT, show_alert=True)
        return

    _action, marriage_id = parsed
    actor = callback.from_user
    if actor is None:
        return

    error: Optional[Exception] = None
    snapshot: Optional[dict] = None

    async with get_session() as session:
        try:
            result = await MarriageService.divorce(session, marriage_id, actor.id)
        except ProposalError as domain_error:
            # MarriageError extends ProposalError. As in the resolution
            # path, no rollback: the domain already wrapped its mutations
            # in a savepoint and a lost race writes nothing of its own.
            error = domain_error
        except Exception as unexpected:
            await session.rollback()
            logger.warning(
                f"Unexpected failure divorcing marriage {marriage_id}: {unexpected}"
            )
            error = unexpected

        if error is None:
            cooldown_until = result.cooldown_until
            snapshot = {
                "marriage_id": int(result.marriage_id),
                "actor_name": safe_name(result.actor.display_name),
                "spouse_name": safe_name(result.spouse.display_name),
                "spouse_id": int(result.spouse_id),
                "love_score": int(result.love_score),
                "cooldown_until": cooldown_until,
                "cooldown_text": (
                    _format_duration(_remaining_seconds(cooldown_until))
                    if cooldown_until is not None
                    else None
                ),
            }

    # The divorce is committed before any Telegram call below, so a failed
    # edit or notification can never roll it back.
    if error is not None:
        await safe_answer_callback(
            callback, _divorce_error_message(error), show_alert=True
        )
        if _should_clear_divorce_buttons(error):
            await _clear_proposal_buttons(callback, bot)
        return

    if snapshot is None:  # pragma: no cover - defensive
        await safe_answer_callback(callback, GENERIC_FAILURE_TEXT, show_alert=True)
        return

    await safe_answer_callback(callback, DIVORCE_ANSWER)
    await safe_edit_callback_message(
        callback, bot, _divorce_card(snapshot), reply_markup=None
    )
    await _notify_group(
        callback,
        f"💔 **{snapshot['actor_name']}** و **{snapshot['spouse_name']}** "
        "از هم جدا شدن.",
    )
    log_event(
        Events.MARRIAGE_EVENT,
        {
            "action": "telegram_divorced",
            "marriage_id": snapshot["marriage_id"],
            "actor_id": actor.id,
            "spouse_id": snapshot["spouse_id"],
        },
    )


@router.callback_query(F.data.startswith(f"{MARRIAGE_DIVORCE_CANCEL}:"))
async def handle_marriage_divorce_cancel(callback: CallbackQuery, bot: Bot) -> None:
    """«❌ انصراف» - dismiss the confirmation card. The marriage is untouched."""
    parsed = parse_marriage_divorce_callback(callback.data)
    if parsed is None or parsed[0] != MARRIAGE_DIVORCE_CANCEL:
        await safe_answer_callback(callback, INVALID_BUTTON_TEXT, show_alert=True)
        return

    actor = callback.from_user
    if actor is None:
        return

    # Initiator-only, resolved server-side. An unknown owner fails closed
    # instead of falling back to whoever pressed the button.
    initiator_id = await _divorce_card_initiator(callback)
    if initiator_id is None or initiator_id != actor.id:
        await safe_answer_callback(
            callback, DIVORCE_CANCEL_FORBIDDEN_TEXT, show_alert=True
        )
        return

    await safe_answer_callback(callback, DIVORCE_CANCEL_ANSWER)
    await safe_edit_callback_message(
        callback, bot, DIVORCE_CANCEL_CARD, reply_markup=None
    )
    log_event(
        Events.MARRIAGE_EVENT,
        {
            "action": "telegram_divorce_cancelled",
            "marriage_id": parsed[1],
            "actor_id": actor.id,
        },
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
        sent = await send_scene_reply(
            message,
            "proposal",
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
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


# ---------------------------------------------------------------------------
# Flower (Phase 7.6)
# ---------------------------------------------------------------------------
#
# A thin Telegram adapter over ``LoveService``. Every rule - marriage
# validity, spouse check, self-send, Flower ownership, the directional daily
# limit, the configured Love gain, the Love clamping and the refund on
# rejection/invalidation - lives in the domain. This module only gathers the
# two Telegram-authenticated identities, calls the service and renders what
# it returns.


def _flower_send_error_message(error: Exception) -> str:
    """Persian copy for a failed SEND.

    Mapped explicitly: the domain errors carry technical English text
    (``"User 42 does not own a flower item."``) that must never reach a chat.
    Order matters - the subclasses are tested before their bases.
    """
    if isinstance(error, SelfFlowerGiftError):
        return FLOWER_SELF_TEXT
    if isinstance(error, NotSpouseError):
        return FLOWER_NOT_SPOUSE_TEXT
    if isinstance(error, NotMarriedError):
        return FLOWER_NOT_MARRIED_TEXT
    if isinstance(error, FlowerRequiredError):
        return FLOWER_NO_FLOWER_TEXT
    if isinstance(error, FlowerDailyLimitError):
        return FLOWER_DAILY_LIMIT_TEXT
    if isinstance(error, FlowerLoveGainNotConfiguredError):
        # A misconfigured bot is never the player's problem to diagnose.
        return FLOWER_GENERIC_FAILURE_TEXT
    return FLOWER_GENERIC_FAILURE_TEXT


def _flower_resolve_error_message(error: Exception) -> str:
    """Persian copy for a failed ACCEPT / REJECT (explicitly mapped)."""
    if isinstance(error, NotFlowerGiftReceiverError):
        return FLOWER_NOT_YOUR_GIFT_TEXT
    if isinstance(error, FlowerGiftAlreadyResolvedError):
        return FLOWER_ALREADY_RESOLVED_TEXT
    if isinstance(error, FlowerGiftMarriageInactiveError):
        return FLOWER_MARRIAGE_INACTIVE_TEXT
    if isinstance(error, FlowerGiftNotFoundError):
        return FLOWER_GONE_TEXT
    return FLOWER_GENERIC_FAILURE_TEXT


def _should_clear_flower_buttons(error: Exception) -> bool:
    """True once the gift behind the card is definitively settled.

    ``NotFlowerGiftReceiverError`` deliberately does NOT clear: a stranger
    pressing the card must not be able to strip the buttons the recipient
    still needs. A marriage-inactive gift IS settled (the domain already
    invalidated and refunded it), so its buttons are dead.
    """
    return isinstance(
        error,
        (
            FlowerGiftNotFoundError,
            FlowerGiftAlreadyResolvedError,
            FlowerGiftMarriageInactiveError,
        ),
    )


def _flower_offer_card(snapshot: dict) -> str:
    """The group card announcing a pending Flower."""
    return (
        "💐 **یه گل برای همسر!** 🍏\n\n"
        f"**{snapshot['sender_name']}** به **{snapshot['receiver_name']}** گل داد!\n"
        f"**{snapshot['receiver_name']}** جان، «قبول گل» یا «رد گل» رو بزن."
    )


def _love_line(previous: int, new: int, percent: Optional[int]) -> str:
    """The Love row of a resolved Flower card (numbers from the domain)."""
    if percent is None:
        return f"❤️ عشق سیبی: **{fa_digits(previous)}** → **{fa_digits(new)}**"
    return (
        f"❤️ عشق سیبی: {love_progress_bar(percent)} **{fa_digits(new)}**\n"
        f"(**{fa_digits(previous)}** → **{fa_digits(new)}**)"
    )


def _flower_accept_card(snapshot: dict) -> str:
    return "\n".join(
        [
            "💐 **گل قبول شد!** 🎉",
            "",
            f"**{snapshot['receiver_name']}** گل **{snapshot['sender_name']}** رو قبول کرد.",
            _love_line(
                snapshot["previous_love"], snapshot["new_love"], snapshot["love_percent"]
            ),
        ]
    )


def _flower_reject_card(snapshot: dict) -> str:
    lines = [
        "💐 **گل رد شد** 🍏",
        "",
        f"**{snapshot['receiver_name']}** گل **{snapshot['sender_name']}** رو رد کرد.",
    ]
    if snapshot["refunded"]:
        lines.append(f"💐 گل به **{snapshot['sender_name']}** برگشت.")
    lines.append(
        _love_line(
            snapshot["previous_love"], snapshot["new_love"], snapshot["love_percent"]
        )
    )
    return "\n".join(lines)


async def _clear_flower_buttons(callback: CallbackQuery, bot: Optional[Bot]) -> None:
    """Strip the Flower pair from a settled card, leaving its text alone."""
    if bot is None or callback.message is None:
        return
    chat = getattr(callback.message, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(callback.message, "message_id", None)
    if not chat_id or not message_id:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception as error:  # pragma: no cover - presentation must not break
        logger.warning(f"Could not clear stale flower buttons: {error}")


async def _discard_undelivered_gift(gift_id: int) -> None:
    """Invalidate (and therefore REFUND) a Flower whose card never arrived.

    A PENDING gift with no message on screen is unusable: the receiver
    cannot see it. ``LoveService.invalidate_gift`` is the domain's existing
    terminal transition for exactly this, and it gives the Flower back.
    """
    try:
        async with get_session() as session:
            await LoveService.invalidate_gift(session, gift_id)
    except Exception as error:  # pragma: no cover - best-effort cleanup
        logger.warning(
            f"Could not roll back undelivered flower gift {gift_id}: {error}"
        )
        return

    log_event(
        Events.LOVE_EVENT,
        {"action": "telegram_send_failed", "gift_id": gift_id},
    )


async def _persist_gift_message_id(
    gift_id: int, chat_id: int, message_id: int
) -> None:
    """Attach the Telegram handle to the gift (best effort)."""
    try:
        async with get_session() as session:
            gift = await session.get(FlowerGift, gift_id)
            if gift is None:
                return
            gift.chat_id = int(chat_id)
            gift.message_id = int(message_id)
            await session.flush()
    except Exception as error:
        # The card is on screen and its buttons carry the gift id, so the
        # gift stays resolvable - only later edits lose the handle.
        logger.warning(
            f"Could not persist message id {message_id} on flower gift "
            f"{gift_id}: {error}"
        )


def _flower_snapshot(result, percent: Optional[int]) -> dict:
    """Render-safe view of a ``FlowerGiftResult`` (no ORM objects escape)."""
    sender = result.sender
    receiver = result.receiver

    def _name(user) -> str:
        return safe_name(getattr(user, "display_name", None), "یک سیبی")

    return {
        "gift_id": int(result.gift.id),
        "marriage_id": int(result.marriage_id),
        "sender_id": int(result.sender_id),
        "receiver_id": int(result.receiver_id),
        "sender_name": _name(sender),
        "receiver_name": _name(receiver),
        "previous_love": int(result.previous_love),
        "new_love": int(result.new_love),
        "love_delta": int(result.love_delta),
        "refunded": bool(result.refunded),
        "love_percent": percent,
    }


@router.message(F.text.regexp(FLOWER_TRIGGER_PATTERN))
async def handle_flower_send(message: Message) -> None:
    """Send a Flower: «گل سیبی» typed as a REPLY to the spouse's message.

    Every rule is the domain's. The handler only supplies the two
    Telegram-authenticated identities (``from_user`` and the replied-to
    user) plus the chat id, then renders the outcome.
    """
    if getattr(message.chat, "type", None) not in ("group", "supergroup"):
        await message.reply(FLOWER_PRIVATE_CHAT_TEXT)
        return

    sender = message.from_user
    if sender is None:
        return

    reply = message.reply_to_message
    if reply is None:
        await message.reply(FLOWER_REPLY_REQUIRED_TEXT)
        return

    target = getattr(reply, "from_user", None)
    if target is None:
        # Channel posts and anonymous admins have no user identity.
        await message.reply(FLOWER_TARGET_NOT_A_USER_TEXT)
        return

    if getattr(target, "is_bot", False):
        await message.reply(FLOWER_TARGET_IS_BOT_TEXT)
        return

    if target.id == sender.id:
        await message.reply(FLOWER_SELF_TEXT)
        return

    error_message: Optional[str] = None
    snapshot: Optional[dict] = None

    async with get_session() as session:
        await UserService.get_or_create_user(session, sender)
        await UserService.get_or_create_user(session, target)
        await GroupService.get_or_create_group(session, message.chat)

        try:
            result = await LoveService.send_flower(
                session,
                sender_user_id=sender.id,
                receiver_user_id=target.id,
                chat_id=message.chat.id,
            )
        except ProposalError as error:
            # Deliberately NO rollback: the domain owns atomicity (the
            # consume + insert live inside a savepoint) and some refusals
            # WRITE ON PURPOSE before raising. get_session() commits on
            # normal exit, so a refused send keeps nothing partial.
            error_message = _flower_send_error_message(error)
            result = None
        except Exception as error:
            await session.rollback()
            logger.warning(f"Unexpected failure sending a flower: {error}")
            error_message = FLOWER_GENERIC_FAILURE_TEXT
            result = None

        if result is not None:
            minimum = await ConfigService.get(session, "love_min")
            maximum = await ConfigService.get(session, "love_max")
            percent = _love_percentage(
                result.new_love,
                _as_configured_int(minimum, "love_min"),
                _as_configured_int(maximum, "love_max"),
            )
            snapshot = _flower_snapshot(result, percent)

    # The transaction is closed: no row lock is held across the send below.
    if error_message is not None:
        await message.reply(error_message)
        return
    if snapshot is None:  # pragma: no cover - defensive
        await message.reply(FLOWER_GENERIC_FAILURE_TEXT)
        return

    gift_id = snapshot["gift_id"]
    try:
        sent = await send_scene_reply(
            message,
            "flower_gift",
            _flower_offer_card(snapshot),
            reply_markup=get_flower_gift_keyboard(gift_id),
            parse_mode="Markdown",
        )
    except Exception as error:
        logger.warning(
            f"Failed to send flower gift {gift_id} to chat {message.chat.id}: {error}"
        )
        sent = None

    sent_message_id = getattr(sent, "message_id", None) if sent is not None else None
    if not sent_message_id:
        await _discard_undelivered_gift(gift_id)
        await _safe_reply(message, FLOWER_SEND_FAILED_TEXT)
        return

    await _persist_gift_message_id(gift_id, message.chat.id, int(sent_message_id))

    log_event(
        Events.LOVE_EVENT,
        {
            "action": "telegram_announced",
            "gift_id": gift_id,
            "sender_id": sender.id,
            "receiver_id": target.id,
            "group_id": message.chat.id,
            "message_id": int(sent_message_id),
        },
    )


async def _resolve_flower_callback(
    callback: CallbackQuery, bot: Optional[Bot], action: str
) -> None:
    """Shared body of the «💐 قبول گل» / «❌ رد گل» handlers.

    Parse -> call the domain -> render. No business rule is decided here and
    the handler takes no lock of its own.
    """
    parsed = parse_love_flower_callback(callback.data)
    if parsed is None or parsed[0] != action:
        await safe_answer_callback(callback, FLOWER_INVALID_BUTTON_TEXT, show_alert=True)
        return

    _parsed_action, gift_id = parsed
    actor = callback.from_user
    if actor is None:
        return

    from sib.services.player_xp import PlayerXPService

    error: Optional[Exception] = None
    outcome = None
    snapshot: Optional[dict] = None

    async with get_session() as session:
        PlayerXPService.set_notification_chat(
            session,
            getattr(getattr(callback.message, "chat", None), "id", None),
        )
        try:
            if action == LOVE_FLOWER_ACCEPT:
                outcome = await LoveService.accept_flower(session, gift_id, actor.id)
            else:
                outcome = await LoveService.reject_flower(session, gift_id, actor.id)
        except ProposalError as domain_error:
            # NO rollback: the domain owns atomicity (all resolution
            # mutations live inside a savepoint) and a refusal legitimately
            # WRITES - an inactive marriage invalidates and REFUNDS the
            # Flower before raising FlowerGiftMarriageInactiveError.
            error = domain_error
        except Exception as unexpected:
            await session.rollback()
            logger.warning(
                f"Unexpected failure resolving flower gift {gift_id}: {unexpected}"
            )
            error = unexpected

        if error is None and outcome is not None:
            minimum = await ConfigService.get(session, "love_min")
            maximum = await ConfigService.get(session, "love_max")
            percent = _love_percentage(
                outcome.new_love,
                _as_configured_int(minimum, "love_min"),
                _as_configured_int(maximum, "love_max"),
            )
            snapshot = _flower_snapshot(outcome, percent)

    # Committed. From here on a Telegram failure can never undo the domain.
    if error is not None:
        await safe_answer_callback(
            callback, _flower_resolve_error_message(error), show_alert=True
        )
        if _should_clear_flower_buttons(error):
            await _clear_flower_buttons(callback, bot)
        return

    if snapshot is None:  # pragma: no cover - defensive
        await safe_answer_callback(callback, FLOWER_GENERIC_FAILURE_TEXT, show_alert=True)
        return

    is_accept = action == LOVE_FLOWER_ACCEPT

    await safe_answer_callback(
        callback, FLOWER_ACCEPT_ANSWER if is_accept else FLOWER_REJECT_ANSWER
    )
    # Buttons are dropped with the rewrite: a resolved gift has no actions.
    await safe_edit_callback_message(
        callback,
        bot,
        _flower_accept_card(snapshot) if is_accept else _flower_reject_card(snapshot),
        reply_markup=None,
    )

    if is_accept:
        await _notify_group(
            callback,
            f"🔔 **{snapshot['sender_name']}**، **{snapshot['receiver_name']}** "
            "گلت رو قبول کرد! 💐",
        )
    else:
        await _notify_group(
            callback,
            f"🔔 **{snapshot['sender_name']}**، **{snapshot['receiver_name']}** "
            "گلت رو رد کرد؛ گل بهت برگشت.",
        )

    log_event(
        Events.LOVE_EVENT,
        {
            "action": "telegram_accepted" if is_accept else "telegram_rejected",
            "gift_id": snapshot["gift_id"],
            "marriage_id": snapshot["marriage_id"],
            "actor_id": actor.id,
            "previous_love": snapshot["previous_love"],
            "new_love": snapshot["new_love"],
        },
    )
    if is_accept:
        await PlayerXPService.deliver_pending_notifications(
            bot,
            fallback_chat_id=getattr(getattr(callback.message, "chat", None), "id", None),
        )



@router.callback_query(F.data.startswith(f"{LOVE_FLOWER_ACCEPT}:"))
async def handle_flower_accept(callback: CallbackQuery, bot: Bot) -> None:
    """«💐 قبول گل» — accept a pending Flower (receiver only, by domain rule).

    The Love increase, the clamping, the timestamps and the concurrency
    guard are all ``LoveService.accept_flower``'s job.
    """
    await _resolve_flower_callback(callback, bot, LOVE_FLOWER_ACCEPT)


@router.callback_query(F.data.startswith(f"{LOVE_FLOWER_REJECT}:"))
async def handle_flower_reject(callback: CallbackQuery, bot: Bot) -> None:
    """«❌ رد گل» — reject a pending Flower (receiver only, by domain rule).

    The Love penalty, the clamping and the Flower refund are all
    ``LoveService.reject_flower``'s job.
    """
    await _resolve_flower_callback(callback, bot, LOVE_FLOWER_REJECT)
