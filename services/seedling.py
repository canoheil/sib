"""Seedling reward service (Phase 6).

Business rules:
- A normal eligible group-game win awards exactly ONE seedling 🌱 to each
  winning player.
- A loss awards zero seedlings.
- A bot-match win awards zero seedlings (bot matches never feed personal
  competitive progression).
- Awards are transactional and idempotent: the (match_id, user_id) unique
  constraint makes duplicate callbacks/retries harmless.

Selling (shop «فروش آیتم‌ها»):
- ONLY seedlings above the garden's planting capacity are sellable. A
  planted tree is never removed (harvest only restarts its production
  cycle, and withering/restoring keeps the tree), so every plot is planted
  at most once and ``garden.capacity_plots`` is the hard ceiling on the
  seedlings a player can EVER use. Everything above it is dead inventory -
  the game's definition of an "extra" seedling.
- The price is config-driven (``seedling_sell_price``) and is paid in normal
  Apples through the same wallet ledger as every other sale
  (``AppleWalletService.earn``, reason ``seedling_sold``).
- Planting / growing / harvesting / invasion awards are untouched: only the
  SELL path is new.
"""

from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger, log_event, Events
from sib.database.models import Garden, SeedlingAward, SeedlingInventory
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.lock_order import lock_user_row


# Display identity of the seedling entry in the shop ("Sell Items" section).
# The asset key is the item key itself, so the shop resolves the bundled
# ``assets/items/seedling.jpg`` icon through the normal AssetService lookup.
SEEDLING_ITEM_KEY = "seedling"
SEEDLING_LABEL = "نهال"
SEEDLING_EMOJI = "🌱"

# Wallet ledger reason for a seedling sale (one auditable entry per sale).
SEEDLING_SOLD_REASON = "seedling_sold"


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
    async def award_invasion_victory(
        cls,
        session: AsyncSession,
        user_id: int,
        amount: int,
        *,
        invasion_id: int,
    ) -> int:
        """Credit the real seedling inventory once per v2 invasion and user.

        The invasion reward service owns the transaction and takes the user
        mutex first. Its completion marker, this source-keyed ledger and the
        inventory update all commit or roll back together.
        """
        if amount < 1:
            raise ValueError("seedling amount must be positive")
        existing = await session.scalar(
            select(SeedlingAward.id).where(
                SeedlingAward.source == "invasion_v2",
                SeedlingAward.source_ref_id == invasion_id,
                SeedlingAward.user_id == user_id,
            )
        )
        if existing is not None:
            return await cls.get_total(session, user_id)

        inv = await cls._get_or_create_inventory(session, user_id)
        inv.total += amount
        session.add(SeedlingAward(
            match_id=None, user_id=user_id, amount=amount,
            reason="invasion_v2_victory", source="invasion_v2",
            source_ref_id=invasion_id,
        ))
        await session.flush()
        log_event(
            Events.SEEDLING_AWARDED,
            {"invasion_id": invasion_id, "user_id": user_id, "amount": amount},
        )
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

    # ------------------------------------------------------------------
    # Shop: selling EXTRA seedlings (above the garden's planting capacity)
    # ------------------------------------------------------------------
    @staticmethod
    async def sell_price(session: AsyncSession) -> Optional[int]:
        """Price of ONE extra seedling in normal Apples.

        ``None`` means selling is disabled (missing/zero/invalid config), so
        the shop hides the entry exactly like an unpriced item.
        """
        raw = await ConfigService.get(session, "seedling_sell_price")
        try:
            price = int(raw)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    @staticmethod
    async def planting_capacity(session: AsyncSession, user_id: int) -> int:
        """Number of plots the player can plant into (= garden capacity).

        This is the structural ceiling on the seedlings a player can ever
        USE: a planted tree is never removed (a harvest only restarts its
        production cycle, and withering + Divine Fertilizer preserves the
        tree), so every plot is planted at most once. Anything above
        ``capacity_plots`` is dead inventory.

        A player without a garden row yet receives ``garden_initial_plots``
        plots the first time the garden is opened, so that value is their
        capacity until the garden exists.
        """
        capacity = await session.scalar(
            select(Garden.capacity_plots).where(Garden.user_id == user_id)
        )
        if capacity is not None:
            return max(0, int(capacity))
        return max(0, await ConfigService.get_int(session, "garden_initial_plots"))

    @classmethod
    async def sellable_total(cls, session: AsyncSession, user_id: int) -> int:
        """How many of the player's seedlings are sellable right now."""
        total = await cls.get_total(session, user_id)
        capacity = await cls.planting_capacity(session, user_id)
        return max(0, total - capacity)

    @classmethod
    async def sell_summary(
        cls, session: AsyncSession, user_id: int
    ) -> Tuple[int, int, int, Optional[int]]:
        """``(total, capacity, sellable_extra, unit_price)`` for the Sell screen."""
        total = await cls.get_total(session, user_id)
        capacity = await cls.planting_capacity(session, user_id)
        return total, capacity, max(0, total - capacity), await cls.sell_price(session)

    @classmethod
    async def sell(
        cls,
        session: AsyncSession,
        user_id: int,
        quantity: int = 1,
    ) -> int:
        """Sell ``quantity`` EXTRA seedlings for normal Apples.

        Returns the number of Apples credited. Raises ``ValueError`` (a
        refusal the caller must let escape its transaction, so nothing is
        committed) when the quantity is invalid, selling is disabled, or the
        player does not have that many seedlings above the planting capacity.

        Locks follow the global policy: the user row (rank 0) first, then the
        seedling inventory row (the rank-5 inventory tier). The wallet credit
        (rank 6) comes after the decrement, exactly like ``ItemService.sell``.
        """
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("تعداد باید حداقل ۱ باشد.")

        await lock_user_row(session, user_id)
        price = await cls.sell_price(session)
        if price is None:
            raise ValueError("این آیتم قابل فروش نیست.")

        inv = await session.scalar(
            select(SeedlingInventory)
            .where(SeedlingInventory.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        have = int(inv.total) if inv is not None else 0
        # Capacity is read under the user lock: a plot unlock also takes rank
        # 0, so it cannot move while this transaction runs.
        capacity = await cls.planting_capacity(session, user_id)
        available = max(0, have - capacity)
        if quantity > available:
            raise ValueError(
                f"موجودی {SEEDLING_EMOJI} «{SEEDLING_LABEL}» کافی نیست! "
                f"(لازم: {quantity}، قابل فروش: {available}) — فقط نهال‌های "
                f"بیشتر از ظرفیت باغ ({capacity} قطعه) فروختنی‌اند."
            )

        inv.total -= quantity
        await session.flush()

        apples = price * quantity
        await AppleWalletService.earn(
            session,
            user_id,
            apples,
            reason=SEEDLING_SOLD_REASON,
            ref_type="seedling",
            ref_id=user_id,
        )
        logger.info(
            f"seedling sold: user={user_id} quantity={quantity} "
            f"apples={apples} remaining={inv.total}"
        )
        log_event(
            Events.SEEDLING_SOLD,
            {
                "user_id": user_id,
                "quantity": quantity,
                "unit_price": price,
                "apples": apples,
                "remaining": inv.total,
                "capacity": capacity,
            },
        )
        return apples
