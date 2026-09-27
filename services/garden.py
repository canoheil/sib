"""Personal garden service (Phase 7).

Growth model (all timings configurable via system_config):
- plant:      EMPTY   + 1 seedling        -> GROWING   (planted_at, mature_at)
- mature:     t >= mature_at              -> MATURE    (fruit cycle starts)
- fruit:      t >= fruit_ready_at         -> FRUIT_READY (one harvest ready)
- harvest:    FRUIT_READY                 -> apples into wallet, tree
              restarts its production cycle (fruit_ready_at = now + cycle).

State is DERIVED from server timestamps on read so client clock changes
can never accelerate growth. Uncollected fruit stays available; it does
not stack beyond one harvest per cycle.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger, log_event, Events
from sib.database.models import Garden, GardenPlot, PlotState
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.config_store import ConfigService
from sib.services.seedling import SeedlingService


@dataclass
class PlotView:
    """Rendered view of a plot with its derived state."""

    plot_index: int
    state: str
    planted_at: Optional[datetime]
    mature_at: Optional[datetime]
    fruit_ready_at: Optional[datetime]
    cycle_count: int
    seconds_to_next_stage: int


class GardenService:
    """Planting, growth, harvest and expansion of personal gardens."""

    @staticmethod
    def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
        """Normalize DB timestamps to timezone-aware UTC datetimes.

        MariaDB DATETIME values may be returned as naive datetimes even when
        the SQLAlchemy column is declared with timezone=True.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    # Basics
    # ------------------------------------------------------------------
    @staticmethod
    async def get_or_create(session: AsyncSession, user_id: int) -> Garden:
        garden = await session.scalar(
            select(Garden)
            .where(Garden.user_id == user_id)
            .options(selectinload(Garden.plots))
            .with_for_update()
        )
        if garden:
            return garden

        capacity = await ConfigService.get_int(session, "garden_initial_plots")
        garden = Garden(user_id=user_id, capacity_plots=capacity)
        session.add(garden)
        await session.flush()

        for idx in range(capacity):
            session.add(GardenPlot(garden_id=user_id, plot_index=idx))
        await session.flush()
        await session.refresh(garden, ["plots"])
        log_event(Events.GARDEN_EVENT, {"user_id": user_id, "action": "created", "plots": capacity})
        return garden

    @classmethod
    def _derive_state(cls, plot: GardenPlot, now: datetime) -> str:
        now = cls._as_utc(now) or datetime.now(timezone.utc)
        planted_at = cls._as_utc(plot.planted_at)
        mature_at = cls._as_utc(plot.mature_at)
        fruit_ready_at = cls._as_utc(plot.fruit_ready_at)

        if planted_at is None:
            return PlotState.EMPTY
        if fruit_ready_at is not None and now >= fruit_ready_at:
            return PlotState.FRUIT_READY
        if mature_at is not None and now >= mature_at:
            return PlotState.MATURE
        return PlotState.GROWING

    @classmethod
    def view(cls, garden: Garden, now: Optional[datetime] = None) -> List[PlotView]:
        # Normalize once: DB timestamps may be naive (MariaDB DATETIME) and
        # callers may pass naive values. Every comparison/subtraction below
        # uses timezone-aware UTC only.
        now = cls._as_utc(now) or datetime.now(timezone.utc)
        views = []
        for plot in sorted(garden.plots, key=lambda p: p.plot_index):
            planted_at = cls._as_utc(plot.planted_at)
            mature_at = cls._as_utc(plot.mature_at)
            fruit_ready_at = cls._as_utc(plot.fruit_ready_at)
            state = cls._derive_state(plot, now)
            next_dt: Optional[datetime] = None
            if state == PlotState.GROWING:
                next_dt = mature_at
            elif state == PlotState.MATURE:
                next_dt = fruit_ready_at
            seconds = 0
            if next_dt is not None:
                seconds = max(0, int((next_dt - now).total_seconds()))
            views.append(
                PlotView(
                    plot_index=plot.plot_index,
                    state=state,
                    planted_at=planted_at,
                    mature_at=mature_at,
                    fruit_ready_at=fruit_ready_at,
                    cycle_count=plot.cycle_count,
                    seconds_to_next_stage=seconds,
                )
            )
        return views

    # ------------------------------------------------------------------
    # Planting
    # ------------------------------------------------------------------
    @classmethod
    async def plant(
        cls,
        session: AsyncSession,
        user_id: int,
        plot_index: int,
    ) -> GardenPlot:
        """Plant one seedling into an empty plot (consumes the seedling)."""
        garden = await cls.get_or_create(session, user_id)
        plot = await session.scalar(
            select(GardenPlot)
            .where(GardenPlot.garden_id == garden.user_id)
            .where(GardenPlot.plot_index == plot_index)
            .with_for_update()
        )
        if plot is None:
            raise ValueError("این قطعه در باغ شما وجود ندارد.")
        if cls._derive_state(plot, datetime.now(timezone.utc)) != PlotState.EMPTY:
            raise ValueError("این قطعه خالی نیست!")

        seedlings = await SeedlingService.get_total(session, user_id)
        if seedlings < 1:
            raise ValueError("نهال کافی ندارید! 🌱")

        now = datetime.now(timezone.utc)
        growth = await ConfigService.get_int(session, "garden_growth_seconds")
        fruit = await ConfigService.get_int(session, "garden_fruit_seconds")

        plot.planted_at = now
        plot.mature_at = now + timedelta(seconds=growth)
        plot.fruit_ready_at = plot.mature_at + timedelta(seconds=fruit)

        # Consume exactly one seedling (auditable, transactional).
        from sib.database.models import SeedlingInventory

        inv = await session.scalar(
            select(SeedlingInventory)
            .where(SeedlingInventory.user_id == user_id)
            .with_for_update()
        )
        if inv is None or inv.total < 1:
            raise ValueError("نهال کافی ندارید! 🌱")
        inv.total -= 1
        await session.flush()

        log_event(
            Events.GARDEN_EVENT,
            {"user_id": user_id, "action": "planted", "plot": plot_index},
        )
        return plot

    # ------------------------------------------------------------------
    # Harvest
    # ------------------------------------------------------------------
    @classmethod
    async def harvest(
        cls,
        session: AsyncSession,
        user_id: int,
        plot_index: int,
    ) -> int:
        """Collect ripe fruit into the wallet and restart the cycle.

        Returns the number of apples collected. The award is idempotent
        per production cycle (cycle_count advances exactly once).
        """
        garden = await cls.get_or_create(session, user_id)
        plot = await session.scalar(
            select(GardenPlot)
            .where(GardenPlot.garden_id == garden.user_id)
            .where(GardenPlot.plot_index == plot_index)
            .with_for_update()
        )
        if plot is None:
            raise ValueError("این قطعه در باغ شما وجود ندارد.")

        now = datetime.now(timezone.utc)
        if cls._derive_state(plot, now) != PlotState.FRUIT_READY:
            raise ValueError("هنوز میوه‌ای برای برداشت آماده نیست! 🍎")

        amount = await ConfigService.get_int(session, "garden_fruit_amount")
        fruit = await ConfigService.get_int(session, "garden_fruit_seconds")

        await AppleWalletService.earn(
            session,
            user_id=user_id,
            amount=amount,
            reason="garden_harvest",
            ref_type="garden_plot",
            ref_id=plot.id,
            idempotency_key=f"garden:{plot.id}:{plot.cycle_count}",
        )

        # Restart the production cycle of the mature tree.
        plot.cycle_count += 1
        plot.fruit_ready_at = now + timedelta(seconds=fruit)
        await session.flush()

        log_event(
            Events.GARDEN_EVENT,
            {"user_id": user_id, "action": "harvested", "plot": plot_index, "apples": amount},
        )
        return amount

    # ------------------------------------------------------------------
    # Expansion
    # ------------------------------------------------------------------
    @classmethod
    async def expand(
        cls,
        session: AsyncSession,
        user_id: int,
    ) -> int:
        """Buy additional plots with apples. Returns the new capacity."""
        garden = await cls.get_or_create(session, user_id)

        step = await ConfigService.get_int(session, "garden_expansion_step")
        cost = await ConfigService.get_int(session, "garden_expansion_cost_apples")
        max_plots = await ConfigService.get_int(session, "garden_max_plots")

        if garden.capacity_plots + step > max_plots:
            raise ValueError("باغ شما به حداکثر ظرفیت رسیده است! 🌳")

        try:
            await AppleWalletService.spend(
                session,
                user_id=user_id,
                amount=cost,
                reason="garden_expansion",
                ref_type="garden",
                ref_id=user_id,
                idempotency_key=f"garden_expand:{user_id}:{garden.capacity_plots}",
            )
        except InsufficientFundsException:
            raise ValueError("سیب‌های شما برای خرید قطعه‌های جدید کافی نیست! 🍎")

        for idx in range(garden.capacity_plots, garden.capacity_plots + step):
            session.add(GardenPlot(garden_id=user_id, plot_index=idx))
        garden.capacity_plots += step
        await session.flush()
        await session.refresh(garden, ["plots"])

        log_event(
            Events.GARDEN_EVENT,
            {"user_id": user_id, "action": "expanded", "capacity": garden.capacity_plots},
        )
        return garden.capacity_plots
