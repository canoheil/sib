"""Card trading service (Phase 13).

Rules (from spec):
- One trade per user per calendar day.
- Both cards must have the same level.
- Both players must explicitly confirm; double confirmation model.
- Ownership transfer is atomic (one transaction).

Safety model (hardened):
- Every read that decides an outcome happens under a row lock
  (``SELECT ... FOR UPDATE``): the trade row and both player cards.
- ``_execute`` re-verifies the trade is still PENDING (so a duplicate
  confirmation/callback can never transfer a card twice) and that both
  cards still belong to the original owners (a card moved elsewhere fails
  the trade safely instead of corrupting inventory).
- Expired trades are refused and marked EXPIRED; ``expire_overdue``
  sweeps stale offers so the UI never shows a dead trade.

Trading is CARD-to-CARD today. The transfer itself only touches
``PlayerCard.user_id``, so a future tradable asset type can be added by
extending the trade payload without changing the confirmation/locking
model.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import PlayerCard, Trade, TradeStatus
from sib.services.config_store import ConfigService


class TradeError(ValueError):
    """Raised for invalid trade operations."""


class TradeService:
    """Card trading with double confirmation and daily limits."""

    # ------------------------------------------------------------------
    # Daily limit
    # ------------------------------------------------------------------
    @classmethod
    async def _trades_used_today(cls, session: AsyncSession, user_id: int) -> int:
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        count = await session.scalar(
            select(func.count(Trade.id)).where(
                ((Trade.proposer_id == user_id) | (Trade.recipient_id == user_id))
                & (Trade.created_at >= day_start)
            )
        )
        return int(count or 0)

    @classmethod
    async def can_trade_today(cls, session: AsyncSession, user_id: int) -> bool:
        limit = int(await ConfigService.get(session, "trades_per_day"))
        return (await cls._trades_used_today(session, user_id)) < limit

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        proposer_id: int,
        recipient_id: int,
        proposer_card_id: int,
        recipient_card_id: int,
    ) -> Trade:
        if proposer_id == recipient_id:
            raise TradeError("نمی‌توانید با خودتان معامله کنید!")

        if proposer_card_id == recipient_card_id:
            raise TradeError("یک کارت را نمی‌توان با خودش مبادله کرد!")

        if not await cls.can_trade_today(session, proposer_id):
            raise TradeError("سهمیه مبادله امروز شما تمام شده است. فردا دوباره تلاش کنید!")
        if not await cls.can_trade_today(session, recipient_id):
            raise TradeError("سهمیه مبادله امروز طرف مقابل تمام شده است.")

        # Locked reads: a card being moved by another trade can never be
        # claimed twice.
        proposer_card = await cls._card_of(session, proposer_id, proposer_card_id)
        recipient_card = await cls._card_of(session, recipient_id, recipient_card_id)

        if proposer_card.level != recipient_card.level:
            raise TradeError(
                f"مبادله فقط بین کارت‌های هم‌سطح مجاز است "
                f"(سطح {proposer_card.level} در برابر سطح {recipient_card.level})."
            )

        # No pending trades on either card (prevents double-spend of a card).
        pending = await session.scalar(
            select(func.count(Trade.id)).where(
                ((Trade.proposer_card_id == proposer_card_id)
                 | (Trade.recipient_card_id == recipient_card_id)
                 | (Trade.proposer_card_id == recipient_card_id)
                 | (Trade.recipient_card_id == proposer_card_id))
                & (Trade.status == TradeStatus.PENDING)
            )
        )
        if pending:
            raise TradeError("یکی از کارت‌ها در معامله‌ی باز دیگری است.")

        trade = Trade(
            proposer_id=proposer_id,
            recipient_id=recipient_id,
            proposer_card_id=proposer_card_id,
            recipient_card_id=recipient_card_id,
            status=TradeStatus.PENDING,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=int(await ConfigService.get(session, "trade_expiration_hours")) * 3600),
        )
        session.add(trade)
        await session.flush()
        log_event(
            Events.TRADE_EVENT,
            {"action": "created", "trade_id": trade.id, "proposer": proposer_id},
        )
        return trade

    # ------------------------------------------------------------------
    # Confirmation (double confirmation model)
    # ------------------------------------------------------------------
    @classmethod
    async def confirm(cls, session: AsyncSession, trade_id: int, user_id: int) -> Trade:
        # Row lock: two simultaneous confirmations (one per side) are
        # serialized, so the swap runs exactly once.
        trade = await cls.get_trade(session, trade_id, for_update=True)
        await cls._assert_open(session, trade)

        if user_id == trade.proposer_id:
            # Duplicate callback / double tap: idempotent, never executes twice.
            trade.proposer_confirmed = True
        elif user_id == trade.recipient_id:
            trade.recipient_confirmed = True
        else:
            raise TradeError("شما در این معامله نقشی ندارید.")

        if trade.proposer_confirmed and trade.recipient_confirmed:
            await cls._execute(session, trade)
        else:
            await session.flush()
        return trade

    @classmethod
    async def cancel(cls, session: AsyncSession, trade_id: int, user_id: int) -> Trade:
        trade = await cls.get_trade(session, trade_id, for_update=True)
        await cls._assert_open(session, trade)
        if user_id not in (trade.proposer_id, trade.recipient_id):
            raise TradeError("شما در این معامله نقشی ندارید.")
        trade.status = TradeStatus.CANCELLED
        await session.flush()
        log_event(Events.TRADE_EVENT, {"action": "cancelled", "trade_id": trade.id})
        return trade

    # ------------------------------------------------------------------
    # Execution (atomic ownership swap)
    # ------------------------------------------------------------------
    @staticmethod
    async def _execute(session: AsyncSession, trade: Trade) -> None:
        # Exactly-once guard: the trade row is locked by the caller, so a
        # duplicate confirmation or a retry can never swap a second time.
        if trade.status != TradeStatus.PENDING:
            return

        proposer_card = await session.get(
            PlayerCard, trade.proposer_card_id, with_for_update=True
        )
        recipient_card = await session.get(
            PlayerCard, trade.recipient_card_id, with_for_update=True
        )
        if not proposer_card or not recipient_card:
            trade.status = TradeStatus.CANCELLED
            await session.flush()
            raise TradeError("کارت یکی از طرفین دیگر موجود نیست؛ معامله لغو شد.")

        # Sanity: cards must still belong to the original owners. If a card
        # was transferred elsewhere meanwhile, the trade fails SAFELY.
        if proposer_card.user_id != trade.proposer_id or recipient_card.user_id != trade.recipient_id:
            trade.status = TradeStatus.CANCELLED
            await session.flush()
            raise TradeError("مالکیت کارت‌ها تغییر کرده است؛ معامله لغو شد.")

        if proposer_card.id == recipient_card.id:
            trade.status = TradeStatus.CANCELLED
            await session.flush()
            raise TradeError("این معامله معتبر نیست؛ لغو شد.")

        # Atomic swap of ownership.
        proposer_card.user_id, recipient_card.user_id = (
            recipient_card.user_id,
            proposer_card.user_id,
        )
        # Favorites don't travel with the card.
        proposer_card.is_favorite = False
        recipient_card.is_favorite = False

        trade.status = TradeStatus.COMPLETED
        trade.completed_at = datetime.now(timezone.utc)
        await session.flush()
        log_event(
            Events.TRADE_EVENT,
            {"action": "completed", "trade_id": trade.id},
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    @staticmethod
    async def get_trade(
        session: AsyncSession, trade_id: int, *, for_update: bool = False
    ) -> Optional[Trade]:
        """Fetch one trade (optionally row-locked for a state transition)."""
        if not for_update:
            return await session.get(Trade, trade_id)
        return await session.scalar(
            select(Trade).where(Trade.id == trade_id).with_for_update()
        )

    @staticmethod
    async def list_pending(session: AsyncSession, user_id: int) -> List[Trade]:
        stmt = (
            select(Trade)
            .where(
                ((Trade.proposer_id == user_id) | (Trade.recipient_id == user_id))
                & (Trade.status == TradeStatus.PENDING)
            )
            .order_by(Trade.id.desc())
        )
        return list((await session.scalars(stmt)).all())

    @classmethod
    async def expire_overdue(cls, session: AsyncSession) -> int:
        """Mark every overdue PENDING trade as EXPIRED (stale-offer sweep)."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(Trade)
            .where(Trade.status == TradeStatus.PENDING)
            .with_for_update()
        )
        expired = 0
        for trade in (await session.scalars(stmt)).all():
            expires = trade.expires_at
            if expires is None:
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                trade.status = TradeStatus.EXPIRED
                expired += 1
        if expired:
            await session.flush()
            log_event(Events.TRADE_EVENT, {"action": "expired_sweep", "count": expired})
            logger.info(f"Expired {expired} stale trade(s)")
        return expired

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    async def _card_of(session: AsyncSession, user_id: int, card_id: int) -> PlayerCard:
        """Owned card, row-locked (ownership is verified under the lock)."""
        card = await session.get(PlayerCard, card_id, with_for_update=True)
        if not card or card.user_id != user_id:
            raise TradeError("کارت موردنظر متعلق به شما نیست.")
        return card

    @staticmethod
    async def _assert_open(session: AsyncSession, trade: Optional[Trade]) -> Trade:
        if not trade:
            raise TradeError("معامله پیدا نشد.")
        if trade.status != TradeStatus.PENDING:
            raise TradeError("این معامله بسته شده است.")
        if trade.expires_at is not None:
            expires = trade.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                trade.status = TradeStatus.EXPIRED
                await session.flush()
                raise TradeError("این معامله منقضی شده است.")
        return trade
