"""Tests for the personal garden system (Phase 7).

Covers: creation (5 plots), planting, server-timestamp growth, fruit
production, harvest, production cycle restart, uncollected fruit
persistence, and apple-purchasable expansion.
"""

from datetime import datetime, timedelta, timezone

import pytest

from sib.database.models import PlotState
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.garden import GardenService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_user


async def _give_seedlings_admin(db_session, user_id, n):
    await SeedlingService.admin_adjust(db_session, user_id, n, reason="test_setup")
    await db_session.commit()


@pytest.mark.asyncio
async def test_garden_created_with_five_plots(db_session):
    user = await create_test_user(db_session, "باغبان ۱")
    garden = await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()

    assert garden.capacity_plots == 5
    assert len(garden.plots) == 5

    views = GardenService.view(garden)
    assert [v.state for v in views] == [PlotState.EMPTY] * 5


@pytest.mark.asyncio
async def test_planting_consumes_seedling_and_starts_growth(db_session):
    user = await create_test_user(db_session, "باغبان ۲")
    await _give_seedlings_admin(db_session, user.id, 2)

    garden = await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()

    await GardenService.plant(db_session, user.id, 0)
    await db_session.commit()

    assert await SeedlingService.get_total(db_session, user.id) == 1

    views = GardenService.view(garden)
    assert views[0].state == PlotState.GROWING
    assert views[0].mature_at is not None
    assert views[0].fruit_ready_at is not None
    assert views[0].seconds_to_next_stage > 0


@pytest.mark.asyncio
async def test_growth_is_server_timestamp_driven(db_session):
    """Derived state follows stored timestamps, not client input."""
    user = await create_test_user(db_session, "باغبان ۳")
    await _give_seedlings_admin(db_session, user.id, 1)

    garden = await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()
    plot = await GardenService.plant(db_session, user.id, 1)
    await db_session.commit()

    # Simulate the passage of time by rewinding the stored timestamps.
    past = datetime.now(timezone.utc) - timedelta(hours=48)
    plot.planted_at = past
    plot.mature_at = past + timedelta(hours=24)
    plot.fruit_ready_at = past + timedelta(hours=36)
    await db_session.commit()

    views = GardenService.view(garden)
    assert views[1].state == PlotState.FRUIT_READY

    # The intermediate mature stage is also derived from timestamps.
    plot.fruit_ready_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await db_session.commit()
    views = GardenService.view(garden)
    assert views[1].state == PlotState.MATURE


@pytest.mark.asyncio
async def test_harvest_pays_apples_and_restarts_cycle(db_session):
    user = await create_test_user(db_session, "باغبان ۴")
    await _give_seedlings_admin(db_session, user.id, 1)

    garden = await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()
    plot = await GardenService.plant(db_session, user.id, 2)
    await db_session.commit()

    past = datetime.now(timezone.utc) - timedelta(hours=48)
    plot.planted_at = past
    plot.mature_at = past + timedelta(hours=24)
    plot.fruit_ready_at = past + timedelta(hours=36)
    await db_session.commit()

    apples = await GardenService.harvest(db_session, user.id, 2)
    await db_session.commit()

    expected = await ConfigService.get_int(db_session, "garden_fruit_amount")
    assert apples == expected
    assert await AppleWalletService.get_balance(db_session, user.id) == expected

    views = GardenService.view(garden)
    assert views[2].state == PlotState.MATURE  # new production cycle running
    assert views[2].cycle_count == 1

    # Harvesting again before the next fruit is ready must fail.
    with pytest.raises(ValueError):
        await GardenService.harvest(db_session, user.id, 2)


@pytest.mark.asyncio
async def test_uncollected_fruit_does_not_stack(db_session):
    """Fruit waits for the player but never accumulates beyond one cycle."""
    user = await create_test_user(db_session, "باغبان ۵")
    await _give_seedlings_admin(db_session, user.id, 1)

    garden = await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()
    plot = await GardenService.plant(db_session, user.id, 3)
    await db_session.commit()

    very_long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    plot.planted_at = very_long_ago
    plot.mature_at = very_long_ago + timedelta(hours=24)
    plot.fruit_ready_at = very_long_ago + timedelta(hours=36)
    await db_session.commit()

    apples = await GardenService.harvest(db_session, user.id, 3)
    await db_session.commit()

    expected = await ConfigService.get_int(db_session, "garden_fruit_amount")
    assert apples == expected  # exactly one cycle's amount despite 30 days


@pytest.mark.asyncio
async def test_harvest_award_is_idempotent_per_cycle(db_session):
    user = await create_test_user(db_session, "باغبان ۶")
    await _give_seedlings_admin(db_session, user.id, 1)

    garden = await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()
    plot = await GardenService.plant(db_session, user.id, 4)
    await db_session.commit()

    past = datetime.now(timezone.utc) - timedelta(hours=48)
    plot.planted_at = past
    plot.mature_at = past + timedelta(hours=24)
    plot.fruit_ready_at = past + timedelta(hours=36)
    await db_session.commit()

    await GardenService.harvest(db_session, user.id, 4)
    await db_session.commit()
    balance_once = await AppleWalletService.get_balance(db_session, user.id)

    # Force the plot ripe again but rewind cycle_count to the SAME value:
    # the ledger idempotency key must block a duplicate award for a
    # production cycle that has already been paid out.
    plot.fruit_ready_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    plot.cycle_count -= 1
    await db_session.commit()

    await GardenService.harvest(db_session, user.id, 4)
    await db_session.commit()
    balance_twice = await AppleWalletService.get_balance(db_session, user.id)

    # The already-paid cycle must not pay twice.
    assert balance_twice == balance_once

    from sqlalchemy import select, func
    from sib.database.models import AppleTransaction

    dup_count = await db_session.scalar(
        select(func.count())
        .select_from(AppleTransaction)
        .where(AppleTransaction.reason == "garden_harvest")
        .where(AppleTransaction.ref_id == plot.id)
    )
    assert dup_count == 1  # exactly one payout per production cycle


@pytest.mark.asyncio
async def test_expansion_costs_apples_and_adds_plots(db_session):
    user = await create_test_user(db_session, "باغبان ۷")

    garden = await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()

    # Not enough apples -> rejected.
    with pytest.raises(ValueError):
        await GardenService.expand(db_session, user.id)

    await AppleWalletService.earn(db_session, user.id, 1000, reason="test_grant")
    await db_session.commit()

    cost = await ConfigService.get_int(db_session, "garden_expansion_cost_apples")
    step = await ConfigService.get_int(db_session, "garden_expansion_step")

    new_capacity = await GardenService.expand(db_session, user.id)
    await db_session.commit()

    assert new_capacity == 5 + step
    assert await AppleWalletService.get_balance(db_session, user.id) == 1000 - cost
    assert len(garden.plots) == 5 + step


@pytest.mark.asyncio
async def test_plant_requires_empty_plot_and_seedling(db_session):
    user = await create_test_user(db_session, "باغبان ۸")
    garden = await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()

    # No seedlings -> cannot plant.
    with pytest.raises(ValueError):
        await GardenService.plant(db_session, user.id, 0)

    await _give_seedlings_admin(db_session, user.id, 1)
    await GardenService.plant(db_session, user.id, 0)
    await db_session.commit()

    # Plot already occupied -> cannot plant again.
    await _give_seedlings_admin(db_session, user.id, 1)
    with pytest.raises(ValueError):
        await GardenService.plant(db_session, user.id, 0)
