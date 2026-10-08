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
from sib.services.sib_claim import fa_digits

HUG_REASON = "sib_hug"
HUG_RECEIVED_REASON = "sib_hug_received"
HUG_REF_TYPE = "sib_hug"
HUG_SCENE_KEY = "hug"

HUG_AMOUNT_KEY = "sib_hug_amount"
HUG_COOLDOWN_KEY = "sib_hug_cooldown_seconds"

HUG_AWARDED = "awarded"
HUG_COOLDOWN = "cooldown"
HUG_DUPLICATE = "duplicate"

# At least 10 distinct variants. Placeholders: {a} sender, {b} recipient,
# {n} reward amount in Persian digits. Names are passed in already escaped
# and wrapped as Markdown mentions.
HUG_MESSAGE_VARIANTS = (
    "🤗 {a} محکم بغلِ {b} کرد! 🍎\n\nهر دو نفر {n} تا سیب جایزه گرفتین. 💞",
    "🫂 یه بغل گرم از طرف {a} تقدیم {b} شد! 🍎\n\nهر دو تا {n} سیب گرفتن. ✨",
    "🍏 {a} و {b} تو یه بغل سیبی قفل شدن! 🤗\n\nبه هر کدوم {n} تا سیب رسید. 😄",
    "🤗 بغل سیبی ثبت شد! {a} به {b} یه بغل حسابی داد.\n\nجایزه‌ی هر دو نفر: {n} سیب 🍎",
    "🫂 {b} یه بغل خیلی گرم از {a} گرفت! 💛\n\n{n} تا سیب به هر دو نفر رسید. 🍎",
    "🍎 {a} با یه بغل سیبی دلِ {b} رو گرم کرد! 🤗\n\nهر دو تا {n} سیب گرفتن. 🎁",
    "🤗 خبر خوش: {a} و {b} همدیگه رو بغل کردن! 🍏\n\nبه هر دو نفر {n} تا سیب رسید. 🥰",
    "🫂 بغلِ دوستانه‌ی {a} به {b} ثبت شد! 🍎\n\nهر دو نفر {n} سیب هدیه گرفتن. ✨",
    "🍏 بغل سیبی بین {a} و {b} برقرار شد! 🤗\n\nهرکدوم {n} تا سیب گرفتن. 💞",
    "🤗 {a} با یه بغل محکم به {b} سلام کرد! 🍎\n\nهر دو تا {n} سیب جایزه گرفتن. 🎉",
    "🫂 یه بغل سیبی کوچیک از {a} برای {b}! 🍏\n\nبرای هر دو نفر {n} تا سیب ثبت شد. 😊",
    "🍎 {b} حسابی بغل شد! {a} یه بغل سیبی حواله‌ی {b} کرد. 🤗\n\nهر دو تا {n} سیب گرفتن. 🌟",
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
    return f"🤗 هنوز زوده! {wait} دیگه می‌تونی دوباره بغل سیبی بدی. 🍎"


def pick_hug_message(
    sender_mention: str,
    target_mention: str,
    amount: int,
    rng: Optional[random.Random] = None,
) -> str:
    """One randomly chosen hug variant, filled with the two mentions."""
    chooser = rng or random
    template = chooser.choice(HUG_MESSAGE_VARIANTS)
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

        # 4) Both rewards in this transaction. Wallet rows are written in
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

        # 5) Verify both expected ledger records exist inside this transaction.
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
        return HugResult(status=HUG_AWARDED, amount=amount)
