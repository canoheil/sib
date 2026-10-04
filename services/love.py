"""Sib Love & Flower Gift domain service (Phase 5).

Responsibilities:
- Manage spouse-to-spouse Flower Gifts and the shared ``Marriage.love_score``
  metric (clamped to ``[love_min, love_max]``, default ``0..100``).
- ``send_flower``:
  - Validates ``sender != receiver``, active ``Marriage`` between them,
    configured ``flower_love_gain`` (raising ``FlowerLoveGainNotConfiguredError``
    before any consumption if ``None``), daily directional limit
    (``flower_daily_limit_per_direction``, default 1 per UTC day for ``A -> B``
    and independently 1 per UTC day for ``B -> A``), and ``flower`` inventory
    ownership.
  - Atomically consumes 1 ``flower`` at SEND time and creates a ``PENDING``
    ``FlowerGift`` record.
- ``accept_flower``:
  - Only ``receiver_id`` may accept a ``PENDING`` gift belonging to an
    ``ACTIVE`` marriage.
  - Transitions ``PENDING -> ACCEPTED`` via atomic CAS, increases shared
    ``love_score`` by ``love_delta`` (clamped to ``[love_min, love_max]``;
    succeeds even when ``love_score`` is already ``100``), and records
    ``last_flower_accepted_at`` and ``last_flower_accepted_date``.
- ``reject_flower``:
  - Only ``receiver_id`` may reject a ``PENDING`` gift belonging to an
    ``ACTIVE`` marriage.
  - Atomically transitions ``PENDING -> REJECTED``, decreases shared
    ``love_score`` by ``flower_reject_love_penalty`` (default 1, clamped at
    ``love_min``), and refunds 1 ``flower`` to the original sender.
- ``invalidate_gift`` / ``expire_gift``:
  - Atomically transitions ``PENDING -> INVALIDATED`` (or ``EXPIRED``), sets
    ``resolved_at``, refunds 1 ``flower`` to the sender, and leaves
    ``love_score`` unchanged.

Lock-order policy (see ``services/marriage.py`` for the full rationale):
Every transaction in the relationship/inventory domain acquires its row locks
in one global order::

    User (ascending id) -> Marriage -> MarriageProposal -> FlowerGift -> UserItem

Consequently ``send_flower`` and every FlowerGift resolution path lock
``User`` *before* ``Marriage``, and only lock the ``FlowerGift`` row once both
are held. Without that shared order a ``send_flower`` transaction holding
``User`` and waiting for ``Marriage`` can deadlock against an
``accept_flower`` transaction holding ``Marriage`` and waiting for ``User``
on MariaDB/MySQL InnoDB.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.logging import Events, logger, log_event
from sib.database.models import (
    FlowerGift,
    FlowerGiftStatus,
    Marriage,
    MarriageStatus,
    User,
)
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.marriage import MarriageError, MarriageNotFoundError, MarriageService
from sib.services.marriage_proposal import _as_utc, _utc_now

FLOWER_ITEM_KEY = "flower"
DEFAULT_LOVE_MIN = 0
DEFAULT_LOVE_MAX = 100
DEFAULT_REJECT_PENALTY = 1
DEFAULT_DAILY_DIRECTIONAL_LIMIT = 1
DEFAULT_DAILY_DECAY = 1
# Mirrors CONFIG_DEFAULTS["flower_gift_pending_timeout_seconds"].
DEFAULT_FLOWER_GIFT_PENDING_TIMEOUT_SECONDS = 300

# Statuses that occupy a sender's directional daily Flower slot for a UTC day.
# REJECTED, INVALIDATED, and EXPIRED refund the Flower and free the slot so the
# sender may try again later the same day.
_QUOTA_COUNTING_STATUSES = (
    FlowerGiftStatus.PENDING,
    FlowerGiftStatus.ACCEPTED,
)


# ---------------------------------------------------------------------------
# Structured Domain Exceptions
# ---------------------------------------------------------------------------


class LoveServiceError(MarriageError):
    """Base exception for all Sib Love & Flower Gift domain errors."""

    code: str = "love_service_error"


class NotMarriedError(LoveServiceError, MarriageNotFoundError):
    """Raised when an operation requires an ACTIVE marriage but the user has none."""

    code = "not_married"

    def __init__(
        self,
        *,
        user_id: int,
        message: Optional[str] = None,
    ) -> None:
        self.user_id = int(user_id)
        LoveServiceError.__init__(
            self,
            message or f"User {user_id} does not have an active marriage.",
            code=self.code,
        )


class NotSpouseError(LoveServiceError):
    """Raised when the receiver is not the sender's current active spouse."""

    code = "not_spouse"

    def __init__(
        self,
        *,
        sender_id: int,
        receiver_id: int,
        actual_spouse_id: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        self.sender_id = int(sender_id)
        self.receiver_id = int(receiver_id)
        self.actual_spouse_id = (
            int(actual_spouse_id) if actual_spouse_id is not None else None
        )
        super().__init__(
            message
            or f"User {receiver_id} is not the active spouse of user {sender_id}."
        )


class SelfFlowerGiftError(NotSpouseError):
    """Raised when a user attempts to send a Flower to themselves."""

    code = "self_flower_gift"

    def __init__(self, *, sender_id: int, receiver_id: int) -> None:
        super().__init__(
            sender_id=sender_id,
            receiver_id=receiver_id,
            actual_spouse_id=None,
            message="A user cannot send a flower to themselves.",
        )


class FlowerRequiredError(LoveServiceError):
    """Raised when the sender does not own at least 1 Flower item."""

    code = "flower_required"

    def __init__(
        self,
        *,
        user_id: int,
        message: Optional[str] = None,
    ) -> None:
        self.user_id = int(user_id)
        super().__init__(
            message or f"User {user_id} does not own a flower item."
        )


class FlowerLoveGainNotConfiguredError(LoveServiceError):
    """Raised when ``flower_love_gain`` is unset (``None``) or invalid in config."""

    code = "flower_love_gain_not_configured"

    def __init__(
        self,
        message: str = "Configuration key 'flower_love_gain' is not configured.",
    ) -> None:
        super().__init__(message)


class FlowerDailyLimitError(LoveServiceError):
    """Raised when the sender has already used their directional daily Flower allowance."""

    code = "flower_daily_limit_reached"

    def __init__(
        self,
        *,
        marriage_id: int,
        sender_id: int,
        receiver_id: int,
        gift_date: str,
        limit: int = DEFAULT_DAILY_DIRECTIONAL_LIMIT,
        message: Optional[str] = None,
    ) -> None:
        self.marriage_id = int(marriage_id)
        self.sender_id = int(sender_id)
        self.receiver_id = int(receiver_id)
        self.gift_date = gift_date
        self.limit = int(limit)
        super().__init__(
            message
            or (
                f"Daily directional flower limit ({limit}) reached for "
                f"{sender_id} -> {receiver_id} on {gift_date}."
            )
        )


class FlowerGiftNotFoundError(LoveServiceError):
    """Raised when a requested FlowerGift record does not exist."""

    code = "flower_gift_not_found"

    def __init__(
        self,
        *,
        gift_id: int,
        message: Optional[str] = None,
    ) -> None:
        self.gift_id = int(gift_id)
        super().__init__(message or f"FlowerGift {gift_id} not found.")


class FlowerGiftNotAcceptableError(LoveServiceError):
    """Base exception for FlowerGift resolution refusals."""

    code = "flower_gift_not_acceptable"

    def __init__(
        self,
        *,
        gift_id: Optional[int] = None,
        current_status: Optional[str] = None,
        reason: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        self.gift_id = int(gift_id) if gift_id is not None else None
        self.current_status = current_status
        self.reason = reason
        super().__init__(
            message
            or f"FlowerGift {gift_id} cannot be resolved (status={current_status})."
        )


class NotFlowerGiftReceiverError(FlowerGiftNotAcceptableError):
    """Raised when a user other than ``receiver_id`` attempts to accept or reject a FlowerGift."""

    code = "not_flower_gift_receiver"

    def __init__(
        self,
        *,
        gift_id: int,
        actor_user_id: int,
        receiver_id: int,
        message: Optional[str] = None,
    ) -> None:
        self.actor_user_id = int(actor_user_id)
        self.receiver_id = int(receiver_id)
        super().__init__(
            gift_id=gift_id,
            reason="not_receiver",
            message=message
            or (
                f"User {actor_user_id} is not the receiver ({receiver_id}) "
                f"of FlowerGift {gift_id}."
            ),
        )


class FlowerGiftAlreadyResolvedError(FlowerGiftNotAcceptableError):
    """Raised when a FlowerGift is no longer PENDING."""

    code = "flower_gift_already_resolved"

    def __init__(
        self,
        *,
        gift_id: int,
        current_status: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        super().__init__(
            gift_id=gift_id,
            current_status=current_status,
            reason="already_resolved",
            message=message
            or f"FlowerGift {gift_id} is already resolved (status={current_status}).",
        )


class FlowerGiftMarriageInactiveError(FlowerGiftNotAcceptableError):
    """Raised when a FlowerGift's marriage is no longer ACTIVE at resolution time."""

    code = "flower_gift_marriage_inactive"

    def __init__(
        self,
        *,
        gift_id: int,
        marriage_id: int,
        marriage_status: Optional[str] = None,
        flower_refunded: bool = False,
        message: Optional[str] = None,
    ) -> None:
        self.marriage_id = int(marriage_id)
        self.marriage_status = marriage_status
        self.flower_refunded = flower_refunded
        super().__init__(
            gift_id=gift_id,
            current_status=FlowerGiftStatus.INVALIDATED if flower_refunded else None,
            reason="marriage_inactive",
            message=message
            or (
                f"Marriage {marriage_id} is no longer ACTIVE "
                f"(status={marriage_status}) for FlowerGift {gift_id}."
            ),
        )


# ---------------------------------------------------------------------------
# Structured Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowerGiftResult:
    """Structured domain result for Flower send, accept, reject, invalidate, and expire operations."""

    gift: FlowerGift
    marriage: Marriage
    sender: Optional[User]
    receiver: Optional[User]
    previous_love: int
    new_love: int
    love_delta: int
    flower_consumed: bool = False
    flower_refunded: bool = False

    @property
    def flower_gift(self) -> FlowerGift:
        return self.gift

    @property
    def id(self) -> int:
        return self.gift.id

    @property
    def gift_id(self) -> int:
        return self.gift.id

    @property
    def marriage_id(self) -> int:
        return self.marriage.id

    @property
    def sender_id(self) -> int:
        return self.gift.sender_id

    @property
    def receiver_id(self) -> int:
        return self.gift.receiver_id

    @property
    def chat_id(self) -> Optional[int]:
        return self.gift.chat_id

    @property
    def message_id(self) -> Optional[int]:
        return self.gift.message_id

    @property
    def gift_date(self) -> str:
        return self.gift.gift_date

    @property
    def status(self) -> str:
        return self.gift.status

    @property
    def resolved_at(self) -> Optional[datetime]:
        return self.gift.resolved_at

    @property
    def love_score(self) -> int:
        return self.new_love

    @property
    def refunded(self) -> bool:
        return self.flower_refunded


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class LoveService:
    """Domain service owning FlowerGift lifecycle and shared Marriage Love score updates."""

    _LOCK_RETRIES = 5
    _LOCK_RETRY_DELAY = 0.03

    # ------------------------------------------------------------------
    # 1. Send Flower
    # ------------------------------------------------------------------
    @classmethod
    async def send_flower(
        cls,
        session: AsyncSession,
        sender_user_id: int,
        receiver_user_id: int,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        *,
        now: Optional[datetime] = None,
    ) -> FlowerGiftResult:
        """Consume 1 ``flower`` and create a ``PENDING`` ``FlowerGift`` to the sender's spouse."""
        sender_id = int(sender_user_id)
        receiver_id = int(receiver_user_id)
        if chat_id is not None:
            chat_id = int(chat_id)
        if message_id is not None:
            message_id = int(message_id)

        if sender_id == receiver_id:
            raise SelfFlowerGiftError(sender_id=sender_id, receiver_id=receiver_id)

        current_time = _utc_now(now)
        gift_date = current_time.strftime("%Y-%m-%d")

        # Validate flower_love_gain configuration BEFORE locking/consuming
        love_gain = await cls._get_configured_flower_love_gain(session)

        # Lock users deterministically in ascending ID order, then the
        # marriage (global lock order: User -> Marriage -> ... -> UserItem).
        users = await MarriageService._lock_users_ascending(
            session, sender_id, receiver_id
        )

        # Find and lock sender's active marriage
        marriage = await cls._lock_active_marriage_for_user(session, sender_id)
        if marriage is None or marriage.status != MarriageStatus.ACTIVE:
            raise NotMarriedError(user_id=sender_id)

        actual_spouse_id = marriage.spouse_of(sender_id)
        if actual_spouse_id != receiver_id:
            raise NotSpouseError(
                sender_id=sender_id,
                receiver_id=receiver_id,
                actual_spouse_id=actual_spouse_id,
            )

        daily_limit = await cls._get_daily_directional_limit(session)

        async with session.begin_nested():
            await session.refresh(marriage)
            if marriage.status != MarriageStatus.ACTIVE:
                raise NotMarriedError(user_id=sender_id)

            # Enforce directional daily limit (A -> B is independent of B -> A).
            # This is a LOCKING read (see ``_count_directional_gifts_for_date``)
            # and now fully serializes concurrent senders, which is why the old
            # post-flush re-count is no longer needed: no other transaction can
            # insert a quota-consuming gift for this (marriage, date, sender)
            # between this read and our own insert.
            used_today = await cls._count_directional_gifts_for_date(
                session,
                marriage_id=marriage.id,
                sender_id=sender_id,
                gift_date=gift_date,
                for_update=True,
            )
            if used_today >= daily_limit:
                raise FlowerDailyLimitError(
                    marriage_id=marriage.id,
                    sender_id=sender_id,
                    receiver_id=receiver_id,
                    gift_date=gift_date,
                    limit=daily_limit,
                )

            # Verify and consume 1 Flower under inventory row lock.
            # Inventory is the last resource in the global lock order, so all
            # FlowerGift locks (the quota read above) are taken first.
            have_flowers = await ItemService.get_total(
                session, sender_id, FLOWER_ITEM_KEY, for_update=True
            )
            if have_flowers < 1:
                raise FlowerRequiredError(user_id=sender_id)

            await cls._consume_flower(session, sender_id=sender_id)

            gift = await cls._create_flower_gift_record(
                session,
                marriage_id=marriage.id,
                sender_id=sender_id,
                receiver_id=receiver_id,
                chat_id=chat_id,
                message_id=message_id,
                gift_date=gift_date,
                love_delta=love_gain,
            )

        await session.flush()
        loaded_gift = await cls.get_gift(session, gift.id)
        loaded_marriage = await MarriageService.get_marriage(session, marriage.id)
        assert loaded_gift is not None
        assert loaded_marriage is not None

        current_love = int(loaded_marriage.love_score)
        log_event(
            Events.LOVE_EVENT,
            {
                "action": "flower_sent",
                "gift_id": loaded_gift.id,
                "marriage_id": loaded_marriage.id,
                "sender_id": sender_id,
                "receiver_id": receiver_id,
                "gift_date": gift_date,
                "love_delta": love_gain,
            },
        )
        return FlowerGiftResult(
            gift=loaded_gift,
            marriage=loaded_marriage,
            sender=users.get(sender_id),
            receiver=users.get(receiver_id),
            previous_love=current_love,
            new_love=current_love,
            love_delta=love_gain,
            flower_consumed=True,
            flower_refunded=False,
        )

    # ------------------------------------------------------------------
    # 2. Accept Flower
    # ------------------------------------------------------------------
    @classmethod
    async def accept_flower(
        cls,
        session: AsyncSession,
        gift_id: int,
        actor_user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> FlowerGiftResult:
        """Accept a ``PENDING`` FlowerGift and increase shared ``Marriage.love_score``."""
        gift_id = int(gift_id)
        actor_id = int(actor_user_id)
        current_time = _utc_now(now)

        # Lock every row this transaction touches in the global lock order
        # (User -> Marriage -> FlowerGift) and re-verify the gift under lock.
        gift, marriage, users = await cls._lock_gift_resolution_context(
            session, gift_id, actor_user_id=actor_id
        )

        # Verify Marriage is still ACTIVE and participants are still spouses
        if not cls._is_marriage_valid_for_gift(marriage, gift):
            refunded = await cls._invalidate_and_refund_if_pending(
                session, gift, current_time
            )
            raise FlowerGiftMarriageInactiveError(
                gift_id=gift.id,
                marriage_id=gift.marriage_id,
                marriage_status=marriage.status if marriage else None,
                flower_refunded=refunded,
            )

        love_min, love_max = await cls._get_love_bounds(session)

        async with session.begin_nested():
            await session.refresh(marriage)
            if not cls._is_marriage_valid_for_gift(marriage, gift):
                raise FlowerGiftMarriageInactiveError(
                    gift_id=gift.id,
                    marriage_id=gift.marriage_id,
                    marriage_status=marriage.status if marriage else None,
                    flower_refunded=False,
                )

            configured_gain = int(gift.love_delta)
            if configured_gain <= 0:
                configured_gain = await cls._get_configured_flower_love_gain(session)

            previous_love = int(marriage.love_score)
            new_love = max(love_min, min(love_max, previous_love + configured_gain))
            applied_delta = new_love - previous_love

            # Atomic CAS: PENDING -> ACCEPTED
            claimed = await session.execute(
                update(FlowerGift)
                .where(
                    FlowerGift.id == gift.id,
                    FlowerGift.receiver_id == actor_id,
                    FlowerGift.status == FlowerGiftStatus.PENDING,
                )
                .values(
                    status=FlowerGiftStatus.ACCEPTED,
                    love_delta=configured_gain,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if (claimed.rowcount or 0) != 1:
                refreshed = await cls.get_gift(session, gift.id)
                raise FlowerGiftAlreadyResolvedError(
                    gift_id=gift.id,
                    current_status=refreshed.status if refreshed else None,
                )

            marriage.love_score = new_love
            marriage.last_flower_accepted_at = current_time
            marriage.last_flower_accepted_date = current_time.strftime("%Y-%m-%d")
            await session.flush()

        await session.flush()
        loaded_gift = await cls.get_gift(session, gift.id)
        loaded_marriage = await MarriageService.get_marriage(session, marriage.id)
        assert loaded_gift is not None
        assert loaded_marriage is not None

        log_event(
            Events.LOVE_EVENT,
            {
                "action": "flower_accepted",
                "gift_id": loaded_gift.id,
                "marriage_id": loaded_marriage.id,
                "sender_id": loaded_gift.sender_id,
                "receiver_id": loaded_gift.receiver_id,
                "previous_love": previous_love,
                "new_love": new_love,
                "applied_delta": applied_delta,
            },
        )
        return FlowerGiftResult(
            gift=loaded_gift,
            marriage=loaded_marriage,
            sender=users.get(loaded_gift.sender_id),
            receiver=users.get(loaded_gift.receiver_id),
            previous_love=previous_love,
            new_love=new_love,
            love_delta=applied_delta,
            flower_consumed=False,
            flower_refunded=False,
        )

    # ------------------------------------------------------------------
    # 3. Reject Flower
    # ------------------------------------------------------------------
    @classmethod
    async def reject_flower(
        cls,
        session: AsyncSession,
        gift_id: int,
        actor_user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> FlowerGiftResult:
        """Reject a ``PENDING`` FlowerGift, decrease shared Love by penalty, and refund 1 Flower."""
        gift_id = int(gift_id)
        actor_id = int(actor_user_id)
        current_time = _utc_now(now)

        # Lock every row this transaction touches in the global lock order
        # (User -> Marriage -> FlowerGift) and re-verify the gift under lock.
        gift, marriage, users = await cls._lock_gift_resolution_context(
            session, gift_id, actor_user_id=actor_id
        )

        # Verify Marriage is still ACTIVE; if inactive, invalidate & refund without Love penalty
        if not cls._is_marriage_valid_for_gift(marriage, gift):
            refunded = await cls._invalidate_and_refund_if_pending(
                session, gift, current_time
            )
            raise FlowerGiftMarriageInactiveError(
                gift_id=gift.id,
                marriage_id=gift.marriage_id,
                marriage_status=marriage.status if marriage else None,
                flower_refunded=refunded,
            )

        love_min, love_max = await cls._get_love_bounds(session)
        penalty = await cls._get_reject_penalty(session)

        async with session.begin_nested():
            await session.refresh(marriage)
            if not cls._is_marriage_valid_for_gift(marriage, gift):
                raise FlowerGiftMarriageInactiveError(
                    gift_id=gift.id,
                    marriage_id=gift.marriage_id,
                    marriage_status=marriage.status if marriage else None,
                    flower_refunded=False,
                )

            previous_love = int(marriage.love_score)
            new_love = max(love_min, min(love_max, previous_love - penalty))
            applied_delta = new_love - previous_love

            # 1. Atomic CAS transition: PENDING -> REJECTED
            claimed = await session.execute(
                update(FlowerGift)
                .where(
                    FlowerGift.id == gift.id,
                    FlowerGift.receiver_id == actor_id,
                    FlowerGift.status == FlowerGiftStatus.PENDING,
                )
                .values(
                    status=FlowerGiftStatus.REJECTED,
                    love_delta=-penalty,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if (claimed.rowcount or 0) != 1:
                refreshed = await cls.get_gift(session, gift.id)
                raise FlowerGiftAlreadyResolvedError(
                    gift_id=gift.id,
                    current_status=refreshed.status if refreshed else None,
                )

            # 2. Decrease Love on the shared Marriage record
            marriage.love_score = new_love

            # 3. Refund 1 Flower to the original sender
            await cls._refund_flower(session, sender_id=gift.sender_id)
            await session.flush()

        await session.flush()
        loaded_gift = await cls.get_gift(session, gift.id)
        loaded_marriage = await MarriageService.get_marriage(session, marriage.id)
        assert loaded_gift is not None
        assert loaded_marriage is not None

        log_event(
            Events.LOVE_EVENT,
            {
                "action": "flower_rejected",
                "gift_id": loaded_gift.id,
                "marriage_id": loaded_marriage.id,
                "sender_id": loaded_gift.sender_id,
                "receiver_id": loaded_gift.receiver_id,
                "previous_love": previous_love,
                "new_love": new_love,
                "applied_delta": applied_delta,
                "flower_refunded": True,
            },
        )
        return FlowerGiftResult(
            gift=loaded_gift,
            marriage=loaded_marriage,
            sender=users.get(loaded_gift.sender_id),
            receiver=users.get(loaded_gift.receiver_id),
            previous_love=previous_love,
            new_love=new_love,
            love_delta=applied_delta,
            flower_consumed=False,
            flower_refunded=True,
        )

    # ------------------------------------------------------------------
    # 4. Invalidation & Expiration Preparation
    # ------------------------------------------------------------------
    @classmethod
    async def invalidate_gift(
        cls,
        session: AsyncSession,
        gift_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> FlowerGiftResult:
        """Transition a ``PENDING`` FlowerGift to ``INVALIDATED`` and refund 1 Flower.

        ``Marriage.love_score`` is NOT modified.
        """
        return await cls._transition_pending_gift_with_refund(
            session,
            gift_id=gift_id,
            target_status=FlowerGiftStatus.INVALIDATED,
            action_label="flower_invalidated",
            now=now,
        )

    # Alias for naming flexibility
    invalidate_flower = invalidate_gift

    @classmethod
    async def expire_gift(
        cls,
        session: AsyncSession,
        gift_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> FlowerGiftResult:
        """Terminal transition ``PENDING -> EXPIRED`` with 1 Flower refund.

        The transition itself is duration-agnostic: ``expire_overdue_gifts``
        decides *which* gifts are overdue from
        ``flower_gift_pending_timeout_seconds`` and calls this once per gift.
        Refunds exactly one Flower, leaves Love untouched, and refuses to
        touch a gift that is no longer ``PENDING``.
        """
        return await cls._transition_pending_gift_with_refund(
            session,
            gift_id=gift_id,
            target_status=FlowerGiftStatus.EXPIRED,
            action_label="flower_expired",
            now=now,
        )

    # Alias for naming flexibility
    expire_flower = expire_gift

    # ------------------------------------------------------------------
    # Pending-gift expiration (maintenance / recovery)
    # ------------------------------------------------------------------

    @staticmethod
    async def _get_pending_gift_timeout_seconds(session: AsyncSession) -> int:
        """Configured ``flower_gift_pending_timeout_seconds``.

        Never hard-coded here. A non-positive value falls back to the
        documented default, matching how ``create_proposal`` treats a
        non-positive ``marriage_proposal_timeout_seconds``.
        """
        raw = await ConfigService.get(session, "flower_gift_pending_timeout_seconds")
        if raw is None:
            return DEFAULT_FLOWER_GIFT_PENDING_TIMEOUT_SECONDS
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_FLOWER_GIFT_PENDING_TIMEOUT_SECONDS
        return seconds if seconds > 0 else DEFAULT_FLOWER_GIFT_PENDING_TIMEOUT_SECONDS

    @classmethod
    async def expire_overdue_gifts(
        cls,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
    ) -> List[FlowerGiftResult]:
        """Expire every PENDING FlowerGift that has outlived its lifetime.

        A gift is overdue when ``created_at + flower_gift_pending_timeout_seconds
        <= now``. The expiration instant is DERIVED from the existing
        ``created_at`` column, so no new column and no migration is needed.

        Idempotent and race-safe:

        * Only ``PENDING`` rows are selected, so ACCEPTED / REJECTED /
          INVALIDATED / EXPIRED gifts are never revisited.
        * Each gift is expired through ``expire_gift``, whose conditional
          ``UPDATE ... WHERE status = 'PENDING'`` means a concurrent
          accept/reject/invalidate wins the race instead of double-refunding.
          Such a gift raises ``FlowerGiftAlreadyResolvedError`` and is skipped
          - it was resolved by someone else, which is a valid outcome.
        * Each gift is processed inside its own savepoint, so one failing gift
          cannot roll back the ones already expired.
        * Candidates are walked in ascending id order (deterministic).

        Every expiration refunds exactly one Flower and leaves Love
        unchanged. Returns the results of the gifts this call expired.
        """
        current_time = _utc_now(now)
        timeout_seconds = await cls._get_pending_gift_timeout_seconds(session)
        cutoff = current_time - timedelta(seconds=timeout_seconds)

        rows = list(
            (
                await session.execute(
                    select(FlowerGift.id, FlowerGift.created_at).where(
                        FlowerGift.status == FlowerGiftStatus.PENDING
                    )
                )
            ).all()
        )

        # ``created_at`` is timezone-aware on MariaDB and naive on SQLite, so
        # every value is normalised before the comparison.
        overdue_ids = sorted(
            int(row.id)
            for row in rows
            if row.created_at is not None and _as_utc(row.created_at) <= cutoff
        )
        if not overdue_ids:
            return []

        expired: List[FlowerGiftResult] = []
        for gift_id in overdue_ids:
            try:
                result = await cls.expire_gift(session, gift_id, now=current_time)
            except FlowerGiftAlreadyResolvedError:
                # A concurrent accept/reject/invalidate got there first: the
                # gift is resolved and its Flower was already handled once.
                continue
            except FlowerGiftNotFoundError:  # pragma: no cover - defensive
                continue
            except Exception as exc:
                # One poisoned gift must not strand the others. Each gift runs
                # in its own savepoint, so the gifts already expired stay
                # expired; this one is simply retried on the next sweep.
                logger.warning(f"Could not expire FlowerGift {gift_id}: {exc}")
                continue
            expired.append(result)

        if expired:
            log_event(
                Events.LOVE_EVENT,
                {
                    "action": "flower_expired_sweep",
                    "count": len(expired),
                    "gift_ids": [int(r.gift_id) for r in expired],
                },
            )
        return expired

    @classmethod
    async def invalidate_pending_gifts_for_marriage(
        cls,
        session: AsyncSession,
        marriage_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> List[FlowerGiftResult]:
        """Invalidate all ``PENDING`` FlowerGifts for ``marriage_id`` and refund each Flower."""
        pending = await cls.get_pending_gifts_for_marriage(session, marriage_id)
        results: List[FlowerGiftResult] = []
        for g in pending:
            try:
                res = await cls.invalidate_gift(session, g.id, now=now)
                results.append(res)
            except FlowerGiftAlreadyResolvedError:
                continue
        return results

    # ------------------------------------------------------------------
    # 5. Queries
    # ------------------------------------------------------------------
    @classmethod
    async def get_gift(
        cls,
        session: AsyncSession,
        gift_id: int,
        *,
        for_update: bool = False,
    ) -> Optional[FlowerGift]:
        """Fetch a ``FlowerGift`` by ID with eager-loaded participants and marriage."""
        stmt = (
            select(FlowerGift)
            .where(FlowerGift.id == int(gift_id))
            .execution_options(populate_existing=True)
            .options(
                selectinload(FlowerGift.sender),
                selectinload(FlowerGift.receiver),
                selectinload(FlowerGift.marriage),
            )
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    @classmethod
    async def get_pending_gift(
        cls,
        session: AsyncSession,
        gift_id: int,
    ) -> Optional[FlowerGift]:
        """Return the ``FlowerGift`` if and only if it is currently ``PENDING``."""
        gift = await cls.get_gift(session, gift_id)
        if gift is None or gift.status != FlowerGiftStatus.PENDING:
            return None
        return gift

    @classmethod
    async def get_pending_gifts_for_marriage(
        cls,
        session: AsyncSession,
        marriage_id: int,
    ) -> List[FlowerGift]:
        """Return all ``PENDING`` ``FlowerGift`` rows for a marriage."""
        stmt = (
            select(FlowerGift)
            .where(
                FlowerGift.marriage_id == int(marriage_id),
                FlowerGift.status == FlowerGiftStatus.PENDING,
            )
            .options(
                selectinload(FlowerGift.sender),
                selectinload(FlowerGift.receiver),
                selectinload(FlowerGift.marriage),
            )
            .order_by(FlowerGift.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    @classmethod
    async def can_send_flower_today(
        cls,
        session: AsyncSession,
        sender_user_id: int,
        receiver_user_id: Optional[int] = None,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        """True if ``sender_user_id`` has an active marriage and has not yet
        used their directional daily Flower quota for today (UTC).
        """
        sender_id = int(sender_user_id)
        marriage = await MarriageService.get_active_marriage_for_user(
            session, sender_id
        )
        if marriage is None:
            return False
        spouse_id = marriage.spouse_of(sender_id)
        if spouse_id is None:
            return False
        if receiver_user_id is not None and int(receiver_user_id) != spouse_id:
            return False

        gift_date = _utc_now(now).strftime("%Y-%m-%d")
        daily_limit = await cls._get_daily_directional_limit(session)
        used = await cls._count_directional_gifts_for_date(
            session,
            marriage_id=marriage.id,
            sender_id=sender_id,
            gift_date=gift_date,
        )
        return used < daily_limit

    @classmethod
    async def get_love_score(
        cls, session: AsyncSession, user_id: int
    ) -> Optional[int]:
        """Return the shared ``love_score`` for ``user_id``'s active marriage, or ``None``."""
        marriage = await MarriageService.get_active_marriage_for_user(session, user_id)
        if marriage is None:
            return None
        return int(marriage.love_score)

    # ------------------------------------------------------------------
    # Daily Love decay (maintenance / recovery)
    # ------------------------------------------------------------------

    @classmethod
    async def apply_daily_decay(
        cls,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """Decay the shared Love of every ACTIVE marriage by the configured
        ``love_daily_decay`` per elapsed UTC day.

        Called by ``MaintenanceService`` (periodic) and ``RecoveryService``
        (startup catch-up). Both are idempotent because the anchor is the
        existing ``marriages.last_decay_date`` column:

        * A second run on the same UTC day computes ``days_elapsed == 0``
          and touches nothing, so the same day can never decay twice.
        * A bot that was offline for several days decays once per elapsed
          calendar day in a single run instead of losing that history.
        * Love is clamped to the configured ``love_min`` - never below it.

        Only ACTIVE marriages are touched; divorced ones keep their frozen
        ``love_score`` (divorce already resets it). Returns the number of
        marriages whose Love actually changed.
        """
        from sqlalchemy import select as _select

        from sib.services.marriage import MarriageService as _MarriageService

        current_time = _utc_now(now)
        today = current_time.strftime("%Y-%m-%d")
        today_date = current_time.date()

        decay_per_day = await cls._get_daily_decay_amount(session)
        if decay_per_day <= 0:
            return 0
        love_min, love_max = await cls._get_love_bounds(session)

        # Candidates only: a non-locking read. Every row is re-locked below
        # before it is written, so a marriage that ends mid-sweep is skipped.
        candidates = list(
            (
                await session.execute(
                    _select(
                        Marriage.id, Marriage.user1_id, Marriage.user2_id
                    ).where(
                        Marriage.status == MarriageStatus.ACTIVE,
                        or_(
                            Marriage.last_decay_date.is_(None),
                            Marriage.last_decay_date < today,
                        ),
                    )
                )
            ).all()
        )

        changed = 0
        # Ascending id: deterministic order, so two concurrent sweeps can
        # never deadlock against each other.
        for marriage_id, user1_id, user2_id in sorted(
            (int(c[0]), int(c[1]), int(c[2])) for c in candidates
        ):
            # Global relationship lock order: User (ascending) -> Marriage.
            await _MarriageService._lock_users_ascending(session, user1_id, user2_id)
            marriage = await cls._lock_marriage(session, marriage_id)
            if marriage is None or marriage.status != MarriageStatus.ACTIVE:
                continue

            anchor_date = cls._decay_anchor_date(marriage, current_time)
            days_elapsed = (today_date - anchor_date).days
            if days_elapsed <= 0:
                # Same UTC day as the anchor: nothing to decay. This is what
                # makes a repeated run a no-op.
                continue

            current_score = int(marriage.love_score)
            target_score = max(love_min, current_score - (decay_per_day * days_elapsed))
            target_score = min(target_score, love_max)
            if target_score == current_score:
                # Already at the floor - still stamp the day so we stop
                # re-examining this marriage every maintenance cycle.
                marriage.last_decay_date = today
                await session.flush()
                continue

            marriage.love_score = target_score
            marriage.last_decay_date = today
            await session.flush()
            changed += 1

            log_event(
                Events.LOVE_EVENT,
                {
                    "action": "daily_decay",
                    "marriage_id": marriage_id,
                    "days_elapsed": days_elapsed,
                    "decay_per_day": decay_per_day,
                    "love_score": target_score,
                },
            )

        if changed:
            log_event(
                Events.LOVE_EVENT,
                {"action": "daily_decay_sweep", "count": changed, "date": today},
            )
        return changed

    @staticmethod
    def _decay_anchor_date(marriage: Marriage, current_time: datetime):
        """The UTC calendar day from which missed decay days are counted.

        ``last_decay_date`` when the marriage has decayed before, otherwise
        the wedding day. A corrupt/unparseable value degrades to the wedding
        day so a bad row can never cause an unbounded catch-up.
        """
        anchor = getattr(marriage, "last_decay_date", None)
        if anchor:
            try:
                return datetime.strptime(str(anchor), "%Y-%m-%d").date()
            except (TypeError, ValueError):
                pass
        married_at = getattr(marriage, "married_at", None)
        if married_at is not None:
            return _as_utc(married_at).date()
        return current_time.date()

    @staticmethod
    async def _get_daily_decay_amount(session: AsyncSession) -> int:
        """Configured ``love_daily_decay``, clamped to a non-negative int."""
        raw = await ConfigService.get(session, "love_daily_decay")
        if raw is None:
            return DEFAULT_DAILY_DECAY
        try:
            amount = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_DAILY_DECAY
        return max(0, amount)

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------
    @classmethod
    async def _transition_pending_gift_with_refund(
        cls,
        session: AsyncSession,
        *,
        gift_id: int,
        target_status: str,
        action_label: str,
        now: Optional[datetime] = None,
    ) -> FlowerGiftResult:
        current_time = _utc_now(now)

        # Lock every row this transaction touches in the global lock order
        # (User -> Marriage -> FlowerGift) and re-verify the gift under lock.
        gift, marriage, users = await cls._lock_gift_resolution_context(session, gift_id)

        async with session.begin_nested():
            claimed = await session.execute(
                update(FlowerGift)
                .where(
                    FlowerGift.id == gift.id,
                    FlowerGift.status == FlowerGiftStatus.PENDING,
                )
                .values(
                    status=target_status,
                    love_delta=0,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if (claimed.rowcount or 0) != 1:
                refreshed = await cls.get_gift(session, gift.id)
                raise FlowerGiftAlreadyResolvedError(
                    gift_id=gift.id,
                    current_status=refreshed.status if refreshed else None,
                )

            await cls._refund_flower(session, sender_id=gift.sender_id)
            await session.flush()

        await session.flush()
        loaded_gift = await cls.get_gift(session, gift.id)
        loaded_marriage = await MarriageService.get_marriage(
            session, gift.marriage_id
        )
        assert loaded_gift is not None
        assert loaded_marriage is not None

        current_love = int(loaded_marriage.love_score)
        log_event(
            Events.LOVE_EVENT,
            {
                "action": action_label,
                "gift_id": loaded_gift.id,
                "marriage_id": loaded_marriage.id,
                "sender_id": loaded_gift.sender_id,
                "receiver_id": loaded_gift.receiver_id,
                "love_score": current_love,
                "flower_refunded": True,
            },
        )
        return FlowerGiftResult(
            gift=loaded_gift,
            marriage=loaded_marriage,
            sender=users.get(loaded_gift.sender_id),
            receiver=users.get(loaded_gift.receiver_id),
            previous_love=current_love,
            new_love=current_love,
            love_delta=0,
            flower_consumed=False,
            flower_refunded=True,
        )

    @classmethod
    async def _invalidate_and_refund_if_pending(
        cls,
        session: AsyncSession,
        gift: FlowerGift,
        current_time: datetime,
    ) -> bool:
        """Atomically transition a PENDING gift to INVALIDATED and refund 1 Flower
        when resolution discovers the marriage is no longer ACTIVE.
        """
        async with session.begin_nested():
            claimed = await session.execute(
                update(FlowerGift)
                .where(
                    FlowerGift.id == gift.id,
                    FlowerGift.status == FlowerGiftStatus.PENDING,
                )
                .values(
                    status=FlowerGiftStatus.INVALIDATED,
                    love_delta=0,
                    resolved_at=current_time,
                )
                .execution_options(synchronize_session=False)
            )
            if (claimed.rowcount or 0) != 1:
                return False
            await cls._refund_flower(session, sender_id=gift.sender_id)
            await session.flush()

        await session.flush()
        await cls.get_gift(session, gift.id)
        return True

    @staticmethod
    def _is_marriage_valid_for_gift(
        marriage: Optional[Marriage], gift: FlowerGift
    ) -> bool:
        if marriage is None or marriage.status != MarriageStatus.ACTIVE:
            return False
        return (
            marriage.includes_user(gift.sender_id)
            and marriage.includes_user(gift.receiver_id)
            and marriage.spouse_of(gift.sender_id) == gift.receiver_id
        )

    @classmethod
    async def _lock_gift_resolution_context(
        cls,
        session: AsyncSession,
        gift_id: int,
        *,
        actor_user_id: Optional[int] = None,
    ) -> Tuple[FlowerGift, Optional[Marriage], Dict[int, User]]:
        """Acquire every row lock a FlowerGift resolution needs, in the global
        lock order ``User (ascending id) -> Marriage -> FlowerGift``.

        ``send_flower`` acquires ``User -> Marriage`` too, so a resolution path
        that locked ``Marriage`` before ``User`` could deadlock against it on
        MariaDB/MySQL InnoDB. Ordering both paths identically removes that
        cycle without weakening any check:

        - The gift is read once *without* a lock only to resolve
          ``marriage_id`` / ``sender_id`` / ``receiver_id``, all of which are
          immutable once the row exists.
        - The gift row is then re-read under ``FOR UPDATE`` (after the SQLite
          write-intent barrier) and every business rule is re-verified against
          the locked version, so a concurrent resolution still loses the race.
        """
        gid = int(gift_id)
        seed = await cls.get_gift(session, gid)
        if seed is None:
            raise FlowerGiftNotFoundError(gift_id=gid)

        # ``receiver_id`` never changes, so refusing an unauthorized actor here
        # is equivalent to refusing it after the lock.
        if actor_user_id is not None and int(actor_user_id) != seed.receiver_id:
            raise NotFlowerGiftReceiverError(
                gift_id=gid,
                actor_user_id=int(actor_user_id),
                receiver_id=seed.receiver_id,
            )

        users = await MarriageService._lock_users_ascending(
            session, seed.sender_id, seed.receiver_id
        )
        marriage = await cls._lock_marriage(session, seed.marriage_id)
        gift = await cls._lock_gift_for_transition(session, gid)
        if gift is None:
            raise FlowerGiftNotFoundError(gift_id=gid)

        if actor_user_id is not None and int(actor_user_id) != gift.receiver_id:
            raise NotFlowerGiftReceiverError(
                gift_id=gift.id,
                actor_user_id=int(actor_user_id),
                receiver_id=gift.receiver_id,
            )
        if gift.status != FlowerGiftStatus.PENDING:
            raise FlowerGiftAlreadyResolvedError(
                gift_id=gift.id,
                current_status=gift.status,
            )
        return gift, marriage, users

    @staticmethod
    async def _count_directional_gifts_for_date(
        session: AsyncSession,
        *,
        marriage_id: int,
        sender_id: int,
        gift_date: str,
        for_update: bool = False,
    ) -> int:
        """Count quota-consuming gifts for ``(marriage, date, sender)``.

        ``for_update=True`` turns this into a LOCKING read. On MariaDB/MySQL,
        whose default isolation level is REPEATABLE READ, a plain consistent
        read reports the transaction's *opening* snapshot and would therefore
        miss a gift committed by a concurrent sender after that snapshot was
        taken - silently bypassing the daily directional limit. The predicate
        is covered by ``ix_flower_gifts_marriage_date_sender_status``, so the
        lock stays narrowly scoped to the couple's own gift rows and is taken
        only after the ``User`` and ``Marriage`` locks (global lock order).
        """
        stmt = select(FlowerGift.id).where(
            FlowerGift.marriage_id == int(marriage_id),
            FlowerGift.gift_date == gift_date,
            FlowerGift.sender_id == int(sender_id),
            FlowerGift.status.in_(_QUOTA_COUNTING_STATUSES),
        )
        if for_update:
            stmt = stmt.with_for_update()
        rows = (await session.scalars(stmt)).all()
        return len(rows)

    @staticmethod
    async def _get_configured_flower_love_gain(session: AsyncSession) -> int:
        raw = await ConfigService.get(session, "flower_love_gain")
        if raw is None:
            raise FlowerLoveGainNotConfiguredError()
        try:
            gain = int(raw)
        except (TypeError, ValueError) as exc:
            raise FlowerLoveGainNotConfiguredError() from exc
        if gain <= 0:
            raise FlowerLoveGainNotConfiguredError(
                "Configuration key 'flower_love_gain' must be a positive integer."
            )
        return gain

    @staticmethod
    async def _get_love_bounds(session: AsyncSession) -> tuple[int, int]:
        min_raw = await ConfigService.get(session, "love_min")
        max_raw = await ConfigService.get(session, "love_max")
        love_min = int(min_raw) if min_raw is not None else DEFAULT_LOVE_MIN
        love_max = int(max_raw) if max_raw is not None else DEFAULT_LOVE_MAX
        return love_min, love_max

    @staticmethod
    async def _get_reject_penalty(session: AsyncSession) -> int:
        raw = await ConfigService.get(session, "flower_reject_love_penalty")
        penalty = int(raw) if raw is not None else DEFAULT_REJECT_PENALTY
        return max(0, penalty)

    @staticmethod
    async def _get_daily_directional_limit(session: AsyncSession) -> int:
        raw = await ConfigService.get(session, "flower_daily_limit_per_direction")
        limit = int(raw) if raw is not None else DEFAULT_DAILY_DIRECTIONAL_LIMIT
        return max(1, limit)

    @classmethod
    async def _consume_flower(cls, session: AsyncSession, *, sender_id: int) -> int:
        try:
            return await ItemService.consume(
                session, sender_id, FLOWER_ITEM_KEY, 1
            )
        except ValueError as exc:
            raise FlowerRequiredError(user_id=sender_id) from exc

    @classmethod
    async def _refund_flower(cls, session: AsyncSession, *, sender_id: int) -> int:
        return await ItemService.grant(session, sender_id, FLOWER_ITEM_KEY, 1)

    @classmethod
    async def _create_flower_gift_record(
        cls,
        session: AsyncSession,
        *,
        marriage_id: int,
        sender_id: int,
        receiver_id: int,
        chat_id: Optional[int],
        message_id: Optional[int],
        gift_date: str,
        love_delta: int,
    ) -> FlowerGift:
        gift = FlowerGift(
            marriage_id=marriage_id,
            sender_id=sender_id,
            receiver_id=receiver_id,
            chat_id=chat_id,
            message_id=message_id,
            gift_date=gift_date,
            status=FlowerGiftStatus.PENDING,
            love_delta=love_delta,
        )
        session.add(gift)
        await session.flush()
        return gift

    @classmethod
    async def _lock_active_marriage_for_user(
        cls, session: AsyncSession, user_id: int
    ) -> Optional[Marriage]:
        uid = int(user_id)
        for attempt in range(cls._LOCK_RETRIES):
            try:
                await session.execute(
                    update(Marriage)
                    .where(
                        Marriage.status == MarriageStatus.ACTIVE,
                        or_(Marriage.user1_id == uid, Marriage.user2_id == uid),
                    )
                    .values(updated_at=func.now())
                    .execution_options(synchronize_session=False)
                )
                stmt = (
                    select(Marriage)
                    .where(
                        Marriage.status == MarriageStatus.ACTIVE,
                        or_(Marriage.user1_id == uid, Marriage.user2_id == uid),
                    )
                    .execution_options(populate_existing=True)
                    .order_by(Marriage.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                return await session.scalar(stmt)
            except OperationalError:
                if attempt + 1 < cls._LOCK_RETRIES:
                    await asyncio.sleep(cls._LOCK_RETRY_DELAY * (attempt + 1))
                    continue
                raise
        return None

    @classmethod
    async def _lock_marriage(
        cls, session: AsyncSession, marriage_id: int
    ) -> Optional[Marriage]:
        """Row-lock a ``Marriage`` by id.

        Delegates to :meth:`MarriageService._lock_marriage`, which owns the
        single implementation (this module already depends on ``marriage.py``,
        so nothing new is introduced and divorce can reuse the same helper).
        """
        return await MarriageService._lock_marriage(session, marriage_id)

    @classmethod
    async def _lock_gift_for_transition(
        cls, session: AsyncSession, gift_id: int
    ) -> Optional[FlowerGift]:
        gid = int(gift_id)
        for attempt in range(cls._LOCK_RETRIES):
            try:
                await session.execute(
                    update(FlowerGift)
                    .where(FlowerGift.id == gid)
                    .values(updated_at=func.now())
                    .execution_options(synchronize_session=False)
                )
                stmt = (
                    select(FlowerGift)
                    .where(FlowerGift.id == gid)
                    .execution_options(populate_existing=True)
                    .options(
                        selectinload(FlowerGift.sender),
                        selectinload(FlowerGift.receiver),
                        selectinload(FlowerGift.marriage),
                    )
                    .with_for_update()
                )
                return await session.scalar(stmt)
            except OperationalError:
                if attempt + 1 < cls._LOCK_RETRIES:
                    if not session.new and not session.dirty and not session.deleted:
                        await session.rollback()
                    await asyncio.sleep(cls._LOCK_RETRY_DELAY * (attempt + 1))
                    continue
                raise
        return None
