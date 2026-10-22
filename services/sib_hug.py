"""Sib Hug: the reply-based "بغل سیبی" group interaction.

A user replies to another user's message in a group with «بغل سیبی». When
the interaction is valid and the sender is off cooldown, BOTH participants
receive ``sib_hug_amount`` apples (default 10) in one database transaction.

Ledger design (no schema change, see ``database/models/apple.py``):

- The sender's row uses ``reason='sib_hug'``. ONLY that reason starts or
  checks a cooldown, and it is written only for successful hugs, so a
  rejected attempt never establishes a cooldown.
- The recipient's row uses ``reason='sib_hug_received'``. It is never read
  by the cooldown check, so being hugged does not start the recipient's own
  hug cooldown.
- Both idempotency keys are derived from the Telegram message that carried
  the command (``chat_id:message_id``), so processing the same message twice
  can never award either side twice.

Friendship Bonus (SOC1): when the sender and the target already share an
ACCEPTED ``apple_friendships`` row, BOTH of them receive the base amount plus
``sib_hug_friend_bonus`` (10 + 10 = 20 by default). The bonus is decided
inside the same transaction but AFTER the cooldown has already passed, so it
can never resurrect a blocked attempt: the cooldown, the duplicate check and
the idempotency keys are all untouched by the bonus and cannot be farmed by
spamming a friend.

Concurrency: both participant ``User`` rows are locked in ascending id order
(the repository's global lock order). The duplicate and cooldown checks are
locking reads, so they see the latest committed hug even under InnoDB
REPEATABLE READ snapshots created earlier in the same request.

The domain never commits or rolls back. The caller's ``get_session()``
block owns the transaction: any exception raised here discards both
rewards together.
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

HUG_REASON = "sib_hug"
HUG_RECEIVED_REASON = "sib_hug_received"
HUG_REF_TYPE = "sib_hug"
HUG_SCENE_KEY = "hug"

HUG_AMOUNT_KEY = "sib_hug_amount"
HUG_COOLDOWN_KEY = "sib_hug_cooldown_seconds"
# SOC1 Friendship Bonus: added to HUG_AMOUNT_KEY for BOTH participants when
# they are accepted apple friends. 10 + 10 == the specified 20 per person.
HUG_FRIEND_BONUS_KEY = "sib_hug_friend_bonus"

HUG_AWARDED = "awarded"
HUG_COOLDOWN = "cooldown"
HUG_DUPLICATE = "duplicate"

# At least 10 distinct variants. Placeholders: {a} sender, {b} recipient,
# {n} reward amount in Persian digits. Names are passed in already escaped
# and wrapped as Markdown mentions.
HUG_MESSAGE_VARIANTS = (
    "🤗 **{a}** **{b}** رو محکم بغل کرد! هر دو **{n} سیب** گرفتن.",
    "🫂 **{b}** یه بغل گرم از **{a}** گرفت! **{n} سیب** به هر کدوم رسید.",
    "🍏 **{a}** و **{b}** همدیگه رو بغل کردن! هر کدوم **{n} سیب** گرفتن.",
    "🤗 **{a}** **{b}** رو بغلش کرد! **{n} سیب** هم به هر دو رسید.",
    "🍎 **{a}** با یه بغل دلِ **{b}** رو گرم کرد! هر دو **{n} سیب** گرفتن.",
    "🫂 بغلِ **{a}** به **{b}** رسید! جایزه‌ی هر دو **{n} سیب** شد.",
    "🤗 **{a}** و **{b}** تو یه بغل قفل شدن! هر کدوم **{n} سیب** گرفتن.",
    "🍏 **{a}** با یه بغل به **{b}** سلام کرد! **{n} سیب** هم به هر دو رسید.",
    "🫂 **{a}** **{b}** رو رها نکرد؛ بغلش محکمه! هر دو **{n} سیب** گرفتن.",
    "🍎 **{a}** به **{b}** یه بغل سیبی هدیه داد! **{n} سیب** به هر کدوم رسید.",
    "🤗 **{b}** از **{a}** یه بغل قشنگ گرفت! **{n} سیب** هم به هر دو رسید.",
    "🫂 **{a}** و **{b}** یه بغل سیبی رد و بدل کردن! هر کدوم **{n} سیب** گرفتن.",
)

# SOC1 Friendship Bonus pool, used ONLY when the two are accepted apple
# friends. Same placeholders as above; each variant names the friendship and
# still prints the real granted amount, so the text can never advertise a
# reward the ledger did not pay.
HUG_FRIEND_MESSAGE_VARIANTS = (
    "🤗 **{a}** **{b}** رو بغل کرد! دوستای سیبی چقدر همدیگه رو دوست دارن! ❤️ هر کدوم **{n} سیب** گرفتن.",
    "🫂 **{a}** رفیق سیبیش **{b}** رو بغل کرد! پاداش دوستی **{n} سیب** به هر کدوم رسید.",
    "🍏 یه بغل دوستانه بین **{a}** و **{b}**! ❤️ هر کدوم **{n} سیب** گرفتن.",
    "🤗 **{a}** و **{b}** که دوستن، همدیگه رو بغل کردن! هر کدوم **{n} سیب** پاداش دوستی گرفتن.",
    "🍎 بغلِ **{a}** برای دوستش **{b}** شیرین‌تره! ❤️ هر دو **{n} سیب** گرفتن.",
    "🫂 **{b}** از دوست سیبیش **{a}** یه بغل گرم گرفت! **{n} سیب** به هر کدوم رسید.",
)


class SibHugError(Exception):
    """Base error for Sib Hug domain refusals."""


class SibHugSelfTargetError(SibHugError):
    """The sender tried to hug themselves."""


class SibHugInvalidTargetError(SibHugError):
    """The target is a bot or a missing/unknown participant."""


class SibHugIntegrityError(SibHugError):
    """Ledger state was inconsistent (e.g. only one side of a pair was keyed)."""


@dataclass(frozen=True)
class HugResult:
    status: str
    amount: int = 0
    remaining_seconds: int = 0
    # SOC1: True when this pair are accepted apple friends, so the amount
    # above already includes the friendship bonus. Drives the narrative pool.
    friendship_bonus: bool = False


def hug_idempotency_keys(message_key: str, sender_id: int, target_id: int) -> tuple:
    """(sender_key, recipient_key) for one Telegram message."""
    return (
        f"{HUG_REASON}:{message_key}:{sender_id}",
        f"{HUG_RECEIVED_REASON}:{message_key}:{target_id}",
    )


def format_hug_cooldown_message(remaining_seconds: float) -> str:
    """Friendly cooldown text in the Sib voice (mirrors the claim formatter)."""
    remaining = max(1, int(math.ceil(remaining_seconds)))
    if remaining < 60:
        wait = f"{fa_digits(remaining)} ثانیه"
    else:
        minutes, seconds = divmod(remaining, 60)
        wait = f"{fa_digits(minutes)} دقیقه"
        if seconds:
            wait += f" و {fa_digits(seconds)} ثانیه"
    return f"🤗 فقط **{wait}** صبر کن؛ بعدش می‌تونی دوباره بغل سیبی بدی!"


def pick_hug_message(
    sender_mention: str,
    target_mention: str,
    amount: int,
    rng: Optional[random.Random] = None,
    *,
    friends: bool = False,
) -> str:
    """One randomly chosen hug variant, filled with the two mentions.

    ``friends=True`` picks from the Friendship Bonus pool, which names the
    friendship in the sentence. Both pools print ``amount`` in Persian digits,
    so the narrative always matches what the ledger actually paid.
    """
    chooser = rng or random
    pool = HUG_FRIEND_MESSAGE_VARIANTS if friends else HUG_MESSAGE_VARIANTS
    template = chooser.choice(pool)
    return template.format(
        a=sender_mention,
        b=target_mention,
        n=fa_digits(amount),
    )


class SibHugService:
    """Validate, enforce the sender cooldown, and award both participants."""

    @classmethod
    async def hug(
        cls,
        session: AsyncSession,
        *,
        sender_id: int,
        target_id: int,
        message_key: str,
    ) -> HugResult:
        """Run one hug inside the caller's transaction.

        Returns ``HUG_AWARDED`` (both rewards staged in ``session``),
        ``HUG_COOLDOWN`` (nothing written), or ``HUG_DUPLICATE`` (this
        message was already processed; nothing written).

        On ``HUG_AWARDED``, ``amount`` is what EACH participant received and
        ``friendship_bonus`` says whether it includes the Friendship Bonus.
        """
        if sender_id == target_id:
            raise SibHugSelfTargetError("a user cannot hug themselves")

        amount = await ConfigService.get_int(session, HUG_AMOUNT_KEY)
        cooldown = await ConfigService.get_int(session, HUG_COOLDOWN_KEY)

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
            raise SibHugInvalidTargetError("participant user row is missing")
        if found[target_id]:
            raise SibHugInvalidTargetError("bots cannot receive a hug")

        # 2) Duplicate check (locking read: sees the latest committed hug).
        sender_key, recipient_key = hug_idempotency_keys(message_key, sender_id, target_id)
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
                return HugResult(status=HUG_DUPLICATE)
            # Half a pair can only come from corruption or a bypassed path.
            raise SibHugIntegrityError("partial hug ledger state for message")

        # 3) Sender cooldown (locking read; DB clock on both sides).
        last = (
            await session.execute(
                select(AppleTransaction.created_at)
                .where(AppleTransaction.user_id == sender_id)
                .where(AppleTransaction.reason == HUG_REASON)
                .order_by(AppleTransaction.created_at.desc(), AppleTransaction.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).first()
        if last is not None:
            db_now = await session.scalar(select(func.now()))
            elapsed = (db_now - last.created_at).total_seconds()
            if elapsed < cooldown:
                return HugResult(
                    status=HUG_COOLDOWN,
                    remaining_seconds=cooldown - elapsed,
                )

        # 4) Friendship Bonus (SOC1): size the reward now that the hug is
        #    known to go through. It runs AFTER the duplicate and cooldown
        #    gates, so the bonus can never turn a blocked hug into a paying
        #    one, and it never touches the idempotency keys or the cooldown
        #    row - a pair of friends is still capped at one hug per window.
        #    Both sides always get the SAME amount.
        friends = await FriendshipService.are_friends(session, sender_id, target_id)
        bonus = (
            await ConfigService.get_int(session, HUG_FRIEND_BONUS_KEY) if friends else 0
        )
        amount = amount + max(0, bonus)

        # 5) Both rewards in this transaction. Wallet rows are written in
        #    ascending user-id order, matching the user-row lock order, so a
        #    concurrent writer that locks the same two wallets cannot invert it.
        #    The earns are keyed, and the checks above ran under the same locks,
        #    so neither can collide.
        rewards = [
            (sender_id, HUG_REASON, target_id, sender_key),
            (target_id, HUG_RECEIVED_REASON, sender_id, recipient_key),
        ]
        for user_id, reason, ref_id, key in sorted(rewards, key=lambda r: r[0]):
            await AppleWalletService.earn(
                session,
                user_id=user_id,
                amount=amount,
                reason=reason,
                ref_type=HUG_REF_TYPE,
                ref_id=ref_id,
                idempotency_key=key,
            )

        # 6) Verify both expected ledger records exist inside this transaction.
        #    The pre-check above proved neither key existed before, so any key
        #    found now was written by this call. A missing key means a reward
        #    was skipped (earn() returns silently on a duplicate key). Raising
        #    here propagates out of get_session(), which rolls back BOTH rewards.
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
            raise SibHugIntegrityError(
                f"hug reward verification failed: missing {sorted({sender_key, recipient_key} - written)}"
            )
        return HugResult(
            status=HUG_AWARDED, amount=amount, friendship_bonus=friends
        )
