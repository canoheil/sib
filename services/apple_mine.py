"""Apple Mine reward and cooldown rules."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import random

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import AppleMineShift, PlayerCard
from sib.services.apple_mine_narrative import pick_narrative
from sib.services.apple_wallet import AppleWalletService
from sib.services.items import ItemService
from sib.services.lock_order import lock_user_row

WORK_COOLDOWN_SECONDS = 1800

# Normal Apples are wallet currency (apple_normal), while the three special
# Apples use their canonical user_items inventory keys.
OUTCOMES: list[tuple[str, int, str, int]] = [
    ("normal", 60, "apple_normal", 15),
    ("golden", 25, "apple_gold", 3),
    ("diamond", 12, "apple_diamond", 1),
    ("obsidian", 3, "apple_obsidian", 1),
]


class AppleMineError(Exception):
    """Base class for expected Apple Mine refusals."""


class CooldownError(AppleMineError):
    """Raised when the user has not finished the global work cooldown."""

    def __init__(self, remaining_seconds: int) -> None:
        self.remaining_seconds = max(0, int(remaining_seconds))
        super().__init__("Apple Mine cooldown is active")


class InvalidCardError(AppleMineError):
    """The selected card is not owned by the acting user."""


@dataclass(frozen=True)
class ShiftResult:
    outcome_code: str
    item_code: str
    amount: int
    narrative: str


def _as_utc(value: datetime) -> datetime:
    """Normalize database timestamps before doing elapsed-time arithmetic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _database_now(session: AsyncSession) -> datetime:
    """Read the database clock so cooldown checks use the same clock as rows."""
    value = await session.scalar(select(func.now()))
    if value is None:  # pragma: no cover - SQL NOW() always returns a value
        return datetime.now(timezone.utc)
    return value


async def _last_worked_at(session: AsyncSession, user_id: int) -> datetime | None:
    return await session.scalar(
        select(AppleMineShift.worked_at)
        .where(AppleMineShift.user_id == user_id)
        .order_by(desc(AppleMineShift.worked_at), desc(AppleMineShift.id))
        .limit(1)
    )


def _remaining_from(last_worked_at: datetime | None, now: datetime) -> int:
    if last_worked_at is None:
        return 0
    elapsed = (_as_utc(now) - _as_utc(last_worked_at)).total_seconds()
    remaining = math.ceil(WORK_COOLDOWN_SECONDS - elapsed)
    return min(WORK_COOLDOWN_SECONDS, max(0, remaining))


async def remaining_cooldown(session: AsyncSession, user_id: int) -> int:
    """Return seconds until this user's global mine cooldown expires."""
    now = await _database_now(session)
    last_worked_at = await _last_worked_at(session, user_id)
    return _remaining_from(last_worked_at, now)


async def perform_shift(
    session: AsyncSession,
    *,
    user_id: int,
    group_id: int | None,
    card_id: int | None,
) -> ShiftResult:
    """Complete one atomic shift, grant exactly one reward, and audit it.

    The user-row lock serializes concurrent taps across groups and workers.
    The card is checked for ownership but is neither consumed nor locked.
    """
    await lock_user_row(session, user_id)
    now = await _database_now(session)
    last_worked_at = await _last_worked_at(session, user_id)
    remaining = _remaining_from(last_worked_at, now)
    if remaining > 0:
        raise CooldownError(remaining)

    if card_id is not None:
        owned_card_id = await session.scalar(
            select(PlayerCard.id)
            .where(PlayerCard.id == card_id)
            .where(PlayerCard.user_id == user_id)
        )
        if owned_card_id is None:
            raise InvalidCardError("Selected card is not owned by this user")

    outcome_code, _weight, item_code, amount = random.choices(
        OUTCOMES,
        weights=[entry[1] for entry in OUTCOMES],
        k=1,
    )[0]

    shift = AppleMineShift(
        user_id=user_id,
        group_id=group_id,
        card_id=card_id,
        outcome=outcome_code,
        item_code=item_code,
        amount=amount,
        worked_at=now,
    )
    session.add(shift)
    await session.flush()

    if item_code == "apple_normal":
        await AppleWalletService.earn(
            session,
            user_id=user_id,
            amount=amount,
            reason="apple_mine",
            ref_type="apple_mine_shift",
            ref_id=shift.id,
            idempotency_key=f"apple_mine:{shift.id}",
        )
    else:
        await ItemService.grant(session, user_id, item_code, quantity=amount)

    return ShiftResult(
        outcome_code=outcome_code,
        item_code=item_code,
        amount=amount,
        narrative=pick_narrative(outcome_code, user_id),
    )
