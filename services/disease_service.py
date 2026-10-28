"""Transactional disease rules; no Telegram calls or scheduler integration.

The caller owns the transaction: mutations flush but never commit. Database
DATETIME values are stored as UTC (the project's convention), while the clock
and returned immunity timestamps use Asia/Tehran. SQLite and MariaDB can return
naive DATETIMEs even for timezone-aware columns, so those are interpreted as
UTC on read.
"""

import logging
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import UserDisease
from sib.services.config_store import ConfigService
from sib.services.lock_order import lock_user_row

TEHRAN = ZoneInfo("Asia/Tehran")
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(TEHRAN)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _immune(disease: UserDisease | None, now: datetime) -> bool:
    return bool(
        disease is not None
        and disease.immunity_until is not None
        and _as_utc(disease.immunity_until) > _as_utc(now)
    )


async def _config_int(session: AsyncSession, key: str) -> int:
    try:
        return await ConfigService.get_int(session, key)
    except (TypeError, ValueError, OverflowError):
        return int(ConfigService.default(key))


async def _percent(session: AsyncSession, key: str) -> int:
    return min(100, max(0, await _config_int(session, key)))


def _roll(percent: int) -> bool:
    return random.random() * 100 < percent


async def _disease_for_update(session: AsyncSession, user_id: int) -> UserDisease | None:
    # Lock the parent first, including when a disease row does not yet exist.
    # This serializes first infections/cures as well as updates on MariaDB.
    await session.flush()
    await lock_user_row(session, user_id)
    return await session.scalar(
        select(UserDisease)
        .where(UserDisease.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def is_sick(session: AsyncSession, user_id: int) -> bool:
    disease = await session.get(UserDisease, user_id)
    return disease is not None and disease.infection_level > 0


async def is_immune(session: AsyncSession, user_id: int) -> bool:
    disease = await session.get(UserDisease, user_id)
    return _immune(disease, _now())


async def infect_user(session: AsyncSession, user_id: int, source: str) -> bool:
    enabled = await ConfigService.get(session, "disease_enabled")
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"true", "1", "yes", "on"}
    if not enabled:
        return False

    disease = await _disease_for_update(session, user_id)
    now = _now()
    if _immune(disease, now) or (disease is not None and disease.infection_level > 0):
        return False

    chance = await _percent(session, "disease_demon_power_chance")
    if disease is None:
        disease = UserDisease(user_id=user_id)
        session.add(disease)
    disease.infection_level = 10
    disease.infected_at = _as_utc(now)
    disease.has_demon_power = _roll(chance)
    await session.flush()
    logger.info("disease infection applied: user_id=%s source=%s", user_id, source)
    return True


async def cure_user(session: AsyncSession, user_id: int) -> bool:
    disease = await _disease_for_update(session, user_id)
    hours = await _config_int(session, "disease_immunity_hours")
    if disease is None:
        disease = UserDisease(user_id=user_id)
        session.add(disease)
    disease.infection_level = 0
    disease.has_demon_power = False
    # Add elapsed hours in UTC so even a Tehran offset change cannot shorten
    # or extend the configured immunity period.
    disease.immunity_until = _as_utc(_now()) + timedelta(hours=hours)
    await session.flush()
    return True


async def get_status(session: AsyncSession, user_id: int) -> dict:
    disease = await session.get(UserDisease, user_id)
    if disease is None:
        return {
            "is_sick": False,
            "level": 0,
            "has_demon_power": False,
            "immunity_until": None,
        }
    immunity_until = disease.immunity_until
    return {
        "is_sick": disease.infection_level > 0,
        "level": disease.infection_level,
        "has_demon_power": disease.has_demon_power,
        "immunity_until": (
            _as_utc(immunity_until).astimezone(TEHRAN) if immunity_until else None
        ),
    }


async def process_initial_infection(
    session: AsyncSession, active_user_ids: list[int]
) -> int:
    """Sample distinct players without replacement, rounding the count down."""
    users = list(dict.fromkeys(active_user_ids))
    percent = await _percent(session, "disease_initial_infection_percent")
    count = len(users) * percent // 100
    if not count:
        return 0
    selected = random.sample(users, count)
    infected = 0
    # Multi-player operations acquire user locks in ascending order.
    for user_id in sorted(selected):
        infected += await infect_user(session, user_id, "initial")
    return infected


async def process_daily_infection_tick(
    session: AsyncSession, candidate_user_ids: list[int]
) -> int:
    """Roll independently for each candidate in ascending lock order."""
    chance = await _percent(session, "disease_daily_infection_chance")
    infected = 0
    for user_id in sorted(candidate_user_ids):
        if _roll(chance):
            infected += await infect_user(session, user_id, "daily_tick")
    return infected
