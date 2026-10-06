"""Card trading service (Phase 13).

Rules (from spec):
- One trade per user per calendar day.
- Both cards must have the same level.
- Both players must explicitly confirm; double confirmation model.
- Ownership transfer is atomic (one transaction).

Daily quota (semantics pinned by tests + maintenance sweep):
- A trade consumes a participant's daily slot while it is PENDING and after
  it COMPLETED or was deliberately CANCELLED by a user.
- Stale (EXPIRED) and failed (FAILED) trades never consume the slot — a
  trade that did not happen must not burn the user's one trade per day.

Safety model (hardened):
- Every read that decides an outcome happens under a row lock
  (``SELECT ... FOR UPDATE``): the trade row and both player cards.
  Cards are always locked in ascending id order so two concurrent trades
  can never deadlock on the same pair of cards.
- ``_execute`` re-verifies the trade is still PENDING via an ATOMIC
  one-shot claim (conditional UPDATE), so a duplicate confirmation or a
  retry can never transfer a card twice — exactly-once even without row
  locks. It also re-verifies that both cards still belong to the original
  owners and that the swap would not give either player two cards of the
  same character (``uq_player_card_user_character``); such a trade fails
  safely as FAILED instead of corrupting inventory.
- The ownership swap itself is collision-free: both cards are parked on
  unique non-player owner ids before the final owners are assigned, so a
  same-character swap never trips the (user_id, character_id) unique
  constraint mid-flight.
- Expired trades are refused and marked EXPIRED; ``expire_overdue``
  sweeps stale offers so the UI never shows a dead trade.

Trading is CARD-to-CARD today. The transfer itself only touches
``PlayerCard.user_id``, so a future tradable asset type can be added by
extending the trade payload without changing the confirmation/locking
model.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select, update
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
                # Only trades that really happened (or are open right now)
                # consume the daily slot: a stale/expired or failed trade
                # must never burn the user's one trade per day.
                & (
                    Trade.status.in_(
                        (
                            TradeStatus.PENDING,
                            TradeStatus.COMPLETED,
                            TradeStatus.CANCELLED,
                        )
                    )
                )
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
            raise TradeError("نمی‌تونی با خودت معامله کنی!")

        if proposer_card_id == recipient_card_id:
            raise TradeError("یک کارت را نمی‌توان با خودش مبادله کرد!")

        if not await cls.can_trade_today(session, proposer_id):
            raise TradeError("سهمیه مبادله امروزت تموم شده. فردا دوباره تلاش کن!")
        if not await cls.can_trade_today(session, recipient_id):
            raise TradeError("سهمیه مبادله امروز طرف مقابل تموم شده.")

        # Locked reads: a card being moved by another trade can never be
        # claimed twice. Both cards are locked in ascending id order so two
        # concurrent creates on overlapping cards cannot deadlock.
        cards = await cls._locked_cards(session, (proposer_card_id, recipient_card_id))
        proposer_card = cards.get(proposer_card_id)
        if not proposer_card or proposer_card.user_id != proposer_id:
            raise TradeError("کارت موردنظر متعلق به شما نیست.")
        recipient_card = cards.get(recipient_card_id)
        if not recipient_card or recipient_card.user_id != recipient_id:
            raise TradeError("کارت موردنظر متعلق به شما نیست.")

        if proposer_card.level != recipient_card.level:
            raise TradeError(
                f"مبادله فقط بین کارت‌های هم‌سطح مجاز است "
                f"(سطح {proposer_card.level} در برابر سطح {recipient_card.level})."
            )

        # The END state must respect 'one card per character' for both sides;
        # reject up front with a friendly message instead of failing later
        # with a raw database constraint error.
        if await cls._swap_conflicts(session, proposer_card, recipient_card):
            raise TradeError(
                "با این مبادله، یکی از شما صاحب دو کارت از یک شخصیت می‌شد؛ "
                "هر بازیکن فقط می‌تونه از هر شخصیت یه کارت داشته باشه."
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
            raise TradeError("تو این معامله نقشی نداری.")

        # Persist the flag, then always attempt execution: the one-shot claim
        # inside ``_execute`` decides from the CURRENT database state (our
        # flush plus any concurrently committed confirmation), so a
        # simultaneous confirmation by the other side can never be lost.
        await session.flush()
        await cls._execute(session, trade)
        return trade

    @classmethod
    async def cancel(cls, session: AsyncSession, trade_id: int, user_id: int) -> Trade:
        trade = await cls.get_trade(session, trade_id, for_update=True)
        await cls._assert_open(session, trade)
        if user_id not in (trade.proposer_id, trade.recipient_id):
            raise TradeError("تو این معامله نقشی نداری.")
        trade.status = TradeStatus.CANCELLED
        await session.flush()
        log_event(Events.TRADE_EVENT, {"action": "cancelled", "trade_id": trade.id})
        return trade

    # ------------------------------------------------------------------
    # Execution (atomic ownership swap)
    # ------------------------------------------------------------------
    @staticmethod
    async def _execute(session: AsyncSession, trade: Trade) -> None:
        # Exactly-once guard: the caller may hold the trade row lock, but the
        # real guarantee is this one-shot claim — a single conditional UPDATE
        # that only one confirming transaction can win. A duplicate
        # confirmation, a retry or a concurrent second confirm can never swap
        # a second time (even on engines without ``FOR UPDATE``).
        claimed = await session.execute(
            update(Trade)
            .where(
                Trade.id == trade.id,
                Trade.status == TradeStatus.PENDING,
                Trade.proposer_confirmed.is_(True),
                Trade.recipient_confirmed.is_(True),
            )
            .values(status=TradeStatus.COMPLETING)
        )
        if claimed.rowcount != 1:
            return
        trade.status = TradeStatus.COMPLETING

        # Locked reads in ascending id order (see ``_locked_cards``).
        cards = await TradeService._locked_cards(
            session, (trade.proposer_card_id, trade.recipient_card_id)
        )
        proposer_card = cards.get(trade.proposer_card_id)
        recipient_card = cards.get(trade.recipient_card_id)
        if not proposer_card or not recipient_card:
            await TradeService._fail(
                session, trade, "کارت یکی از طرفین دیگر موجود نیست؛ معامله انجام نشد."
            )

        # Sanity: cards must still belong to the original owners. If a card
        # was transferred elsewhere meanwhile, the trade fails SAFELY.
        if proposer_card.user_id != trade.proposer_id or recipient_card.user_id != trade.recipient_id:
            await TradeService._fail(
                session, trade, "مالکیت کارت‌ها تغییر کرده؛ معامله انجام نشد."
            )

        if proposer_card.id == recipient_card.id:
            await TradeService._fail(session, trade, "این معامله معتبر نیست؛ انجام نشد.")

        # Re-validate the END state under the card locks: 'one card per
        # character' can change between creation and confirmation (e.g. a
        # fresh card was acquired meanwhile). Fail SAFELY instead of letting
        # the database constraint blow up mid-swap.
        if await TradeService._swap_conflicts(session, proposer_card, recipient_card):
            await TradeService._fail(
                session,
                trade,
                "با این مبادله یکی از شما صاحب دو کارت از یک شخصیت می‌شد؛ معامله انجام نشد.",
            )

        # Atomic swap of ownership — collision-free with
        # uq_player_card_user_character: two direct user_id UPDATEs would
        # transiently hand one owner two cards of the same character (both
        # cards of one character is a legal trade!), which the unique pair
        # (user_id, character_id) rejects mid-flight. Park BOTH rows on
        # unique non-player ids first (negative card ids can never collide
        # with a real Telegram user id), then assign the final owners — every
        # intermediate state satisfies the constraint, and the whole swap is
        # one transaction (rollback on failure restores the original owners).
        proposer_owner, recipient_owner = proposer_card.user_id, recipient_card.user_id
        proposer_card.user_id = -proposer_card.id
        recipient_card.user_id = -recipient_card.id
        await session.flush()

        proposer_card.user_id = recipient_owner
        recipient_card.user_id = proposer_owner
        # Favorites don't travel with the card.
        proposer_card.is_favorite = False
        recipient_card.is_favorite = False

        trade.status = TradeStatus.COMPLETED
        trade.completed_at = datetime.now(timezone.utc)
        await session.flush()

        from sib.services.player_xp import PlayerXPService

        # The incoming character is a one-time collection milestone for its
        # new owner. Existing owners (including launch-era cards marked by
        # the migration) are protected by the per-user/character event key.
        incoming = [
            (recipient_owner, proposer_card.character_id),
            (proposer_owner, recipient_card.character_id),
        ]
        for owner_id, character_id in sorted(incoming, key=lambda item: item[0]):
            await PlayerXPService.award_character_collection(
                session, user_id=owner_id, character_id=character_id
            )
            await PlayerXPService.award(
                session,
                user_id=owner_id,
                source="trade",
                event_key=f"trade:{trade.id}",
            )

        log_event(
            Events.TRADE_EVENT,
            {"action": "completed", "trade_id": trade.id},
        )

    @staticmethod
    async def _fail(session: AsyncSession, trade: Trade, message: str) -> None:
        """Mark the trade FAILED (quota released) and abort the transition.

        Only called on VALID session state (before any partial swap), so the
        FAILED status is flushed and later committed together with the
        handler's normal exit — the trade is closed, nothing moved, and the
        daily quota of both participants is released.
        """
        trade.status = TradeStatus.FAILED
        await session.flush()
        log_event(
            Events.TRADE_EVENT,
            {"action": "failed", "trade_id": trade.id, "reason": message},
        )
        raise TradeError(message)

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
    async def _locked_cards(
        session: AsyncSession, card_ids: Tuple[int, ...]
    ) -> Dict[int, PlayerCard]:
        """The given cards, row-locked in deterministic (ascending id) order.

        Two concurrent flows locking the same cards in opposite orders can
        deadlock; one global lock order makes that impossible. An explicit
        ``SELECT ... FOR UPDATE`` is used (instead of ``session.get(...,
        with_for_update=True)``) so the locking read is emitted even for rows
        already present in the session's identity map.
        """
        ids = sorted({card_id for card_id in card_ids})
        if not ids:
            return {}
        rows = await session.scalars(
            select(PlayerCard)
            .where(PlayerCard.id.in_(ids))
            .order_by(PlayerCard.id)
            .with_for_update()
        )
        return {card.id: card for card in rows.all()}

    @staticmethod
    async def _swap_conflicts(
        session: AsyncSession, proposer_card: PlayerCard, recipient_card: PlayerCard
    ) -> bool:
        """True when the swap would give a player two cards of one character.

        Enforces the 'one card per character' rule on the END state of the
        trade. A same-character swap (both cards of character X) is legal —
        each side still ends with exactly one X card.
        """
        for keeper, incoming_character_id in (
            (proposer_card, recipient_card.character_id),
            (recipient_card, proposer_card.character_id),
        ):
            conflict = await session.scalar(
                select(func.count(PlayerCard.id)).where(
                    PlayerCard.user_id == keeper.user_id,
                    PlayerCard.character_id == incoming_character_id,
                    PlayerCard.id != keeper.id,
                )
            )
            if conflict:
                return True
        return False

    @staticmethod
    async def _assert_open(session: AsyncSession, trade: Optional[Trade]) -> Trade:
        if not trade:
            raise TradeError("معامله پیدا نشد.")
        if trade.status != TradeStatus.PENDING:
            raise TradeError("این معامله بسته شده.")
        if trade.expires_at is not None:
            expires = trade.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                trade.status = TradeStatus.EXPIRED
                await session.flush()
                raise TradeError("این معامله منقضی شده.")
        return trade
