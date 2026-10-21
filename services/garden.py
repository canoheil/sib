"""Personal garden service (Phase 7 + magic trees).

Growth model (all timings configurable via system_config):
- plant:      EMPTY   + 1 seedling        -> GROWING   (planted_at, mature_at)
- mature:     t >= mature_at              -> MATURE    (fruit cycle starts)
- fruit:      t >= fruit_ready_at         -> FRUIT_READY (one harvest ready)
- harvest:    FRUIT_READY                 -> ONE roll on the magic-level
              drop table: Normal Apple = currency credit, special Apple
              (Golden/Diamond/Obsidian) = inventory item. The tree then
              restarts its production cycle (fruit_ready_at = now + cycle).

Magical progression (per-tree, Magic Powder driven):
- every tree has its own magic_level 0..garden_max_magic_level (default 5),
- upgrading consumes Magic Powder (cost = magic_powder_level_costs[level]),
- the level shifts the drop table towards special Apples.

Plot unlocks: plots 1..garden_initial_plots are free; each further plot is
bought individually at garden_plot_prices (100/250/500/1000/2000 for plots
6..10). Gardens that already exceed the new 10-plot cap are grandfathered.

State is DERIVED from server timestamps on read so client clock changes
can never accelerate growth. Uncollected fruit stays available; it does
not stack beyond one harvest per cycle.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from sib.services.invasion_v2.enums import InvasionStatus

from sib.core.logging import logger, log_event, Events
from sib.database.models import (
    Garden,
    GardenPlot,
    GardenWitherEvent,
    Inv2Attack,
    Inv2GroupMember,
    Inv2Invasion,
    PlotState,
)
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.config_store import ConfigService
from sib.services.lock_order import lock_user_row
from sib.services.seedling import SeedlingService

# Item key of the consumable that restores a withered plot.
DIVINE_FERTILIZER_ITEM = "divine_fertilizer"

# Persian refusal shown by every action blocked by corruption.
WITHERED_REFUSAL = "این قطعه پژمرده است؛ اول با کود الهی احیایش کن. 🥀"


# The currency outcome key of the drop table (not an inventory item).
CURRENCY_OUTCOME = "apple_normal"

# Height labels communicate magical progression visually (per level).
MAGIC_HEIGHT_LABELS = {
    0: "کوتاه",
    1: "نیمه‌بلند",
    2: "بلند",
    3: "خیلی بلند",
    4: "بسیار بلند",
    5: "اسطوره‌ای",
}


def magic_height_label(level: int, max_level: int = 5) -> str:
    """Persian height label for a magic level (extrapolates past max)."""
    return MAGIC_HEIGHT_LABELS.get(level) or MAGIC_HEIGHT_LABELS[max_level]


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
    magic_level: int = 0
    is_withered: bool = False

    @property
    def has_tree(self) -> bool:
        return self.planted_at is not None


@dataclass
class HarvestOutcome:
    """Result of one plot harvest (exactly one outcome per cycle)."""

    plot_index: int
    magic_level: int
    # Normal-Apple outcome: Apples credited to the wallet.
    apples: int = 0
    # Special-Apple outcome: the inventory item granted.
    item_key: Optional[str] = None
    item_label: Optional[str] = None
    # Presentation (message + caption may be configured per outcome key).
    message: Optional[str] = None
    # Caption for the celebration image (escalates with the drop rarity).
    caption: Optional[str] = None
    asset: Optional[str] = None

    @property
    def is_special(self) -> bool:
        return self.item_key is not None


@dataclass
class WitherResult:
    """One corruption outcome, used by the settlement hook to send DMs."""

    user_id: int
    plot_index: int
    invasion_id: int


class GardenService:
    """Planting, growth, harvest, magic and plot unlocks of gardens."""

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
    async def get_or_create(
        session: AsyncSession, user_id: int, *, for_update: bool = False
    ) -> Garden:
        """Load (or create) the player's garden.

        ``for_update=False`` (renders/read paths) skips the row lock — a
        read-only screen must not serialize against writers. Mutating
        flows pass ``for_update=True`` and hold the lock through commit.
        """
        stmt = (
            select(Garden)
            .where(Garden.user_id == user_id)
            .options(selectinload(Garden.plots))
        )
        if for_update:
            stmt = stmt.with_for_update()
        garden = await session.scalar(stmt)
        if garden:
            return garden
        if not for_update:
            # Re-check under the lock before inserting (creation race).
            garden = await session.scalar(stmt.with_for_update())
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
        # Corruption overrides every timestamp-derived lifecycle state.
        if bool(plot.is_withered):
            return PlotState.WITHERED
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
            withered = bool(plot.is_withered)
            state = PlotState.WITHERED if withered else cls._derive_state(plot, now)
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
                    magic_level=plot.magic_level,
                    is_withered=withered,
                )
            )
        return views

    # ------------------------------------------------------------------
    # Planting
    # ------------------------------------------------------------------
    @classmethod
    async def _get_plot(
        cls, session: AsyncSession, garden: Garden, plot_index: int
    ) -> GardenPlot:
        plot = await session.scalar(
            select(GardenPlot)
            .where(GardenPlot.garden_id == garden.user_id)
            .where(GardenPlot.plot_index == plot_index)
            .with_for_update()
        )
        if plot is None:
            raise ValueError("این قطعه در باغ شما وجود ندارد.")
        return plot

    @classmethod
    async def plant(
        cls,
        session: AsyncSession,
        user_id: int,
        plot_index: int,
    ) -> GardenPlot:
        """Plant one seedling into an empty plot (consumes the seedling)."""
        garden = await cls.get_or_create(session, user_id, for_update=True)
        plot = await cls._get_plot(session, garden, plot_index)
        if bool(plot.is_withered):
            raise ValueError(WITHERED_REFUSAL)
        if cls._derive_state(plot, datetime.now(timezone.utc)) != PlotState.EMPTY:
            raise ValueError("این قطعه خالی نیست!")

        seedlings = await SeedlingService.get_total(session, user_id)
        if seedlings < 1:
            raise ValueError("نهال کافی نداری! 🌱")

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
            raise ValueError("نهال کافی نداری! 🌱")
        inv.total -= 1
        await session.flush()

        from sib.services.player_xp import PlayerXPService

        await PlayerXPService.award(
            session,
            user_id=user_id,
            source="garden_plant",
            event_key=f"garden_plant:{garden.user_id}:{plot.plot_index}",
        )

        log_event(
            Events.GARDEN_EVENT,
            {"user_id": user_id, "action": "planted", "plot": plot_index},
        )
        return plot

    # ------------------------------------------------------------------
    # Harvest (drop-table roll per cycle)
    # ------------------------------------------------------------------
    @classmethod
    async def _harvest_plot(
        cls,
        session: AsyncSession,
        user_id: int,
        garden: Garden,
        plot: GardenPlot,
        rng=None,
    ) -> HarvestOutcome:
        """Collect one ripe plot: ONE roll on the level's drop table.

        Normal Apple outcome -> wallet credit (idempotent per cycle).
        Special Apple outcome -> inventory item. Either way the cycle
        advances exactly once inside the same transaction.
        """
        from sib.services.items import DropService, ItemService, item_label

        now = datetime.now(timezone.utc)
        if bool(plot.is_withered):
            raise ValueError(WITHERED_REFUSAL)
        if cls._derive_state(plot, now) != PlotState.FRUIT_READY:
            raise ValueError("هنوز میوه‌ای برای برداشت آماده نیست! 🍎")

        amount = await ConfigService.get_int(session, "garden_fruit_amount")
        fruit = await ConfigService.get_int(session, "garden_fruit_seconds")

        rolled = await DropService.roll(session, plot.magic_level, rng=rng)
        key, entry = rolled if rolled else (CURRENCY_OUTCOME, {})

        if key == CURRENCY_OUTCOME:
            # Normal Apples: currency credit (idempotent per cycle).
            await AppleWalletService.earn(
                session,
                user_id=user_id,
                amount=amount,
                reason="garden_harvest",
                ref_type="garden_plot",
                ref_id=plot.id,
                idempotency_key=f"garden:{plot.id}:{plot.cycle_count}",
            )
            outcome = HarvestOutcome(
                plot_index=plot.plot_index,
                magic_level=plot.magic_level,
                apples=amount,
                message=entry.get("message"),
            )
        else:
            # Special Apple: a real inventory item, same transaction.
            await ItemService.grant(session, user_id, key, quantity=1)
            outcome = HarvestOutcome(
                plot_index=plot.plot_index,
                magic_level=plot.magic_level,
                item_key=key,
                item_label=item_label(key),
                message=entry.get("message"),
                caption=entry.get("caption") or entry.get("message"),
                asset=entry.get("asset"),
            )

        # Restart the production cycle of the mature tree. The stable event
        # key uses the one-based harvest number, captured before any retry can
        # advance this plot again.
        harvest_number = int(plot.cycle_count) + 1
        plot.cycle_count += 1
        plot.fruit_ready_at = now + timedelta(seconds=fruit)
        await session.flush()

        from sib.services.player_xp import PlayerXPService

        await PlayerXPService.award(
            session,
            user_id=user_id,
            source="garden_harvest",
            event_key=f"garden:{plot.id}:{harvest_number}",
        )

        log_event(
            Events.GARDEN_EVENT,
            {
                "user_id": user_id,
                "action": "harvested",
                "plot": plot.plot_index,
                "apples": outcome.apples,
                "item": outcome.item_key,
                "magic_level": plot.magic_level,
            },
        )
        return outcome

    @classmethod
    async def harvest(
        cls,
        session: AsyncSession,
        user_id: int,
        plot_index: int,
        rng=None,
    ) -> HarvestOutcome:
        """Harvest a single plot (see ``_harvest_plot``)."""
        garden = await cls.get_or_create(session, user_id, for_update=True)
        plot = await cls._get_plot(session, garden, plot_index)
        return await cls._harvest_plot(session, user_id, garden, plot, rng=rng)

    @classmethod
    async def harvest_all(cls, session: AsyncSession, user_id: int, rng=None) -> List[HarvestOutcome]:
        """Harvest every ripe plot in ONE transaction (one re-render)."""
        garden = await cls.get_or_create(session, user_id, for_update=True)
        now = datetime.now(timezone.utc)
        ready = sorted(
            (p for p in garden.plots if cls._derive_state(p, now) == PlotState.FRUIT_READY),
            key=lambda p: p.plot_index,
        )
        if not ready:
            raise ValueError("هنوز میوه‌ای برای برداشت آماده نیست! 🍎")
        outcomes: List[HarvestOutcome] = []
        for plot in ready:
            plot = await cls._get_plot(session, garden, plot.plot_index)
            outcomes.append(
                await cls._harvest_plot(session, user_id, garden, plot, rng=rng)
            )
        return outcomes

    # ------------------------------------------------------------------
    # Magical progression (per-tree)
    # ------------------------------------------------------------------
    @classmethod
    async def magic_upgrade_cost(cls, session: AsyncSession, current_level: int) -> Optional[int]:
        """Magic Powder units for the NEXT level (None when maxed)."""
        max_level = await ConfigService.get_int(session, "garden_max_magic_level")
        if current_level >= max_level:
            return None
        costs = await ConfigService.get(session, "magic_powder_level_costs") or []
        if current_level < len(costs):
            return int(costs[current_level])
        # Defensive: configured fewer costs than levels -> last cost repeats.
        return int(costs[-1]) if costs else None

    @classmethod
    async def apply_magic(
        cls,
        session: AsyncSession,
        user_id: int,
        plot_index: int,
    ) -> int:
        """Spend Magic Powder to raise ONE tree's magic level by one.

        Returns the new level. Each tree progresses independently.
        """
        from sib.services.items import ItemService

        garden = await cls.get_or_create(session, user_id, for_update=True)
        plot = await cls._get_plot(session, garden, plot_index)
        if bool(plot.is_withered):
            raise ValueError(WITHERED_REFUSAL)
        if plot.planted_at is None:
            raise ValueError("در این قطعه درختی نیست که جادویی‌اش کنی! 🌱")

        cost = await cls.magic_upgrade_cost(session, plot.magic_level)
        if cost is None:
            raise ValueError("این درخت به حداکثر سطح جادویی رسیده است! ✨")

        # Raises a friendly Persian error when the inventory is short.
        await ItemService.consume(session, user_id, "magic_powder", cost)

        plot.magic_level += 1
        await session.flush()

        from sib.services.player_xp import PlayerXPService

        await PlayerXPService.award(
            session,
            user_id=user_id,
            source="garden_magic",
            event_key=(
                f"garden_magic:{garden.user_id}:{plot.plot_index}:{plot.magic_level}"
            ),
        )

        log_event(
            Events.GARDEN_EVENT,
            {
                "user_id": user_id,
                "action": "magic_upgraded",
                "plot": plot.plot_index,
                "level": plot.magic_level,
                "powder_cost": cost,
            },
        )
        return plot.magic_level

    # ------------------------------------------------------------------
    # Plot unlocks (individual, escalating prices)
    # ------------------------------------------------------------------
    @classmethod
    async def next_unlock_price(cls, session: AsyncSession, garden: Garden) -> Optional[int]:
        """Price of the next locked plot, or None when the cap is reached."""
        max_plots = await ConfigService.get_int(session, "garden_max_plots")
        next_index = garden.capacity_plots  # plots are contiguous
        if next_index >= max_plots:
            return None
        initial = await ConfigService.get_int(session, "garden_initial_plots")
        prices = await ConfigService.get(session, "garden_plot_prices") or []
        slot = next_index - initial
        if 0 <= slot < len(prices):
            return int(prices[slot])
        # Defensive: more unlockable plots than configured prices.
        return int(prices[-1]) * (2 ** max(0, slot - len(prices) + 1)) if prices else None

    @classmethod
    async def unlock_plot(
        cls,
        session: AsyncSession,
        user_id: int,
    ) -> int:
        """Buy the next locked plot individually. Returns the new capacity.

        Raises ``ValueError`` (refusal) before any write it cannot undo. The
        caller must let the error escape its transaction so nothing commits.
        """
        # User row first (lock policy rank 0), then the garden row.
        await lock_user_row(session, user_id)
        garden = await cls.get_or_create(session, user_id, for_update=True)

        price = await cls.next_unlock_price(session, garden)
        if price is None:
            raise ValueError("باغ شما به حداکثر ظرفیت رسیده است! 🌳")

        next_index = garden.capacity_plots
        try:
            debit = await AppleWalletService.debit_once(
                session,
                user_id,
                price,
                reason="garden_plot_unlock",
                ref_type="garden",
                ref_id=user_id,
                idempotency_key=f"garden_unlock:{user_id}:{next_index}",
            )
        except InsufficientFundsException:
            raise ValueError("سیب‌های شما برای باز کردن قطعه جدید کافی نیست! 🍎")
        if not debit.applied:
            # The unlock key for this plot index was already consumed by an
            # earlier charge: a replay must never open a free plot.
            raise ValueError("این قطعه قبلاً پرداخت شده است؛ صفحه باغ را دوباره باز کن. 🌳")

        session.add(GardenPlot(garden_id=user_id, plot_index=next_index))
        garden.capacity_plots += 1
        await session.flush()
        await session.refresh(garden, ["plots"])

        from sib.services.player_xp import PlayerXPService

        await PlayerXPService.award(
            session,
            user_id=user_id,
            source="garden_unlock",
            event_key=f"garden_unlock:{user_id}:{next_index}",
        )

        log_event(
            Events.GARDEN_EVENT,
            {
                "user_id": user_id,
                "action": "plot_unlocked",
                "plot_index": next_index,
                "price": price,
                "capacity": garden.capacity_plots,
            },
        )
        return garden.capacity_plots

    # ------------------------------------------------------------------
    # Invasion corruption (G2): withering + Divine Fertilizer restore
    # ------------------------------------------------------------------
    @classmethod
    async def apply_invasion_corruption(
        cls,
        session: AsyncSession,
        invasion: Inv2Invasion,
        *,
        rng: Optional[random.Random] = None,
    ) -> List[WitherResult]:
        """Wither one random plot of every zero-damage member of a lost invasion.

        Fairness rules (see docs/games/G2-RECON.md):

        - Only ``DEFEAT`` or an *announced* ``EXPIRED`` (``started_at`` set,
          i.e. the group really received a join button) corrupt gardens.
        - A user with ANY ``inv2_attacks`` row participated and is exempt —
          even when their rolled damage was zero.
        - At most one wither per user per UTC day and one per invasion; the
          ``garden_wither_events`` unique constraints are the hard backstop,
          the pre-check keeps ordinary retries off the integrity error.
        - Users without a garden, or whose plots are all withered, are no-ops.
          A garden is never lazily created from this background path.

        Returns one :class:`WitherResult` per affected player so the caller
        can deliver the private DM after committing.
        """
        rng = rng or random.Random()
        if invasion.status != InvasionStatus.DEFEAT.value and not (
            invasion.status == InvasionStatus.EXPIRED.value
            and invasion.started_at is not None
        ):
            return []

        now = datetime.now(timezone.utc)
        window_days = await ConfigService.get_int(
            session, "inv2_member_active_window_days"
        )
        active_member_ids = list(
            (
                await session.scalars(
                    select(Inv2GroupMember.user_id).where(
                        Inv2GroupMember.group_id == invasion.group_id,
                        Inv2GroupMember.last_seen_at
                        >= now - timedelta(days=window_days),
                    )
                )
            ).all()
        )
        if not active_member_ids:
            return []

        attacker_ids = set(
            (
                await session.scalars(
                    select(Inv2Attack.user_id).where(
                        Inv2Attack.invasion_id == invasion.id
                    )
                )
            ).all()
        )
        candidate_ids = sorted(
            uid for uid in active_member_ids if uid not in attacker_ids
        )

        # G4.5: configurable offline grace — only players seen within last N hours are eligible (default 4h).
        if candidate_ids:
            raw_grace = await ConfigService.get(session, "garden_wither_grace_hours")
            try:
                grace_hours = int(raw_grace or 4)
            except (TypeError, ValueError):
                grace_hours = 4
            if grace_hours <= 0:
                grace_hours = 4
            grace_cutoff = now - timedelta(hours=grace_hours)
            grace_ids = set(
                (
                    await session.scalars(
                        select(Inv2GroupMember.user_id).where(
                            Inv2GroupMember.group_id == invasion.group_id,
                            Inv2GroupMember.user_id.in_(candidate_ids),
                            Inv2GroupMember.last_seen_at >= grace_cutoff,
                        )
                    )
                ).all()
            )
            candidate_ids = [uid for uid in candidate_ids if uid in grace_ids]
            if not candidate_ids:
                return []

        # G4: limit to max 2 withered plots per invasion — shuffled targeted attack.
        if len(candidate_ids) > 2:
            rng.shuffle(candidate_ids)
            candidate_ids = sorted(candidate_ids[:2])

        today = now.date()
        results: List[WitherResult] = []
        for user_id in candidate_ids:
            # Multi-user settlement takes the per-user mutexes in ascending
            # id order (docs/lock-order.md), before any garden write.
            await lock_user_row(session, user_id)
            garden = await session.scalar(
                select(Garden)
                .where(Garden.user_id == user_id)
                .options(selectinload(Garden.plots))
                .with_for_update()
            )
            if garden is None:
                continue  # never create a garden from background settlement

            existing_event = await session.scalar(
                select(GardenWitherEvent.id)
                .where(
                    GardenWitherEvent.user_id == user_id,
                    or_(
                        GardenWitherEvent.wither_day == today,
                        GardenWitherEvent.invasion_id == invasion.id,
                    ),
                )
                .limit(1)
            )
            if existing_event is not None:
                continue  # daily cap reached, or this invasion already applied

            healthy_plots = list(
                (
                    await session.scalars(
                        select(GardenPlot)
                        .where(
                            GardenPlot.garden_id == user_id,
                            GardenPlot.is_withered.is_(False),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            if not healthy_plots:
                continue

            target = rng.choice(healthy_plots)
            target.is_withered = True
            session.add(
                GardenWitherEvent(
                    user_id=user_id,
                    invasion_id=invasion.id,
                    wither_day=today,
                )
            )
            await session.flush()
            results.append(
                WitherResult(
                    user_id=user_id,
                    plot_index=target.plot_index,
                    invasion_id=invasion.id,
                )
            )
            log_event(
                Events.GARDEN_EVENT,
                {
                    "user_id": user_id,
                    "action": "withered",
                    "plot": target.plot_index,
                    "invasion_id": invasion.id,
                },
            )
        return results

    @classmethod
    async def restore_plot(
        cls,
        session: AsyncSession,
        user_id: int,
        plot_index: int,
    ) -> GardenPlot:
        """Consume one Divine Fertilizer to restore a withered plot.

        Check-before-consume (like ``WarService.use_healing_potion``): a
        healthy plot or an empty inventory refuses before anything is spent.
        The tree's timestamps, cycle count and magic level are preserved, so
        the restored plot resumes its exact lifecycle.
        """
        from sib.services.items import ItemService

        await lock_user_row(session, user_id)
        garden = await cls.get_or_create(session, user_id, for_update=True)
        plot = await cls._get_plot(session, garden, plot_index)
        if not bool(plot.is_withered):
            raise ValueError(
                "این قطعه پژمرده نیست؛ کود الهی فقط برای قطعه‌های پژمرده کاربرد دارد."
            )
        owned = await ItemService.get_total(
            session, user_id, DIVINE_FERTILIZER_ITEM, for_update=True
        )
        if owned < 1:
            raise ValueError("کود الهی نداری! می‌تونی از فروشگاه تهیه‌اش کنی. 🧪")

        await ItemService.consume(session, user_id, DIVINE_FERTILIZER_ITEM, 1)
        plot.is_withered = False
        await session.flush()
        log_event(
            Events.GARDEN_EVENT,
            {
                "user_id": user_id,
                "action": "restored",
                "plot": plot_index,
            },
        )
        return plot
