"""Scheduler populates target region metadata for new four-wave Invasions.

This is a regression test for the defect that left ``target_region_*`` NULL
on every auto-scheduled battle, causing the UI to render "مشخص نشده".
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from sib.database.models import Faction, Invasion
from sib.services.maintenance.service import MaintenanceService
from sib.tests.test_parties import create_test_group

UTC = timezone.utc
SLOT = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _seed_moon_descendants_faction(session) -> None:
    """Insert the Moon Children faction with its canonical territory if absent.

    ``seed_default_data`` in the shared conftest does not seed factions or
    characters (those are seeded by ``init_db``, which the test suite avoids
    calling with the isolated test engine). Production always seeds factions
    on startup via ``seed_characters`` -> ``FactionService.seed_from_data``.
    """
    existing = await session.scalar(
        select(Faction.id).where(Faction.code == "moon_descendants")
    )
    if existing is not None:
        return
    session.add(
        Faction(
            code="moon_descendants",
            name_fa="فرزندان ماه",
            name_en="Moon Descendants",
            symbol="🌙",
            lore_fa="ساکنان درّه ماه.",
            territory_fa="درّه ماه (Moonfall Valley)",
            display_order=3,
            is_active=True,
        )
    )
    await session.commit()


async def _latest_invasion(session, group_id: int):
    session.expire_all()
    return await session.scalar(
        select(Invasion)
        .where(Invasion.group_id == group_id)
        .order_by(Invasion.id.desc())
        .limit(1)
    )


@pytest.mark.asyncio
async def test_scheduler_populates_target_region_name_from_faction_territory(db_session):
    await _seed_moon_descendants_faction(db_session)
    group = await create_test_group(db_session, "Invasion region test group")
    await db_session.commit()

    # Activate inside the slot window so the scheduler creates the battle.
    stats = await MaintenanceService.run_invasion_cycle(
        now=SLOT + timedelta(minutes=1)
    )
    assert stats["invasions_scheduled"] == 1

    invasion = await _latest_invasion(db_session, int(group.id))
    assert invasion is not None
    # Scheduler uses the existing optional columns; code is left NULL (no
    # territory-code schema exists), but the display name must be populated
    # from the faction's territory so the UI does not fall back to
    # "مشخص نشده" for newly created Invasions.
    assert invasion.target_region_code is None
    assert invasion.target_region_name_snapshot == "درّه ماه (Moonfall Valley)"


@pytest.mark.asyncio
async def test_scheduler_keeps_region_null_when_faction_is_missing(db_session):
    """Defensive: without the faction row, creation must still succeed with NULL region.

    This is the compatibility path for databases that predate faction
    territory seeding or where the row was removed by an operator. The UI
    falls back to "مشخص نشده" for those rows.
    """
    group = await create_test_group(db_session, "Invasion region-fallback group")
    await db_session.commit()

    stats = await MaintenanceService.run_invasion_cycle(
        now=SLOT + timedelta(minutes=1)
    )
    assert stats["invasions_scheduled"] == 1

    invasion = await _latest_invasion(db_session, int(group.id))
    assert invasion is not None
    assert invasion.target_region_code is None
    assert invasion.target_region_name_snapshot is None
    assert invasion.status == "ACTIVE"
