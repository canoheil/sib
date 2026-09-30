"""Magic trees: per-tree magic levels fueled by Magic Powder (spec §magic)."""

from datetime import datetime, timedelta, timezone

import pytest

from sib.database.models import PlotState
from sib.services.garden import GardenService
from sib.services.items import DropService, ItemService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_user


class _FixedRng:
    """Rng pinned to one float: 0.0 -> first key (currency), ~1.0 -> last."""

    def __init__(self, value: float):
        self._value = value

    def random(self):
        return self._value


async def _planted_garden(session, name: str, plots: int = 2):
    """A user with `plots` planted trees (seedlings granted accordingly)."""
    user = await create_test_user(session, name)
    await SeedlingService.admin_adjust(session, user.id, plots, reason="test")
    garden = await GardenService.get_or_create(session, user.id)
    for plot_index in range(plots):
        await GardenService.plant(session, user.id, plot_index)
    await session.commit()
    return user, garden


def _force_fruit_ready(plot):
    past = datetime.now(timezone.utc) - timedelta(days=5)
    plot.planted_at = past
    plot.mature_at = past + timedelta(days=1)
    plot.fruit_ready_at = past + timedelta(days=2)


# ---------------------------------------------------------------------------
# Magic upgrade: powder cost, per-tree independence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_magic_costs_powder_and_raises_level(db_session):
    user, _garden = await _planted_garden(db_session, "جادوگر ۱")
    await ItemService.grant(db_session, user.id, "magic_powder", 10)
    await db_session.commit()

    cost_before = await GardenService.magic_upgrade_cost(db_session, 0)
    level = await GardenService.apply_magic(db_session, user.id, 0)
    await db_session.commit()

    assert cost_before == 1  # magic_powder_level_costs[0]
    assert level == 1
    assert await ItemService.get_total(db_session, user.id, "magic_powder") == 10 - cost_before


@pytest.mark.asyncio
async def test_magic_costs_escalate_per_level(db_session):
    user, _garden = await _planted_garden(db_session, "جادوگر ۲", plots=1)
    await ItemService.grant(db_session, user.id, "magic_powder", 100)
    await db_session.commit()

    costs = []
    for expected_level in range(1, 6):  # up to garden_max_magic_level = 5
        costs.append(await GardenService.apply_magic(db_session, user.id, 0))
    await db_session.commit()

    assert costs == [1, 2, 3, 4, 5]
    # Powder spent: 1 + 2 + 3 + 5 + 8 = 19 (config magic_powder_level_costs).
    assert await ItemService.get_total(db_session, user.id, "magic_powder") == 100 - 19


@pytest.mark.asyncio
async def test_magic_rejected_without_powder(db_session):
    user, _garden = await _planted_garden(db_session, "جادوگر ۳", plots=1)
    uid = user.id
    await db_session.commit()

    with pytest.raises(ValueError):
        await GardenService.apply_magic(db_session, uid, 0)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_magic_is_per_tree_not_global(db_session):
    """Each tree progresses independently (old design: global level)."""
    user, garden = await _planted_garden(db_session, "جادوگر ۴", plots=1)
    await ItemService.grant(db_session, user.id, "magic_powder", 1)
    await db_session.commit()

    await GardenService.apply_magic(db_session, user.id, 0)
    await db_session.commit()

    garden = await GardenService.get_or_create(db_session, user.id)
    levels = {p.plot_index: p.magic_level for p in garden.plots}
    assert levels[0] == 1
    assert levels[1] == 0  # untouched neighbor stays at level 0


@pytest.mark.asyncio
async def test_magic_capped_at_max_level(db_session):
    user, _garden = await _planted_garden(db_session, "جادوگر ۵", plots=1)
    await ItemService.grant(db_session, user.id, "magic_powder", 100)
    await db_session.commit()

    uid = user.id
    for _ in range(5):
        await GardenService.apply_magic(db_session, uid, 0)

    assert await GardenService.magic_upgrade_cost(db_session, 5) is None
    with pytest.raises(ValueError):  # "حداکثر سطح جادویی"
        await GardenService.apply_magic(db_session, uid, 0)
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Magic level changes the harvest drop odds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_higher_magic_level_shifts_drop_odds(db_session):
    l0 = await DropService.weights_for_level(db_session, 0)
    l3 = await DropService.weights_for_level(db_session, 3)
    assert l3["apple_normal"] < l0["apple_normal"]
    assert l3["apple_obsidian"] > l0["apple_obsidian"]


@pytest.mark.asyncio
async def test_lucky_roll_on_magical_tree_grants_special_item(db_session):
    """A lucky roll yields an inventory item INSTEAD of apples."""
    user, garden = await _planted_garden(db_session, "جادوگر ۶", plots=1)
    await ItemService.grant(db_session, user.id, "magic_powder", 3)
    await GardenService.apply_magic(db_session, user.id, 0)
    await db_session.commit()

    garden = await GardenService.get_or_create(db_session, user.id)
    plot = next(p for p in garden.plots if p.plot_index == 0)
    _force_fruit_ready(plot)
    await db_session.commit()

    from sib.services.apple_wallet import AppleWalletService

    balance_before = await AppleWalletService.get_balance(db_session, user.id)
    outcome = await GardenService.harvest(
        db_session, user.id, 0, rng=_FixedRng(0.9999)
    )
    await db_session.commit()

    assert outcome.item_key == "apple_obsidian"  # last table key at weight tail
    assert outcome.apples == 0
    assert await ItemService.get_total(db_session, user.id, "apple_obsidian") == 1
    assert (
        await AppleWalletService.get_balance(db_session, user.id) == balance_before
    )
    # The production cycle still restarts exactly once.
    garden = await GardenService.get_or_create(db_session, user.id)
    views = GardenService.view(garden)
    assert views[0].cycle_count == 1
    assert views[0].state != PlotState.FRUIT_READY
