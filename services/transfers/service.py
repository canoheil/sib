"""User-to-user apple transfers - transactional service layer (Phase 1).

No Telegram, no handlers, no Persian text: this module validates, locks,
moves balances and writes the audit row, and reports the outcome as a
:class:`TransferResult` with a machine-readable ``reason`` code.

Storage of the four transferable currencies
-------------------------------------------
============================  =================================================
``item_type``                 where the balance lives
============================  =================================================
``apple``                     ``apple_wallets.balance`` (regular Apple currency)
``apple_gold``                ``user_items`` row with ``item_key='apple_gold'``
``apple_diamond``             ``user_items`` row ``item_key='apple_diamond'``
``apple_obsidian``            ``user_items`` row ``item_key='apple_obsidian'``
============================  =================================================

Transaction rules
-----------------
* Everything happens in ONE transaction owned by the caller. This service
  never commits and never rolls back; it only flushes.
* Validation runs in a fixed order (see :func:`execute_transfer`) and every
  refusal returns BEFORE any balance is touched, so a failed transfer has no
  side effects (no log row, no balance change).
* Locks are taken in the project's global order (``services/lock_order.py``):
  the two ``users`` rows first, ascending ``user_id`` (rank 0), then the
  wallet rows (rank 6) or the inventory rows (rank 5) also ascending
  ``user_id``. See :func:`execute_transfer` for the deadlock argument.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import (
    AppleWallet,
    PendingTransfer,
    PendingTransferState,
    TransferLog,
    User,
    UserItem,
)
from sib.services.config_store import ConfigService
from sib.services.lock_order import lock_user_rows_in_order
from sib.services.transfers.parser import ITEM_APPLE, TRANSFER_ITEM_TYPES

TEHRAN_TZ = ZoneInfo("Asia/Tehran")

# Reason codes returned in TransferResult.reason (Phase 2 owns the Persian
# wording for each one).
REASON_DISABLED = "disabled"
REASON_OUT_OF_BOUNDS = "amount_out_of_bounds"
REASON_SELF = "self"
REASON_USER_NOT_FOUND = "user_not_found"
REASON_BANNED = "banned"
REASON_BOT = "bot"
REASON_COOLDOWN = "cooldown"
REASON_DAILY_LIMIT = "daily_limit"
REASON_INSUFFICIENT = "insufficient"
REASON_INVALID_ITEM_TYPE = "invalid_item_type"
REASON_INVALID_AMOUNT = "invalid_amount"


@dataclass(frozen=True)
class TransferResult:
    """Outcome of :func:`execute_transfer` (never an exception).

    ``amount_sent`` is what the SENDER paid (amount + fee) and
    ``amount_received`` is what the RECEIVER got (== amount). On a refusal
    both are 0 and ``transfer_id`` is ``None``.
    """

    ok: bool
    reason: Optional[str] = None
    amount_sent: int = 0
    amount_received: int = 0
    fee: int = 0
    transfer_id: Optional[int] = None


@dataclass(frozen=True)
class TransferConfig:
    """Snapshot of the six ``transfer_*`` system_config values."""

    enabled: bool
    min_amount: int
    max_amount: int
    fee_percent: int
    daily_limit: int
    cooldown_seconds: int


def _as_int(value: object, default: int) -> int:
    """Tolerant int cast for config values (JSON int, or a raw string)."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_bool(value: object, default: bool) -> bool:
    """Tolerant bool cast: config may arrive as bool or as "true"/"false"."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _utc_now() -> datetime:
    """Naive UTC "now" - the project's stored-timestamp convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize a stored timestamp to naive UTC for comparisons.

    MariaDB ``DATETIME`` and SQLite both hand back naive values (already UTC),
    but the value can be tz-aware right after an in-session flush.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def tehran_day_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """``[start, end)`` of the current Asia/Tehran calendar day, in naive UTC."""
    now_utc = now or _utc_now()
    local_now = now_utc.replace(tzinfo=timezone.utc).astimezone(TEHRAN_TZ)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
        local_end.astimezone(timezone.utc).replace(tzinfo=None),
    )


async def load_transfer_config(session: AsyncSession) -> TransferConfig:
    """Read the six transfer settings through ConfigService (never cached)."""
    return TransferConfig(
        enabled=_as_bool(await ConfigService.get(session, "transfer_enabled"), True),
        min_amount=max(
            1, _as_int(await ConfigService.get(session, "transfer_min_amount"), 1)
        ),
        max_amount=max(
            1, _as_int(await ConfigService.get(session, "transfer_max_amount"), 10000)
        ),
        fee_percent=min(
            100, max(0, _as_int(await ConfigService.get(session, "transfer_fee_percent"), 0))
        ),
        daily_limit=max(
            0, _as_int(await ConfigService.get(session, "transfer_daily_limit"), 0)
        ),
        cooldown_seconds=max(
            0,
            _as_int(await ConfigService.get(session, "transfer_cooldown_seconds"), 0),
        ),
    )


def compute_fee(amount: int, fee_percent: int) -> int:
    """``ceil(amount * fee_percent / 100)`` with pure integer arithmetic."""
    if amount <= 0 or fee_percent <= 0:
        return 0
    return -(-amount * fee_percent // 100)


def _fail(reason: str) -> TransferResult:
    return TransferResult(ok=False, reason=reason)


async def _lock_wallet(
    session: AsyncSession, user_id: int
) -> Optional[AppleWallet]:
    """Row-lock (rank 6) an existing wallet row; ``None`` when absent."""
    return await session.scalar(
        select(AppleWallet)
        .where(AppleWallet.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _lock_user_item(
    session: AsyncSession, user_id: int, item_key: str
) -> Optional[UserItem]:
    """Row-lock (rank 5) an existing inventory row; ``None`` when absent."""
    return await session.scalar(
        select(UserItem)
        .where(UserItem.user_id == user_id)
        .where(UserItem.item_key == item_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _latest_transfer_at(
    session: AsyncSession, user_id: int
) -> Optional[datetime]:
    """Timestamp of the sender's most recent completed transfer."""
    return await session.scalar(
        select(TransferLog.created_at)
        .where(TransferLog.from_user_id == user_id)
        .order_by(TransferLog.created_at.desc(), TransferLog.id.desc())
        .limit(1)
    )


async def _transfers_today(session: AsyncSession, user_id: int) -> int:
    """Completed transfers of ``user_id`` in the current Asia/Tehran day."""
    day_start, day_end = tehran_day_bounds()
    return int(
        await session.scalar(
            select(func.count(TransferLog.id)).where(
                TransferLog.from_user_id == user_id,
                TransferLog.created_at >= day_start,
                TransferLog.created_at < day_end,
            )
        )
        or 0
    )


async def _transfers_today_locked(session: AsyncSession, user_id: int) -> int:
    """Same day count, read as a LOCKING read (post-lock re-check).

    A plain ``SELECT`` would be answered from the transaction's REPEATABLE
    READ snapshot, which was established before the competing transfer
    committed - i.e. it would still see the stale count. Row locking reads
    the latest committed rows (and, under InnoDB REPEATABLE READ, takes the
    gap locks on the sender's ``(from_user_id, created_at)`` range), so the
    re-check really does observe a concurrent commit.
    """
    day_start, day_end = tehran_day_bounds()
    rows = await session.scalars(
        select(TransferLog.id)
        .where(
            TransferLog.from_user_id == user_id,
            TransferLog.created_at >= day_start,
            TransferLog.created_at < day_end,
        )
        .with_for_update()
    )
    return len(list(rows))


async def execute_transfer(
    session: AsyncSession,
    from_user_id: int,
    to_user_id: int,
    item_type: str,
    amount: int,
) -> TransferResult:
    """Move ``amount`` of ``item_type`` from one player to another.

    Validation order (each refusal leaves the database untouched):

    1. config ``transfer_enabled`` -> ``disabled``
    2. amount inside [min, max] -> ``amount_out_of_bounds``
    3. same account -> ``self``
    4. both users exist -> ``user_not_found``
    5. neither banned / is a bot -> ``banned`` / ``bot``
    6. cooldown -> ``cooldown``
    7. daily limit (Tehran day) -> ``daily_limit``
    8. fee = ceil(amount * percent / 100); total_sent = amount + fee
    9. lock user rows + the balance rows (deterministic order), re-check the
       sender balance -> ``insufficient``
    10. move the balance and INSERT the ``transfer_log`` row

    Deadlock safety: the two ``users`` rows are locked first in ascending id
    order, then the balance rows are locked in the same ascending id order
    (inventory row 5 / wallet row 6, per ``services/lock_order.py``). A->B and
    B->A therefore acquire the SAME physical locks in the SAME order, so they
    serialize on the first shared lock instead of forming a cycle.

    The caller owns the transaction: nothing is committed here and every
    refusal returns a result instead of raising.
    """
    config = await load_transfer_config(session)

    if not config.enabled:
        return _fail(REASON_DISABLED)

    if item_type not in TRANSFER_ITEM_TYPES:
        return _fail(REASON_INVALID_ITEM_TYPE)

    if isinstance(amount, bool) or not isinstance(amount, int):
        return _fail(REASON_INVALID_AMOUNT)

    if amount < config.min_amount or amount > config.max_amount:
        return _fail(REASON_OUT_OF_BOUNDS)

    if from_user_id == to_user_id:
        return _fail(REASON_SELF)

    sender = await session.get(User, from_user_id)
    receiver = await session.get(User, to_user_id)
    if sender is None or receiver is None:
        return _fail(REASON_USER_NOT_FOUND)

    if sender.is_banned or receiver.is_banned:
        return _fail(REASON_BANNED)

    if sender.is_bot or receiver.is_bot:
        return _fail(REASON_BOT)

    now = _utc_now()

    if config.cooldown_seconds > 0:
        last_at = _as_naive_utc(await _latest_transfer_at(session, from_user_id))
        if last_at is not None:
            elapsed = (now - last_at).total_seconds()
            if 0 <= elapsed < config.cooldown_seconds:
                return _fail(REASON_COOLDOWN)

    if config.daily_limit > 0:
        if await _transfers_today(session, from_user_id) >= config.daily_limit:
            return _fail(REASON_DAILY_LIMIT)

    fee = compute_fee(amount, config.fee_percent)
    total_sent = amount + fee

    # --- locks (deterministic order) -------------------------------------
    try:
        await lock_user_rows_in_order(session, (from_user_id, to_user_id))
    except ValueError:  # user vanished between the read and the lock
        return _fail(REASON_USER_NOT_FOUND)

    # TOCTOU re-check (Phase 1 review): the pre-lock count above can be raced
    # by another transfer of the same sender. Now that the sender's user row
    # (rank 0) is held, every other transfer of this sender is blocked or
    # already committed, so re-counting in the same transaction closes the
    # window. Additive: the pre-lock validation order is unchanged.
    if config.daily_limit > 0:
        if await _transfers_today_locked(session, from_user_id) >= config.daily_limit:
            return _fail(REASON_DAILY_LIMIT)

    first_id, second_id = sorted((from_user_id, to_user_id))

    if item_type == ITEM_APPLE:
        wallets: dict[int, Optional[AppleWallet]] = {}
        for user_id in (first_id, second_id):
            wallets[user_id] = await _lock_wallet(session, user_id)
        wallet = wallets[from_user_id]
        if wallet is None or wallet.balance < total_sent:
            return _fail(REASON_INSUFFICIENT)
        receiver_wallet = wallets[to_user_id]
        if receiver_wallet is None:
            # Safe insert: the receiver's users row (rank 0) is already held,
            # so no other transfer can create this wallet in parallel.
            receiver_wallet = AppleWallet(user_id=to_user_id, balance=0)
            session.add(receiver_wallet)
            await session.flush()
        wallet.balance -= total_sent
        receiver_wallet.balance += amount
    else:
        items: dict[int, Optional[UserItem]] = {}
        for user_id in (first_id, second_id):
            items[user_id] = await _lock_user_item(session, user_id, item_type)
        sender_item = items[from_user_id]
        if sender_item is None or sender_item.quantity < total_sent:
            return _fail(REASON_INSUFFICIENT)
        receiver_item = items[to_user_id]
        if receiver_item is None:
            # Same argument as the wallet insert above: the receiver's users
            # row serializes any competing insert for this (user, key) pair.
            receiver_item = UserItem(
                user_id=to_user_id, item_key=item_type, quantity=0
            )
            session.add(receiver_item)
            await session.flush()
        sender_item.quantity -= total_sent
        receiver_item.quantity += amount

    await session.flush()

    log = TransferLog(
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        item_type=item_type,
        amount=amount,
        fee=fee,
    )
    session.add(log)
    await session.flush()

    return TransferResult(
        ok=True,
        reason=None,
        amount_sent=total_sent,
        amount_received=amount,
        fee=fee,
        transfer_id=int(log.id),
    )


async def cleanup_expired_pending(session: AsyncSession) -> int:
    """Cancel every ``pending`` row whose ``expires_at`` has passed.

    Returns the number of rows updated. Caller owns the transaction.
    """
    result = await session.execute(
        update(PendingTransfer)
        .where(
            PendingTransfer.state == PendingTransferState.PENDING,
            PendingTransfer.expires_at < _utc_now(),
        )
        .values(state=PendingTransferState.CANCELLED)
    )
    return int(result.rowcount or 0)


class TransferService:
    """Namespace facade, so callers can use the other services' style.

    The canonical entry points are the module-level functions
    :func:`execute_transfer` and :func:`cleanup_expired_pending`; this class
    only exposes them as ``TransferService.transfer`` /
    ``TransferService.cleanup_expired_pending`` for discoverability.
    """

    transfer = staticmethod(execute_transfer)
    cleanup_expired_pending = staticmethod(cleanup_expired_pending)
