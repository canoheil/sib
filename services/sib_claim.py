"""Sib claim: the "سیب" group wake word is a REAL daily action.

Saying the exact word "سیب" in a group adds apples to the user's wallet
(random 5–10 reward, per-user cooldown — Phase N7). The cooldown is
derived from the AppleTransaction ledger (reason='sib_claim'), so no
schema change is needed and every award stays auditable. All clock math
uses the MariaDB clock (SQL NOW()) on BOTH sides. Reward timestamps are
normalized explicitly to UTC for the Tehran golden-hour check; naive database
timestamps are never interpreted as server-local time.

Messages are short, friendly and in the Sib character voice (task spec #5);
the success line is drawn at random from the N7 narrative pool
(``sib_claim_narrative``).
"""

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import AppleTransaction, User
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.golden_hour import reward_multiplier as golden_hour_multiplier
from sib.services.sib_claim_narrative import pick_claim_narrative

CLAIM_REASON = "sib_claim"

#: N7: each claim pays a RANDOM amount in this range (inclusive). The
#: cooldown stays fixed (see ``sib_claim_cooldown_seconds`` in config).
MIN_CLAIM_AMOUNT = 5
MAX_CLAIM_AMOUNT = 10


def roll_claim_amount() -> int:
    """Random reward for one claim (N7: 5–10 apples)."""
    return random.randint(MIN_CLAIM_AMOUNT, MAX_CLAIM_AMOUNT)

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_NUM_WORDS = {
    1: "یک",
    2: "دو",
    3: "سه",
    4: "چهار",
    5: "پنج",
    6: "شش",
    7: "هفت",
    8: "هشت",
    9: "نه",
    10: "ده",
}


def fa_digits(value) -> str:
    """Convert Latin digits to Persian digits for user-facing text."""
    return str(value).translate(_FA_DIGITS)


def fa_count(value: int) -> str:
    """Persian word for small counts (1-10), Persian digits otherwise."""
    return _NUM_WORDS.get(value, fa_digits(value))


def format_cooldown_message(
    remaining_seconds: int,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Describe the wait and the reward range available when it ends."""
    remaining = max(1, int(math.ceil(remaining_seconds)))

    if remaining <= 15:
        return (
            "🍎 فقط چند ثانیه دیگه!\n\n"
            "کمی صبر کن؛ سیبات تا چند لحظه‌ی دیگه آماده‌ست. ⏳"
        )
    if remaining < 60:
        return (
            f"🍎 یه کم صبر کن!\n\n"
            f"{fa_digits(remaining)} ثانیه دیگه دوباره می‌تونی سیب بگیری."
        )

    minutes, seconds = divmod(remaining, 60)
    if seconds == 0:
        wait = f"{fa_digits(minutes)} دقیقه"
    else:
        wait = f"{fa_digits(minutes)} دقیقه و {fa_digits(seconds)} ثانیه"
    actual_remaining = max(0.0, float(remaining_seconds))
    available_at = (now or datetime.now(timezone.utc)) + timedelta(
        seconds=actual_remaining
    )
    multiplier = golden_hour_multiplier(CLAIM_REASON, now=available_at)
    min_amount = MIN_CLAIM_AMOUNT * multiplier
    max_amount = MAX_CLAIM_AMOUNT * multiplier
    return (
        f"🍎 هنوز زوده 😄\n\n"
        f"{wait} دیگه دوباره می‌تونی از {fa_digits(min_amount)} "
        f"تا {fa_digits(max_amount)} سیب بگیری."
    )


def format_claim_success(amount: int, cooldown_seconds: int) -> str:
    """Friendly success text in the Sib voice (N7: random narrative line)."""
    minutes = max(1, int(round(cooldown_seconds / 60)))
    return (
        f"{pick_claim_narrative(amount)}\n\n"
        f"{fa_count(minutes)} دقیقه دیگه دوباره می‌تونی بیای سراغم. 😌"
    )


class SibClaimService:
    """Per-user apple claim with a cooldown, backed by the wallet ledger."""

    @classmethod
    async def _last_claim_at(cls, session: AsyncSession, user_id: int) -> Optional[object]:
        return await session.scalar(
            select(AppleTransaction.created_at)
            .where(AppleTransaction.user_id == user_id)
            .where(AppleTransaction.reason == CLAIM_REASON)
            .order_by(AppleTransaction.created_at.desc(), AppleTransaction.id.desc())
            .limit(1)
        )

    @classmethod
    async def claim(
        cls,
        session: AsyncSession,
        user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> Tuple[str, bool]:
        """Try to claim. Returns (message, awarded).

        On success the award is persisted to the AppleWallet and recorded
        in the AppleTransaction ledger (auditable, idempotent per claim).
        Concurrent claims are safe: a per-user row lock serializes them and
        a ledger-unique key gates the award; lock races are retried once.
        """
        # N7: the reward is a fresh random roll per claim (5–10). The
        # cooldown stays config-driven and untouched.
        amount = roll_claim_amount()
        cooldown = (
            await ConfigService.get_int(session, "sib_claim_cooldown_seconds") or 300
        )
        awarded_at = now or datetime.now(timezone.utc)

        for attempt in range(2):
            try:
                return await cls._claim_once(
                    session, user_id, amount, cooldown, awarded_at
                )
            except (IntegrityError, OperationalError) as exc:
                # Lost a lock/unique race; roll back and retry on a fresh
                # snapshot - the second attempt sees the winner's claim and
                # answers with the cooldown message.
                logger.warning(f"sib claim race for user {user_id} (attempt {attempt}): {exc}")
                await session.rollback()
        return format_cooldown_message(cooldown - 1), False

    @classmethod
    async def _claim_once(
        cls,
        session: AsyncSession,
        user_id: int,
        amount: int,
        cooldown: int,
        awarded_at: datetime,
    ) -> Tuple[str, bool]:
        # Serialize claims per user: the row lock on the (existing) user row
        # makes concurrent "سیب" messages queue up instead of deadlocking on
        # the lazily-created wallet row. Held until this session commits.
        await session.execute(select(User.id).where(User.id == user_id).with_for_update())

        # Both timestamps come from the MariaDB clock -> subtraction is safe.
        db_now = await session.scalar(select(func.now()))
        last_at = await cls._last_claim_at(session, user_id)
        if last_at is not None:
            elapsed = (db_now - last_at).total_seconds()
            if elapsed < cooldown:
                return format_cooldown_message(cooldown - elapsed, now=db_now), False

        multiplier = golden_hour_multiplier(CLAIM_REASON, now=awarded_at)
        awarded_amount = amount * multiplier

        # Atomic award gate: the idempotency key names the PREVIOUS claim
        # (its ledger id). Two concurrent claims compute the same key and
        # the ledger's unique constraint lets exactly one of them through -
        # even across a second-boundary race.
        prev_id = await session.scalar(
            select(func.max(AppleTransaction.id))
            .where(AppleTransaction.user_id == user_id)
            .where(AppleTransaction.reason == CLAIM_REASON)
        )
        key = f"{CLAIM_REASON}:{user_id}:{prev_id or 0}"

        wallet = await AppleWalletService._get_or_create(session, user_id)
        wallet.balance += awarded_amount
        session.add(
            AppleTransaction(
                user_id=user_id,
                amount=awarded_amount,
                reason=CLAIM_REASON,
                ref_type="wake_word",
                idempotency_key=key,
                reward_multiplier=multiplier,
                balance_after=wallet.balance,
            )
        )
        await session.flush()

        log_event(
            Events.APPLE_TRANSACTION,
            {
                "user_id": user_id,
                "action": "sib_claim",
                "amount": awarded_amount,
                "balance": wallet.balance,
            },
        )
        return format_claim_success(awarded_amount, cooldown), True
