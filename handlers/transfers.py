"""Telegram UX for user-to-user transfers («انتقال سیب ...»).

Transfers happen in the GROUP chat where the reply is made: A replies to one
of B's messages with a transfer command, the bot asks A to confirm with two
inline buttons, and the confirmed transfer is delivered with a notification
for B in the same chat.

Flow
----
    A replies «انتقال سیب 100» to B's message        (pure parser gate)
      └─ cheap pre-checks (config + users, NO locks)
          └─ pending_transfer row (60s TTL) + confirmation message
              ├─ ✅ tr:confirm:<id>  → claim row → execute_transfer → deliver
              └─ ❌ tr:cancel:<id>   → claim row → cancelled

Safety
------
* The parser is the only gate: anything it rejects is silent (a group chat
  must never answer random chatter), as are non-replies and private chats.
* The confirmation message is the source of truth for the 60s TTL - the
  pending row lives in the database, so a bot restart never loses it.
* Double presses are impossible to pay twice: ``pending`` is claimed as
  ``processing`` inside ``SELECT ... FOR UPDATE`` in a short transaction
  BEFORE the money moves; every other state answers «قبلاً پردازش شده است.».
* The presser must be the row's sender; anyone else is answered without
  touching state (see the trade confirm/cancel precedent in
  ``services/panel.py``: shared messages authorize in the handler/service,
  not in the panel-guard middleware).
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from sib.core.bidi import fix_bidi
from sib.core.config import settings
from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import PendingTransfer, PendingTransferState, User
from sib.keyboards.common import danger_button, success_button
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.transfers.parser import parse_transfer_command
from sib.services.transfers.service import (
    compute_fee,
    execute_transfer,
    load_transfer_config,
    cleanup_expired_pending,
)
from sib.services.transfers.text import (
    ALREADY_PROCESSED_TEXT,
    CONFIRM_BUTTON_CANCEL,
    CONFIRM_BUTTON_CONFIRM,
    CANCELLED_TEXT,
    CANCELLED_TOAST,
    EXPIRED_TEXT,
    INVALID_REQUEST_TEXT,
    NOT_YOUR_BUTTON_TEXT,
    PENDING_TTL_SECONDS,
    SUCCESS_TOAST,
    display_name,
    render_confirmation,
    render_expired,
    render_failure,
    render_reason,
    render_receiver_notification,
    render_success,
)
from sib.services.user import UserService

router = Router(name="transfers")

TRANSFER_COMMAND_PATTERN = r"^\s*انتقال(\s|$)"
CONFIRM_PREFIX = "tr:confirm:"
CANCEL_PREFIX = "tr:cancel:"
PENDING_CLEANUP_INTERVAL_SECONDS = 60

# Reason codes produced by the cheap pre-checks (see :func:`_precheck`).
REASON_DISABLED = "disabled"
REASON_OUT_OF_BOUNDS = "amount_out_of_bounds"
REASON_SELF = "self"
REASON_USER_NOT_FOUND = "user_not_found"
REASON_BANNED = "banned"
REASON_BOT = "bot"


def _now() -> datetime:
    """Naive UTC "now" - the stored-timestamp convention (patchable in tests)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def confirmation_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    """✅ confirm / ❌ cancel row for one pending transfer."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                success_button(
                    text=CONFIRM_BUTTON_CONFIRM,
                    callback_data=f"{CONFIRM_PREFIX}{int(pending_id)}",
                ),
                danger_button(
                    text=CONFIRM_BUTTON_CANCEL,
                    callback_data=f"{CANCEL_PREFIX}{int(pending_id)}",
                ),
            ]
        ]
    )


def _callback_pending_id(data: Optional[str], prefix: str) -> Optional[int]:
    """``<prefix><id>`` -> id, or None when the payload is malformed."""
    if not data or not data.startswith(prefix):
        return None
    raw = data[len(prefix):]
    if not raw.isdigit():
        return None
    return int(raw)


def _is_group(message: Optional[Message]) -> bool:
    chat = getattr(message, "chat", None) if message is not None else None
    return getattr(chat, "type", None) in ("group", "supergroup")


async def _precheck(
    session,
    *,
    config,
    sender_tg,
    target_tg,
    amount: int,
) -> Optional[str]:
    """Cheap pre-flight checks (config + user rows, no locks, no balance).

    Order mirrors the service: disabled, bounds, self, bot, existence,
    banned, is_bot. Returns a reason code, or ``None`` when the transfer may
    proceed to confirmation. The balance is deliberately NOT checked here -
    it is only authoritative inside the locked execution.
    """
    if not config.enabled:
        return REASON_DISABLED

    if amount < config.min_amount or amount > config.max_amount:
        return REASON_OUT_OF_BOUNDS

    if sender_tg.id == target_tg.id:
        return REASON_SELF

    if int(sender_tg.id) == int(settings.BOT_USER_ID) or getattr(
        target_tg, "is_bot", False
    ):
        return REASON_BOT

    sender = await session.get(User, sender_tg.id)
    target = await session.get(User, target_tg.id)
    if sender is None or target is None:
        return REASON_USER_NOT_FOUND

    if sender.is_banned or target.is_banned:
        return REASON_BANNED

    if sender.is_bot or target.is_bot:
        return REASON_BOT

    return None


@router.message(F.text.regexp(TRANSFER_COMMAND_PATTERN))
async def handle_transfer_command(message: Message) -> None:
    """«انتقال <نوع سیب> <تعداد>» as a REPLY to another player's message."""
    # Transfers are a group-chat behaviour; elsewhere they are silent.
    if not _is_group(message):
        return

    sender = message.from_user
    if sender is None or getattr(sender, "is_bot", False):
        return

    # The parser is the ONLY gate: it rejects unknown types, zero/negative/
    # non-integer amounts and every trailing token. Silence on failure.
    command = parse_transfer_command(message.text or "")
    if command is None:
        return

    reply = message.reply_to_message
    target = getattr(reply, "from_user", None) if reply is not None else None
    if target is None:
        # Not a reply (or a channel/anonymous author): stay silent.
        return

    async with get_session() as session:
        # The sender is the acting user, so their own message is proof they
        # exist; the TARGET must already be a known player.
        await UserService.get_or_create_user(session, sender)
        config = await load_transfer_config(session)
        reason = await _precheck(
            session,
            config=config,
            sender_tg=sender,
            target_tg=target,
            amount=command.amount,
        )

    if reason is not None:
        await message.reply(
            render_reason(
                reason,
                min_amount=config.min_amount,
                max_amount=config.max_amount,
            )
        )
        return

    pending_id = await _create_pending(
        chat_id=message.chat.id,
        from_user_id=sender.id,
        to_user_id=target.id,
        item_type=command.item_type,
        amount=command.amount,
    )

    fee = compute_fee(command.amount, config.fee_percent)
    body = render_confirmation(
        from_display=display_name(sender.first_name, sender.username),
        to_display=display_name(target.first_name, target.username),
        item_type=command.item_type,
        amount=command.amount,
        fee=fee,
    )

    try:
        sent = await message.reply(
            fix_bidi(body), reply_markup=confirmation_keyboard(pending_id)
        )
    except Exception as error:  # pragma: no cover - Telegram send failure
        logger.error(f"transfer confirmation could not be sent ({pending_id}): {error}")
        await _set_state(pending_id, PendingTransferState.CANCELLED)
        return

    await _remember_message_id(pending_id, getattr(sent, "message_id", None))


async def _create_pending(
    *,
    chat_id: int,
    from_user_id: int,
    to_user_id: int,
    item_type: str,
    amount: int,
) -> int:
    """Insert the ``pending`` row (60 s TTL) and return its id."""
    async with get_session() as session:
        row = PendingTransfer(
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            item_type=item_type,
            amount=amount,
            chat_id=chat_id,
            state=PendingTransferState.PENDING,
            expires_at=_now() + timedelta(seconds=PENDING_TTL_SECONDS),
        )
        session.add(row)
        await session.flush()
        return int(row.id)


async def _remember_message_id(pending_id: int, message_id: Optional[int]) -> None:
    """Store the id of the confirmation message we just sent."""
    if message_id is None:  # pragma: no cover - Telegram always returns one
        return
    async with get_session() as session:
        row = await session.get(PendingTransfer, pending_id)
        if row is not None:
            row.message_id = int(message_id)


async def _set_state(pending_id: int, state: str) -> None:
    """Best-effort state write in its own transaction (error paths only)."""
    try:
        async with get_session() as session:
            row = await session.get(PendingTransfer, pending_id)
            if row is not None:
                row.state = state
    except Exception:  # pragma: no cover - defensive
        logger.exception(f"could not set transfer {pending_id} state to {state}")


# ---------------------------------------------------------------------------
# Callback handling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Snapshot:
    """The pending row's data, copied out of the transaction."""

    id: int
    from_user_id: int
    to_user_id: int
    item_type: str
    amount: int
    chat_id: int
    message_id: Optional[int]


@dataclass(frozen=True)
class _Claim:
    """Outcome of the row-claim transaction."""

    kind: str  # "missing" | "not_owner" | "already" | "expired" | "claimed"
    snapshot: Optional[_Snapshot] = None


async def _lock_pending(session, pending_id: int) -> Optional[PendingTransfer]:
    """The pending row under ``SELECT ... FOR UPDATE``."""
    return await session.scalar(
        select(PendingTransfer)
        .where(PendingTransfer.id == pending_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _snapshot(row: PendingTransfer) -> _Snapshot:
    return _Snapshot(
        id=int(row.id),
        from_user_id=int(row.from_user_id),
        to_user_id=int(row.to_user_id),
        item_type=row.item_type,
        amount=int(row.amount),
        chat_id=int(row.chat_id),
        message_id=int(row.message_id) if row.message_id is not None else None,
    )


async def _claim_pending(pending_id: int, presser_id: Optional[int]) -> _Claim:
    """Txn 1: validate the row and move ``pending`` -> ``processing``.

    The row is row-locked for the whole check, so two simultaneous presses
    can never both see ``pending``: the loser blocks and then observes
    ``processing``/``done`` and takes the ``already`` branch.
    """
    async with get_session() as session:
        row = await _lock_pending(session, pending_id)
        if row is None:
            return _Claim("missing")

        if presser_id != int(row.from_user_id):
            # Someone else's button: no state change, no edit.
            return _Claim("not_owner")

        if row.state != PendingTransferState.PENDING:
            return _Claim("already")

        if _now() >= (_as_naive_utc(row.expires_at) or _now()):
            row.state = PendingTransferState.CANCELLED
            return _Claim("expired", _snapshot(row))

        row.state = PendingTransferState.PROCESSING
        return _Claim("claimed", _snapshot(row))


async def _cancel_pending(pending_id: int, presser_id: Optional[int]) -> _Claim:
    """Txn 1 for ❌: validate the row and move ``pending`` -> ``cancelled``."""
    async with get_session() as session:
        row = await _lock_pending(session, pending_id)
        if row is None:
            return _Claim("missing")

        if presser_id != int(row.from_user_id):
            return _Claim("not_owner")

        if row.state != PendingTransferState.PENDING:
            return _Claim("already")

        row.state = PendingTransferState.CANCELLED
        return _Claim("cancelled", _snapshot(row))


async def _load_displays(session, snapshot: _Snapshot) -> tuple[str, str]:
    """``(sender display, receiver display)`` from the authoritative rows."""
    sender = await session.get(User, snapshot.from_user_id)
    receiver = await session.get(User, snapshot.to_user_id)
    return (
        display_name(
            sender.first_name if sender else None,
            sender.username if sender else None,
        ),
        display_name(
            receiver.first_name if receiver else None,
            receiver.username if receiver else None,
        ),
    )


async def _drop_keyboard(callback: CallbackQuery) -> None:
    """Remove the inline keyboard, if the message still has one."""
    message = callback.message
    if message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception as error:
        logger.debug(f"transfer keyboard could not be removed: {error}")


async def _answer(callback: CallbackQuery, text: str) -> None:
    await safe_answer_callback(callback, text, show_alert=False)


@router.callback_query(F.data.regexp(r"^tr:confirm:\d+$"))
async def handle_transfer_confirm(callback: CallbackQuery, bot: Bot) -> None:
    """✅ — claim the pending row, then move the balance in a second txn."""
    pending_id = _callback_pending_id(callback.data, CONFIRM_PREFIX)
    presser_id = callback.from_user.id if callback.from_user else None
    if pending_id is None:  # pragma: no cover - the filter guarantees digits
        await _answer(callback, INVALID_REQUEST_TEXT)
        return

    claim = await _claim_pending(pending_id, presser_id)

    if claim.kind == "missing":
        await _answer(callback, INVALID_REQUEST_TEXT)
        await _drop_keyboard(callback)
        return

    if claim.kind == "not_owner":
        await _answer(callback, NOT_YOUR_BUTTON_TEXT)
        return

    if claim.kind == "already":
        await _answer(callback, ALREADY_PROCESSED_TEXT)
        await _drop_keyboard(callback)
        return

    if claim.kind == "expired":
        await _answer(callback, EXPIRED_TEXT)
        await safe_edit_callback_message(
            callback,
            bot,
            render_expired(await _confirmation_body(claim.snapshot)),
            reply_markup=None,
        )
        return

    snapshot = claim.snapshot
    if snapshot is None:  # pragma: no cover - defensive
        await _answer(callback, INVALID_REQUEST_TEXT)
        return

    try:
        async with get_session() as session:
            result = await execute_transfer(
                session,
                snapshot.from_user_id,
                snapshot.to_user_id,
                snapshot.item_type,
                snapshot.amount,
            )
            # Same transaction as the money: the state can never disagree
            # with what actually happened.
            row = await _lock_pending(session, pending_id)
            if row is not None:
                row.state = (
                    PendingTransferState.DONE
                    if result.ok
                    else PendingTransferState.FAILED
                )
            config = await load_transfer_config(session)
            from_display, to_display = await _load_displays(session, snapshot)
    except Exception as error:  # pragma: no cover - defensive
        # get_session() rolled the whole transaction back, so neither the
        # balance nor the state changed; mark the row failed and move on.
        logger.exception(f"transfer {pending_id} failed while executing: {error}")
        await _set_state(pending_id, PendingTransferState.FAILED)
        await _answer(callback, render_reason("unknown"))
        await safe_edit_callback_message(
            callback, bot, render_failure("unknown"), reply_markup=None
        )
        return

    if not result.ok:
        reason_text = render_reason(
            result.reason,
            min_amount=config.min_amount,
            max_amount=config.max_amount,
        )
        await _answer(callback, reason_text)
        await safe_edit_callback_message(
            callback,
            bot,
            render_failure(
                result.reason,
                min_amount=config.min_amount,
                max_amount=config.max_amount,
            ),
            reply_markup=None,
        )
        return

    await _answer(callback, SUCCESS_TOAST)
    await safe_edit_callback_message(
        callback,
        bot,
        fix_bidi(
            render_success(
                to_display=to_display,
                item_type=snapshot.item_type,
                amount=result.amount_received,
                fee=result.fee,
            )
        ),
        reply_markup=None,
    )
    await _notify_receiver(
        callback,
        bot,
        snapshot,
        from_display=from_display,
        to_display=to_display,
        amount=result.amount_received,
    )


async def _confirmation_body(snapshot: Optional[_Snapshot]) -> str:
    """Re-render the confirmation body for the "expired" edit.

    The original text is not persisted, so it is rebuilt from the pending
    row (and the live fee config) with the same renderer the sender saw.
    """
    if snapshot is None:  # pragma: no cover - the caller always has one
        return EXPIRED_TEXT
    async with get_session() as session:
        config = await load_transfer_config(session)
        from_display, to_display = await _load_displays(session, snapshot)
    return render_confirmation(
        from_display=from_display,
        to_display=to_display,
        item_type=snapshot.item_type,
        amount=snapshot.amount,
        fee=compute_fee(snapshot.amount, config.fee_percent),
    )


async def _notify_receiver(
    callback: CallbackQuery,
    bot: Bot,
    snapshot: _Snapshot,
    *,
    from_display: str,
    to_display: str,
    amount: int,
) -> None:
    """Tell the receiver in the same chat, in the transfer's thread.

    The notification always names the receiver (``from_display`` is the
    sender), so it is unambiguous who received what. It replies to the
    confirmation message because that id is the only one persisted for the
    pending row (see the phase report: replying to B's *original* message
    would need a second message-id column, i.e. a schema change).
    """
    text = fix_bidi(
        render_receiver_notification(
            from_display=from_display,
            item_type=snapshot.item_type,
            amount=amount,
            to_mention=(
                f"[{to_display}](tg://user?id={int(snapshot.to_user_id)})"
            ),
        )
    )
    try:
        await bot.send_message(
            chat_id=snapshot.chat_id,
            text=text,
            reply_to_message_id=snapshot.message_id,
            parse_mode="Markdown",
        )
    except Exception as error:  # pragma: no cover - presentation only
        # The transfer is already committed; a failed notification is noise.
        logger.warning(f"transfer receiver notification failed ({snapshot.id}): {error}")


@router.callback_query(F.data.regexp(r"^tr:cancel:\d+$"))
async def handle_transfer_cancel(callback: CallbackQuery, bot: Bot) -> None:
    """❌ — cancel a pending transfer."""
    pending_id = _callback_pending_id(callback.data, CANCEL_PREFIX)
    presser_id = callback.from_user.id if callback.from_user else None
    if pending_id is None:  # pragma: no cover - the filter guarantees digits
        await _answer(callback, INVALID_REQUEST_TEXT)
        return

    claim = await _cancel_pending(pending_id, presser_id)

    if claim.kind == "missing":
        await _answer(callback, INVALID_REQUEST_TEXT)
        await _drop_keyboard(callback)
        return

    if claim.kind == "not_owner":
        await _answer(callback, NOT_YOUR_BUTTON_TEXT)
        return

    if claim.kind == "already":
        await _answer(callback, ALREADY_PROCESSED_TEXT)
        await _drop_keyboard(callback)
        return

    await _answer(callback, CANCELLED_TOAST)
    await safe_edit_callback_message(
        callback, bot, CANCELLED_TEXT, reply_markup=None
    )


# ---------------------------------------------------------------------------
# Periodic cleanup
# ---------------------------------------------------------------------------


async def run_pending_cleanup_forever(stop_event: asyncio.Event) -> None:
    """Cancel expired pending transfers every 60 s until ``stop_event``.

    Mirrors :meth:`sib.services.maintenance.service.MaintenanceService
    .run_forever`: one task started at boot, cancelled at shutdown, every
    iteration isolated so a database hiccup can never kill the loop.
    """
    logger.info(
        f"Transfer pending cleanup started "
        f"(interval={PENDING_CLEANUP_INTERVAL_SECONDS}s)"
    )
    try:
        while not stop_event.is_set():
            try:
                async with get_session() as session:
                    cancelled = await cleanup_expired_pending(session)
                if cancelled:
                    logger.info(f"Transfer pending cleanup cancelled {cancelled} row(s)")
            except Exception:
                logger.exception(
                    "Transfer pending cleanup failed; retrying next interval"
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=PENDING_CLEANUP_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        logger.info("Transfer pending cleanup cancelled")
        raise
    finally:
        logger.info("Transfer pending cleanup stopped")
