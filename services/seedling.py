"""Seedling reward service (Phase 6).

Business rules:
- A normal eligible group-game win awards exactly ONE seedling 🌱 to each
  winning player.
- A loss awards zero seedlings.
- A bot-match win awards zero seedlings (bot matches never feed personal
  competitive progression).
- Awards are transactional and idempotent: the (match_id, user_id) unique
  constraint makes duplicate callbacks/retries harmless.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger, log_event, Events
from sib.database.models import SeedlingAward, SeedlingInventory


class SeedlingService:
    """Personal seedling inventory and awards."""

    @staticmethod
    async def get_total(session: AsyncSession, user_id: int) -> int:
        inv = await session.scalar(
            select(SeedlingInventory).where(SeedlingInventory.user_id == user_id)
        )
        return inv.total if inv else 0

    @staticmethod
    async def _get_or_create_inventory(
        session: AsyncSession, user_id: int
    ) -> SeedlingInventory:
        inv = await session.scalar(
            select(SeedlingInventory)
            .where(SeedlingInventory.user_id == user_id)
            .with_for_update()
        )
        if not inv:
            inv = SeedlingInventory(user_id=user_id, total=0)
            session.add(inv)
            await session.flush()
        return inv

    @classmethod
    async def award_match_win(
        cls,
        session: AsyncSession,
        match_id: int,
        user_id: int,
        amount: int = 1,
    ) -> bool:
        """Award seedlings for a match win. Returns True only on first award.

        Idempotent: a second call for the same (match_id, user_id) is a
        no-op, regardless of retries or duplicate callback processing.
        """
        existing = await session.scalar(
            select(SeedlingAward.id).where(
                SeedlingAward.match_id == match_id,
                SeedlingAward.user_id == user_id,
            )
        )
        if existing is not None:
            logger.warning(
                f"Seedling award already issued for match={match_id} user={user_id}; "
                f"skipping duplicate."
            )
            return False

        inv = await cls._get_or_create_inventory(session, user_id)
        inv.total += amount
        session.add(
            SeedlingAward(
                match_id=match_id,
                user_id=user_id,
                amount=amount,
                reason="match_win",
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            # Concurrent duplicate insert: the unique constraint is the
            # final arbiter; treat as already awarded.
            await session.rollback()
            logger.warning(
                f"Concurrent duplicate seedling award for match={match_id} user={user_id}"
            )
            return False

        log_event(
            Events.SEEDLING_AWARDED,
            {"match_id": match_id, "user_id": user_id, "amount": amount},
        )
        return True

    @classmethod
    async def admin_adjust(
        cls,
        session: AsyncSession,
        user_id: int,
        delta: int,
        reason: str = "admin_adjust",
    ) -> int:
        """Controlled administrative adjustment (may be negative, clamped at 0)."""
        inv = await cls._get_or_create_inventory(session, user_id)
        inv.total = max(0, inv.total + delta)
        session.add(
            SeedlingAward(match_id=None, user_id=user_id, amount=delta, reason=reason)
        )
        await session.flush()
        return inv.total

    @classmethod
    async def award_daily_chest(
        cls,
        session: AsyncSession,
        user_id: int,
        amount: int,
        *,
        claim_id: int,
    ) -> int:
        """Grant daily-chest seedlings idempotently per (claim, user).

        Same award ledger and inventory as match wins and Apple-Battle wins
        (``source='chest'`` names the grant source; ``source_ref_id`` is the
        owning ``daily_chest_claims.id``). The (source, source_ref_id,
        user_id) unique constraint makes any duplicate delivery a no-op.
        """
        if amount < 1:
            raise ValueError("seedling amount must be positive")
        existing = await session.scalar(
            select(SeedlingAward.id).where(
                SeedlingAward.source == "chest",
                SeedlingAward.source_ref_id == claim_id,
                SeedlingAward.user_id == user_id,
            )
        )
        if existing is not None:
            logger.warning(
                f"Seedling chest award already issued for claim={claim_id} "
                f"user={user_id}; skipping duplicate."
            )
            return await cls.get_total(session, user_id)

        inv = await cls._get_or_create_inventory(session, user_id)
        inv.total += amount
        session.add(
            SeedlingAward(
                match_id=None,
                user_id=user_id,
                amount=amount,
                reason="daily_chest",
                source="chest",
                source_ref_id=claim_id,
            )
        )
        await session.flush()
        log_event(
            Events.SEEDLING_AWARDED,
            {"claim_id": claim_id, "user_id": user_id, "amount": amount},
        )
        return inv.total
