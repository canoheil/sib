"""Sib Kiss: the reply-based "بوس سیبی" group interaction.

A user replies to another user's message in a group with «بوس سیبی». When
the interaction is valid and the sender is off kiss cooldown, BOTH
participants receive ``sib_kiss_amount`` apples (default 10) in one database
transaction.

This mirrors ``services/sib_hug.py`` with its own reasons, keys, and
cooldown, so the two interactions never share state:

- The sender's row uses ``reason='sib_kiss'``. ONLY that reason starts or
  checks the kiss cooldown, and it is written only for successful kisses.
- The recipient's row uses ``reason='sib_kiss_received'``, which the cooldown
  never reads. Being kissed never starts the recipient's own kiss cooldown.
- The Sib claim (``sib_claim``) and Hug (``sib_hug``) reasons are never read
  here, so their cooldowns are independent of this one.
- Both idempotency keys are derived from the Telegram message that carried
  the command (``chat_id:message_id``). Redelivery cannot pay twice.

Friendship Bonus (SOC1): when the sender and the target already share an
ACCEPTED ``apple_friendships`` row, BOTH of them receive the base amount plus
``sib_kiss_friend_bonus`` (10 + 10 = 20 by default). The bonus is decided
inside the same transaction but AFTER the cooldown has already passed, so it
can never resurrect a blocked attempt: the cooldown, the duplicate check and
the idempotency keys are all untouched by the bonus and cannot be farmed by
spamming a friend.

Concurrency and integrity (same guarantees as Hug):

1. Both participant ``User`` rows are locked in ascending id order. This is
   the first database access after validation.
2. The duplicate and cooldown checks are locking reads, so they see the latest
   committed kiss even under InnoDB REPEATABLE READ snapshots created earlier.
3. Both wallet rewards are written in ascending user-id order.
4. Both expected ledger keys are verified inside the same transaction before
   returning success. A missing key raises ``SibKissIntegrityError``.

The domain never commits or rolls back. The caller's ``get_session()`` block
owns the transaction: any exception raised here discards both rewards.
"""

import math
import random
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import AppleTransaction, User
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.friendship import FriendshipService
from sib.services.sib_claim import fa_digits

KISS_REASON = "sib_kiss"
KISS_RECEIVED_REASON = "sib_kiss_received"
KISS_REF_TYPE = "sib_kiss"
KISS_SCENE_KEY = "kiss"

KISS_AMOUNT_KEY = "sib_kiss_amount"
KISS_COOLDOWN_KEY = "sib_kiss_cooldown_seconds"
# SOC1 Friendship Bonus: added to KISS_AMOUNT_KEY for BOTH participants when
# they are accepted apple friends. 10 + 10 == the specified 20 per person.
KISS_FRIEND_BONUS_KEY = "sib_kiss_friend_bonus"

KISS_AWARDED = "awarded"
KISS_COOLDOWN = "cooldown"
KISS_DUPLICATE = "duplicate"

# At least 10 distinct variants. Placeholders: {a} sender, {b} recipient,
# {n} reward amount in Persian digits. Names arrive already escaped and
# wrapped as Markdown mentions.
KISS_MESSAGE_VARIANTS = (
    "😘 **{a}** **{b}** رو بوسید! هر دو **{n} سیب** گرفتن.",
    "💋 **{b}** یه بوس شیرین از **{a}** گرفت! **{n} سیب** به هر کدوم رسید.",
    "🍏 **{a}** به **{b}** یه بوس سیبی داد! هر کدوم **{n} سیب** گرفتن.",
    "😘 **{a}** **{b}** رو بوسیدش! **{n} سیب** هم به هر دو رسید.",
    "💋 **{a}** لپ **{b}** رو بوسید! هر دو **{n} سیب** گرفتن.",
    "🍎 بوسِ **{a}** به **{b}** رسید! جایزه‌ی هر دو **{n} سیب** شد.",
    "😘 **{a}** و **{b}** یه بوس سیبی رد و بدل کردن! هر کدوم **{n} سیب** گرفتن.",
    "💋 **{a}** برای **{b}** یه بوس فرستاد! **{n} سیب** به هر کدوم رسید.",
    "🍏 **{b}** یه بوس سیبی از **{a}** گرفت! هر دو **{n} سیب** گرفتن.",
    "😘 **{a}** با یه بوس **{b}** رو خوشحال کرد! **{n} سیب** هم به هر دو رسید.",
    "💋 **{a}** یه بوس کوچولو به **{b}** داد! هر کدوم **{n} سیب** گرفتن.",
    "🍎 **{a}** و **{b}** با یه بوس لبخند زدن! هر دو **{n} سیب** گرفتن.",
)

# SOC1 Friendship Bonus pool, used ONLY when the two are accepted apple
# friends. Same placeholders as above; each variant names the friendship and
# still prints the real granted amount, so the text can never advertise a
# reward the ledger did not pay.
KISS_FRIEND_MESSAGE_VARIANTS = (
    "😘 **{a}** **{b}** رو بوسید! دوستای سیبی چقدر همدیگه رو دوست دارن! ❤️ هر کدوم **{n} سیب** گرفتن.",
    "💋 **{a}** یه بوس دوستانه به **{b}** داد! پاداش دوستی **{n} سیب** به هر کدوم رسید.",
    "🍏 یه بوس سیبی بین دو تا رفیق: **{a}** و **{b}**! ❤️ هر کدوم **{n} سیب** گرفتن.",
    "😘 **{b}** از دوست سیبیش **{a}** یه بوس گرفت! **{n} سیب** هم به هر دو رسید.",
    "🍎 بوسِ **{a}** برای دوستش **{b}** شیرین‌تره! ❤️ هر دو **{n} سیب** گرفتن.",
    "💋 **{a}** و **{b}** با یه بوس دوستی‌شون رو جشن گرفتن! هر کدوم **{n} سیب** گرفتن.",
)


class SibKissError(Exception):
    """Base error for Sib Kiss domain refusals."""


class SibKissSelfTargetError(SibKissError):
    """The sender tried to kiss themselves."""


class SibKissInvalidTargetError(SibKissError):
    """The target is a bot or a missing participant row."""


class SibKissIntegrityError(SibKissError):
    """Ledger state was inconsistent (partial pair, or a reward was skipped)."""


@dataclass(frozen=True)
class KissResult:
    status: str
    amount: int = 0
    remaining_seconds: int = 0
    # SOC1: True when this pair are accepted apple friends, so the amount
    # above already includes the friendship bonus. Drives the narrative pool.
    friendship_bonus: bool = False


def kiss_idempotency_keys(message_key: str, sender_id: int, target_id: int) -> tuple:
    """(sender_key, recipient_key) for one Telegram message."""
    return (
        f"{KISS_REASON}:{message_key}:{sender_id}",
        f"{KISS_RECEIVED_REASON}:{message_key}:{target_id}",
    )


def format_kiss_cooldown_message(remaining_seconds: float) -> str:
    """Friendly cooldown text in the Sib voice (same shape as the hug text)."""
    remaining = max(1, int(math.ceil(remaining_seconds)))
    if remaining < 60:
        wait = f"{fa_digits(remaining)} ثانیه"
    else:
        minutes, seconds = divmod(remaining, 60)
        wait = f"{fa_digits(minutes)} دقیقه"
        if seconds:
            wait += f" و {fa_digits(seconds)} ثانیه"
    return f"😘 فقط **{wait}** صبر کن؛ بعدش می‌تونی دوباره بوس سیبی بدی!"


def pick_kiss_message(
    sender_mention: str,
    target_mention: str,
    amount: int,
    rng: Optional[random.Random] = None,
    *,
    friends: bool = False,
) -> str:
    """One randomly chosen kiss variant, filled with the two mentions.

    ``friends=True`` picks from the Friendship Bonus pool, which names the
    friendship in the sentence. Both pools print ``amount`` in Persian digits,
    so the narrative always matches what the ledger actually paid.
    """
    chooser = rng or random
    pool = KISS_FRIEND_MESSAGE_VARIANTS if friends else KISS_MESSAGE_VARIANTS
    template = chooser.choice(pool)
    return template.format(
        a=sender_mention,
        b=target_mention,
        n=fa_digits(amount),
    )


class SibKissService:
    """Validate, enforce the sender kiss cooldown, and award both participants."""

    @classmethod
    async def kiss(
        cls,
        session: AsyncSession,
        *,
        sender_id: int,
        target_id: int,
        message_key: str,
    ) -> KissResult:
        """Run one kiss inside the caller's transaction.

        Returns ``KISS_AWARDED`` (both rewards staged in ``session``),
        ``KISS_COOLDOWN`` (nothing written), or ``KISS_DUPLICATE`` (this
        message was already processed; nothing written).

        On ``KISS_AWARDED``, ``amount`` is what EACH participant received and
        ``friendship_bonus`` says whether it includes the Friendship Bonus.
        """
        if sender_id == target_id:
            raise SibKissSelfTargetError("a user cannot kiss themselves")

        amount = await ConfigService.get_int(session, KISS_AMOUNT_KEY)
        cooldown = await ConfigService.get_int(session, KISS_COOLDOWN_KEY)

        # 1) Lock both participant rows in ascending id order.
        ordered_ids = sorted({sender_id, target_id})
        users = (
            await session.execute(
                select(User.id, User.is_bot)
                .where(User.id.in_(ordered_ids))
                .order_by(User.id)
                .with_for_update()
            )
        ).all()
        found = {int(row.id): bool(row.is_bot) for row in users}
        if set(found) != set(ordered_ids):
            raise SibKissInvalidTargetError("participant user row is missing")
        if found[target_id]:
            raise SibKissInvalidTargetError("bots cannot receive a kiss")

        # 2) Duplicate check (locking read: sees the latest committed kiss).
        sender_key, recipient_key = kiss_idempotency_keys(message_key, sender_id, target_id)
        existing = set(
            (
                await session.scalars(
                    select(AppleTransaction.idempotency_key)
                    .where(AppleTransaction.idempotency_key.in_([sender_key, recipient_key]))
                    .with_for_update()
                )
            ).all()
        )
        if existing:
            if existing == {sender_key, recipient_key}:
                return KissResult(status=KISS_DUPLICATE)
            raise SibKissIntegrityError("partial kiss ledger state for message")

        # 3) Sender kiss cooldown (locking read; DB clock on both sides).
        last = (
            await session.execute(
                select(AppleTransaction.created_at)
                .where(AppleTransaction.user_id == sender_id)
                .where(AppleTransaction.reason == KISS_REASON)
                .order_by(AppleTransaction.created_at.desc(), AppleTransaction.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).first()
        if last is not None:
            db_now = await session.scalar(select(func.now()))
            elapsed = (db_now - last.created_at).total_seconds()
            if elapsed < cooldown:
                return KissResult(
                    status=KISS_COOLDOWN,
                    remaining_seconds=cooldown - elapsed,
                )

        # 4) Friendship Bonus (SOC1): size the reward now that the kiss is
        #    known to go through. It runs AFTER the duplicate and cooldown
        #    gates, so the bonus can never turn a blocked kiss into a paying
        #    one, and it never touches the idempotency keys or the cooldown
        #    row - a pair of friends is still capped at one kiss per window.
        #    Both sides always get the SAME amount.
        friends = await FriendshipService.are_friends(session, sender_id, target_id)
        bonus = (
            await ConfigService.get_int(session, KISS_FRIEND_BONUS_KEY) if friends else 0
        )
        amount = amount + max(0, bonus)

        # 5) Both rewards, in ascending user-id order (wallet lock order).
        rewards = [
            (sender_id, KISS_REASON, target_id, sender_key),
            (target_id, KISS_RECEIVED_REASON, sender_id, recipient_key),
        ]
        for user_id, reason, ref_id, key in sorted(rewards, key=lambda r: r[0]):
            await AppleWalletService.earn(
                session,
                user_id=user_id,
                amount=amount,
                reason=reason,
                ref_type=KISS_REF_TYPE,
                ref_id=ref_id,
                idempotency_key=key,
            )

        # 6) Verify both expected ledger records exist inside this transaction.
        #    The pre-check proved neither key existed before, so any key found
        #    now was written by this call. A missing key means a reward was
        #    skipped (earn() returns silently on a duplicate key). Raising
        #    propagates out of get_session(), which rolls back BOTH rewards.
        written = set(
            (
                await session.scalars(
                    select(AppleTransaction.idempotency_key).where(
                        AppleTransaction.idempotency_key.in_([sender_key, recipient_key])
                    )
                )
            ).all()
        )
        if written != {sender_key, recipient_key}:
            raise SibKissIntegrityError(
                f"kiss reward verification failed: missing {sorted({sender_key, recipient_key} - written)}"
            )
        return KissResult(
            status=KISS_AWARDED, amount=amount, friendship_bonus=friends
        )
