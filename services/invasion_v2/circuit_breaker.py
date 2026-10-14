"""Per-group send-failure circuit breaker; no Telegram calls here."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.connection import get_session
from sib.database.models import Group, Inv2GroupState
from sib.services.config_store import ConfigService


def _utc(value: datetime) -> datetime:
    """MariaDB returns naive UTC DATETIME values; compare in UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def _enabled() -> bool:
    from sib.services.maintenance.service import MaintenanceService

    return await MaintenanceService._invasion_enabled()


async def _positive_config(session: AsyncSession, key: str, default: int) -> int:
    raw = await ConfigService.get(session, key)
    value = int(default if raw is None else raw)
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


async def record_send_failure(
    group_id: int, *, session: AsyncSession | None = None, now: datetime | None = None
) -> None:
    """Count a send error; at the configured threshold mute for 24h by default.

    Lock the Group row first so even a missing state row has a serialization
    point when two sends for the same group fail concurrently.
    """
    if not await _enabled():
        return
    if session is None:
        async with get_session() as db:
            await record_send_failure(group_id, session=db, now=now)
        return
    group = await session.scalar(select(Group).where(Group.id == group_id).with_for_update())
    if group is None:
        return
    state = await session.scalar(
        select(Inv2GroupState).where(Inv2GroupState.group_id == group_id).with_for_update()
    )
    if state is None:
        state = Inv2GroupState(group_id=group_id, consecutive_failures=0)
        session.add(state)
    threshold = await _positive_config(session, "invasion_circuit_breaker_threshold", 3)
    hours = await _positive_config(session, "invasion_mute_hours", 24)
    current = _utc(now or datetime.now(timezone.utc))
    state.consecutive_failures += 1
    state.last_failure_at = current
    if state.consecutive_failures >= threshold:
        state.muted_until = current + timedelta(hours=hours)


async def record_send_success(
    group_id: int, *, session: AsyncSession | None = None
) -> None:
    """Reset failures after any successful send (including an expired mute)."""
    if not await _enabled():
        return
    if session is None:
        async with get_session() as db:
            await record_send_success(group_id, session=db)
        return
    state = await session.scalar(
        select(Inv2GroupState).where(Inv2GroupState.group_id == group_id).with_for_update()
    )
    if state is not None:
        state.consecutive_failures = 0
        state.muted_until = None  # a successful send ends the old mute episode


async def is_group_available(
    group_id: int, *, session: AsyncSession | None = None, now: datetime | None = None
) -> bool:
    """Only active, unmuted Groups can be targeted for a send/spawn."""
    if not await _enabled():
        return False
    if session is None:
        async with get_session() as db:
            return await is_group_available(group_id, session=db, now=now)
    group = await session.get(Group, group_id)
    if group is None or not group.is_active:
        return False
    state = await session.get(Inv2GroupState, group_id)
    return state is None or state.muted_until is None or _utc(now or datetime.now(timezone.utc)) > _utc(state.muted_until)


async def maybe_permanently_disable(
    group_id: int, *, session: AsyncSession | None = None, now: datetime | None = None
) -> bool:
    """Deactivate an active Group if its last mute ended more than 7d ago."""
    if not await _enabled():
        return False
    if session is None:
        async with get_session() as db:
            return await maybe_permanently_disable(group_id, session=db, now=now)
    group = await session.scalar(select(Group).where(Group.id == group_id).with_for_update())
    if group is None or not group.is_active:
        return False
    state = await session.get(Inv2GroupState, group_id)
    if state is None or state.muted_until is None:
        return False
    if _utc(now or datetime.now(timezone.utc)) > _utc(state.muted_until) + timedelta(days=7):
        group.is_active = False
        return True
    return False
