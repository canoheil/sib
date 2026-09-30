"""Garden capacity: individual plot unlocks + harvest-all (spec §garden)."""

from datetime import datetime, timedelta, timezone

import pytest

from sib.database.models import GardenPlot
from sib.services.apple_wallet import AppleWalletService
from sib.services.garden import GardenService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_user


class _CurrencyRng:
    def random(self):
        return 0.0


def _force_fruit_ready(plot):
    past = datetime.now(timezone.utc) - timedelta(days=5)
    plot.planted_at = past
    plot.mature_at = past + timedelta(days=1)
    plot.fruit_ready_at = past + timedelta(days=2)


# ---------------------------------------------------------------------------
# Unlocking plots one by one at escalating prices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlock_prices_escalate_per_plot(db_session):
    user = await create_test_user(db_session, "زمین‌دار ۱")
    garden = await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()

    from sib.services.config_store import ConfigService

    prices = await ConfigService.get(db_session, "garden_plot_prices")
    for expected_price in prices:
        await AppleWalletService.earn(
            db_session, user.id, expected_price, reason="test_grant"
        )
        await db_session.commit()

        capacity = await GardenService.unlock_plot(db_session, user.id)
        await db_session.commit()
        assert capacity == garden.capacity_plots
        assert await AppleWalletService.get_balance(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_garden_capped_at_max_plots(db_session):
    user = await create_test_user(db_session, "زمین‌دار ۲")
    uid = user.id
    garden = await GardenService.get_or_create(db_session, uid)
    await db_session.commit()

    from sib.services.config_store import ConfigService

    max_plots = await ConfigService.get_int(db_session, "garden_max_plots")
    total = sum(await ConfigService.get(db_session, "garden_plot_prices"))
    await AppleWalletService.earn(db_session, uid, total * 10, reason="test_grant")
    await db_session.commit()

    while await GardenService.next_unlock_price(db_session, garden) is not None:
        await GardenService.unlock_plot(db_session, uid)
        await db_session.commit()
        garden = await GardenService.get_or_create(db_session, uid)

    assert garden.capacity_plots == max_plots
    assert await GardenService.next_unlock_price(db_session, garden) is None
    with pytest.raises(ValueError):  # "حداکثر ظرفیت"
        await GardenService.unlock_plot(db_session, uid)
    await db_session.rollback()
    # Every unlock created exactly one real plot row.
    garden = await GardenService.get_or_create(db_session, uid)
    rows = garden.plots
    assert len(rows) == max_plots
    assert sorted(p.plot_index for p in rows) == list(range(max_plots))


@pytest.mark.asyncio
async def test_unlock_is_idempotent_per_plot(db_session):
    """The same slot can never be paid for twice."""
    user = await create_test_user(db_session, "زمین‌دار ۳")
    uid = user.id
    garden = await GardenService.get_or_create(db_session, uid)
    await AppleWalletService.earn(db_session, uid, 1000, reason="test_grant")
    await db_session.commit()

    await GardenService.unlock_plot(db_session, uid)
    await db_session.commit()
    balance_after = await AppleWalletService.get_balance(db_session, uid)

    # Rewind the capacity so the same slot index is attempted again.
    garden = await GardenService.get_or_create(db_session, uid)
    garden.capacity_plots -= 1
    plot = next(p for p in garden.plots if p.plot_index == 5)
    await db_session.delete(plot)
    await db_session.commit()

    await GardenService.unlock_plot(db_session, uid)
    await db_session.commit()
    assert await AppleWalletService.get_balance(db_session, uid) == balance_after


# ---------------------------------------------------------------------------
# Harvest-all: every ripe plot in ONE transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harvest_all_collects_every_ready_plot(db_session):
    user = await create_test_user(db_session, "برداشت‌کننده ۱")
    await SeedlingService.admin_adjust(db_session, user.id, 3, reason="test")
    garden = await GardenService.get_or_create(db_session, user.id)
    await GardenService.plant(db_session, user.id, 0)
    await GardenService.plant(db_session, user.id, 1)
    await GardenService.plant(db_session, user.id, 2)
    await db_session.commit()

    garden = await GardenService.get_or_create(db_session, user.id)
    for plot in garden.plots[:3]:  # ONLY the three planted plots
        _force_fruit_ready(plot)
    await db_session.commit()

    # Plot 3 stays unripe: plant it but keep the fruit in the future.
    await SeedlingService.admin_adjust(db_session, user.id, 1, reason="test")
    await GardenService.plant(db_session, user.id, 3)
    await db_session.commit()

    garden = await GardenService.get_or_create(db_session, user.id)
    unripe_plot = next(p for p in garden.plots if p.plot_index == 3)
    unripe_plot.fruit_ready_at = datetime.now(timezone.utc) + timedelta(hours=2)
    await db_session.commit()

    outcomes = await GardenService.harvest_all(db_session, user.id, rng=_CurrencyRng())
    await db_session.commit()

    assert len(outcomes) == 3  # only the ripe plots
    assert [o.plot_index for o in outcomes] == [0, 1, 2]
    assert all(o.apples > 0 for o in outcomes)

    garden = await GardenService.get_or_create(db_session, user.id)
    views = GardenService.view(garden)
    assert [v.cycle_count for v in views[:3]] == [1, 1, 1]
    assert views[3].cycle_count == 0  # the unripe plot was untouched


@pytest.mark.asyncio
async def test_harvest_all_rejected_when_nothing_is_ready(db_session):
    user = await create_test_user(db_session, "برداشت‌کننده ۲")
    await GardenService.get_or_create(db_session, user.id)
    await db_session.commit()

    with pytest.raises(ValueError):
        await GardenService.harvest_all(db_session, user.id)
    await db_session.rollback()
