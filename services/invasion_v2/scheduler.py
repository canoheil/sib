"""DB-only inv2 scheduler: no Telegram send, old invasion table, or reward grant."""

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Group, Inv2GroupState, Inv2Invasion, Inv2Region
from sib.services.config_store import ConfigService

from .circuit_breaker import is_group_available, maybe_permanently_disable
from .enemy import choose_enemy_type, compute_enemy_hp, enemy_spec
from .enums import InvasionStatus
from .occurrence import build_occurrence_key
from .power_resolver import get_power_resolver


def _utc(value: datetime) -> datetime:
    """Normalize MariaDB's naive UTC DATETIME fields for Python arithmetic."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def _enabled() -> bool:
    # Import lazily: MaintenanceService imports this scheduler at its call site.
    from sib.services.maintenance.service import MaintenanceService

    return await MaintenanceService._invasion_enabled()


class InvasionScheduler:
    """One sweep each maintenance tick; occurrences are durable and idempotent."""

    _missing_resolver_logged = False

    @staticmethod
    def choose_region(
        regions: Sequence[Inv2Region], current_region_code: str | None,
        rng: random.Random,
    ) -> Inv2Region:
        """70% lost (or current); 30% uniform among all regions.

        The most recent invasion's region stands in for the group's current
        region until Phase 4.5 defines an authoritative group-region mapping.
        With no such record, both branches choose uniformly across all regions.
        """
        if not regions:
            raise ValueError("No inv2 regions are seeded")
        lost = [region for region in regions if region.is_lost_to_enemy]
        current = next((r for r in regions if r.code == current_region_code), None)
        if rng.random() < 0.70:
            preferred = lost or ([current] if current is not None else regions)
            return rng.choice(preferred)
        return rng.choice(regions)

    @classmethod
    async def _finalize_due(cls, session: AsyncSession, now: datetime) -> None:
        due = list((await session.scalars(
            select(Inv2Invasion).where(
                Inv2Invasion.status.in_((InvasionStatus.PENDING.value, InvasionStatus.ACTIVE.value)),
                Inv2Invasion.ends_at.is_not(None),
                Inv2Invasion.ends_at <= now,
            ).order_by(Inv2Invasion.id).with_for_update()
        )).all())
        for invasion in due:
            if invasion.status == InvasionStatus.PENDING.value:
                invasion.status = InvasionStatus.EXPIRED.value
            elif invasion.enemy_hp_remaining <= 0:
                invasion.status = InvasionStatus.VICTORY.value
                region = await session.get(Inv2Region, invasion.region_code)
                if region is not None:
                    region.is_lost_to_enemy = False
            else:
                invasion.status = InvasionStatus.DEFEAT.value
                region = await session.get(Inv2Region, invasion.region_code)
                if region is not None:
                    region.is_lost_to_enemy = True
            invasion.finalized_at = now
            # No rewards here. Settlement belongs to a later phase.

    @classmethod
    async def _deactivate_stale_mutes(cls, session: AsyncSession, now: datetime) -> None:
        stale_ids = list((await session.scalars(
            select(Inv2GroupState.group_id).where(
                Inv2GroupState.muted_until.is_not(None),
                Inv2GroupState.muted_until < now - timedelta(days=7),
            )
        )).all())
        for group_id in stale_ids:
            await maybe_permanently_disable(group_id, session=session, now=now)

    @classmethod
    def _log_missing_resolver_once(cls) -> None:
        if not cls._missing_resolver_logged:
            logger.info("inv2 spawn skipped: no power resolver configured")
            cls._missing_resolver_logged = True

    @classmethod
    async def _spawn_if_due(cls, session: AsyncSession, now: datetime, rng: random.Random) -> None:
        # Avoid scanning groups without an installed authoritative resolver.
        if get_power_resolver() is None:
            cls._log_missing_resolver_once()
            return
        cls._missing_resolver_logged = False
        raw_interval = await ConfigService.get(session, "invasion_interval_seconds")
        interval = int(3600 if raw_interval is None else raw_interval)
        if interval <= 0:
            logger.error("inv2 spawn skipped: invasion_interval_seconds must be positive")
            return
        raw_difficulty = await ConfigService.get(session, "invasion_difficulty_multiplier")
        difficulty = float(4.0 if raw_difficulty is None else raw_difficulty)
        if not math.isfinite(difficulty) or difficulty <= 0:
            logger.error("inv2 spawn skipped: invasion_difficulty_multiplier must be positive and finite")
            return

        # Lock all active Group rows in a fixed order. On MariaDB this makes
        # overlapping maintenance workers serialize their global round-robin
        # decision. Query the last occurrence AFTER locking (current read).
        groups = list((await session.scalars(
            select(Group).where(Group.is_active.is_(True)).order_by(Group.id).with_for_update()
        )).all())
        if not groups:
            return
        last = await session.scalar(
            select(Inv2Invasion).order_by(
                Inv2Invasion.scheduled_at.desc(), Inv2Invasion.id.desc()
            ).limit(1).with_for_update()
        )
        if last is not None and now < _utc(last.scheduled_at) + timedelta(seconds=interval):
            return
        regions = list((await session.scalars(select(Inv2Region).order_by(Inv2Region.code))).all())
        if not regions:
            logger.warning("inv2 spawn skipped: no regions seeded")
            return

        next_id = last.group_id if last is not None else None
        ordered = [g for g in groups if next_id is None or g.id > next_id]
        if next_id is not None:
            ordered += [g for g in groups if g.id <= next_id]
        for group in ordered:
            if not await is_group_available(group.id, session=session, now=now):
                continue
            live = await session.scalar(
                select(Inv2Invasion.id).where(
                    Inv2Invasion.group_id == group.id,
                    Inv2Invasion.status.in_((InvasionStatus.PENDING.value, InvasionStatus.ACTIVE.value)),
                ).limit(1)
            )
            if live is not None:
                continue
            resolver = get_power_resolver()
            if resolver is None:  # resolver can be unregistered during a sweep
                cls._log_missing_resolver_once()
                return
            average = await resolver.average_main_card_power(group_id=group.id, session=session)
            if average is None:
                logger.debug("inv2 spawn skipped: group %s has no authoritative power", group.id)
                continue
            if not math.isfinite(average) or average <= 0:
                logger.debug("inv2 spawn skipped: group %s has no positive card power", group.id)
                continue

            previous_region = await session.scalar(
                select(Inv2Invasion.region_code).where(Inv2Invasion.group_id == group.id)
                .order_by(Inv2Invasion.scheduled_at.desc(), Inv2Invasion.id.desc()).limit(1)
            )
            region = cls.choose_region(regions, previous_region, rng)
            enemy_type = choose_enemy_type(rng)
            hp = compute_enemy_hp(average, enemy_type, difficulty)
            if hp <= 0:
                logger.debug("inv2 spawn skipped: group %s computed nonpositive HP", group.id)
                continue
            slot_epoch = int(now.timestamp() // interval * interval)
            occurrence_key = build_occurrence_key(group.id, region.code, slot_epoch)
            exists = await session.scalar(
                select(Inv2Invasion.id).where(Inv2Invasion.occurrence_key == occurrence_key)
            )
            if exists is not None:
                continue
            invasion = Inv2Invasion(
                group_id=group.id, region_code=region.code,
                enemy_type=enemy_type.value, enemy_name_fa=enemy_spec(enemy_type).name_fa,
                enemy_hp=hp, enemy_hp_remaining=hp, difficulty_multiplier=difficulty,
                status=InvasionStatus.PENDING.value, scheduled_at=now,
                # Phase 5 starts the join window when a group announcement is sent.
                started_at=None, ends_at=None, occurrence_key=occurrence_key,
            )
            session.add(invasion)
            await session.flush()
            region.last_invasion_id = invasion.id
            return  # at most one new invasion per global interval

    @classmethod
    async def tick(cls, *, now: datetime | None = None, rng: random.Random | None = None) -> None:
        """Finalize due rows, then schedule at most one eligible group.

        False/missing flag means *no session and no writes*, even when this
        method is called directly instead of via MaintenanceService.
        """
        if not await _enabled():
            return
        current = _utc(now or datetime.now(timezone.utc))
        async with get_session() as session:
            await cls._finalize_due(session, current)
            await cls._deactivate_stale_mutes(session, current)
            await cls._spawn_if_due(session, current, rng or random.Random())

    @classmethod
    async def recover(cls, *, now: datetime | None = None) -> int:
        """Expire past-due ACTIVE rows after downtime, without rewards/loss."""
        if not await _enabled():
            return 0
        current = _utc(now or datetime.now(timezone.utc))
        async with get_session() as session:
            overdue = list((await session.scalars(
                select(Inv2Invasion).where(
                    Inv2Invasion.status == InvasionStatus.ACTIVE.value,
                    Inv2Invasion.ends_at.is_not(None),
                    Inv2Invasion.ends_at <= current,
                ).order_by(Inv2Invasion.id).with_for_update()
            )).all())
            for invasion in overdue:
                invasion.status = InvasionStatus.EXPIRED.value
                invasion.finalized_at = current
            count = len(overdue)
        if count:
            logger.warning("inv2 startup recovery expired %s overdue invasion(s)", count)
        return count
