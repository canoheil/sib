"""Card progression rules + cost table (pure logic, no Telegram)."""

import json
from pathlib import Path

import pytest

from sib.services import card_progression as cp
from sib.services.card_progression import (
    ATTRIBUTE_KEYS,
    ATTRIBUTE_STEP,
    MAX_CARD_LEVEL,
    CardAttributes,
    attributes_at_level,
    base_attributes,
    clamp_level,
    next_level,
)
from sib.services.card_upgrade import (
    CardUpgradeService,
    InvalidUpgradeCostTable,
    UpgradeCost,
    missing_for,
    parse_cost_table,
)
from sib.services.config_store import ConfigService

SEED = json.loads(
    (Path(__file__).resolve().parents[1] / "data" / "characters_seed.json").read_text(
        encoding="utf-8"
    )
)


class _Char:
    def __init__(self, power, speed, intelligence):
        self.power, self.speed, self.intelligence = power, speed, intelligence


# ---------------------------------------------------------------------------
# Attribute progression
# ---------------------------------------------------------------------------
def test_exactly_three_attributes_and_fixed_step():
    assert ATTRIBUTE_KEYS == ("power", "speed", "intelligence")
    assert [label for _k, _e, label in cp.CARD_ATTRIBUTES] == ["قدرت", "سرعت", "هوش"]
    assert set(CardAttributes(1, 2, 3).as_dict()) == set(ATTRIBUTE_KEYS)
    assert (cp.MIN_CARD_LEVEL, MAX_CARD_LEVEL, ATTRIBUTE_STEP) == (1, 10, 10)


def test_example_card_20_20_20_follows_the_spec_table():
    base = CardAttributes(20, 20, 20)
    totals = [attributes_at_level(base, lvl).total for lvl in range(1, 11)]
    assert totals == [60, 90, 120, 150, 180, 210, 240, 270, 300, 330]
    assert attributes_at_level(base, 2) == CardAttributes(30, 30, 30)
    assert attributes_at_level(base, 3) == CardAttributes(40, 40, 40)


@pytest.mark.parametrize("entry", SEED, ids=[e["code"] for e in SEED])
def test_every_real_card_keeps_its_level_one_base_and_gains_10_each(entry):
    char = _Char(entry["power"], entry["speed"], entry["intelligence"])
    base = base_attributes(char)
    # Level 1 is EXACTLY the existing definition.
    assert attributes_at_level(base, 1) == base
    assert base.total == 60
    previous = base
    for level in range(2, MAX_CARD_LEVEL + 1):
        current = attributes_at_level(base, level)
        assert current.power == previous.power + 10
        assert current.speed == previous.speed + 10
        assert current.intelligence == previous.intelligence + 10
        assert current.total == 60 + 30 * (level - 1)
        previous = current
    assert previous.total == 330
    # Deriving never mutates the shared definition.
    assert (char.power, char.speed, char.intelligence) == (
        entry["power"], entry["speed"], entry["intelligence"],
    )


def test_real_seed_level_one_distributions_are_unchanged():
    by_code = {e["code"]: (e["power"], e["speed"], e["intelligence"]) for e in SEED}
    assert by_code == {
        "sib_sorkh": (22, 20, 18),
        "sib_sabz": (16, 18, 26),
        "sib_talayi": (20, 24, 16),
        "sib_mahi": (14, 16, 30),
    }


def test_level_helpers_never_exceed_the_range():
    assert next_level(1) == 2 and next_level(9) == 10 and next_level(10) is None
    assert clamp_level(0) == 1 and clamp_level(99) == 10 and clamp_level("x") == 1
    base = CardAttributes(20, 20, 20)
    assert attributes_at_level(base, 99) == attributes_at_level(base, 10)


def test_no_manual_distribution_api_exists():
    from sib.database.models import PlayerCard
    from sib.services.card_upgrade import CardUpgradeService
    from sib.services.player_cards import PlayerCardService
    import inspect

    for retired in ("upgrade", "reallocate", "level_budget", "upgrade_cost"):
        assert not hasattr(PlayerCardService, retired)
    params = set(inspect.signature(CardUpgradeService.upgrade).parameters)
    assert params == {"session", "user_id", "card_id", "expected_level"}
    columns = set(PlayerCard.__table__.columns.keys())
    assert columns.isdisjoint(
        {"power", "speed", "intelligence", "boosted_power", "boosted_speed", "boosted_intelligence"}
    )


# ---------------------------------------------------------------------------
# Cost table
# ---------------------------------------------------------------------------
def _default_table():
    return parse_cost_table(ConfigService.default("card_upgrade_costs"))


# Level boundaries are data; this pins the audited default.
EXPECTED_DEFAULT = [
    (100, "apple_gold", 3),
    (150, "apple_gold", 5),
    (250, "apple_gold", 8),
    (400, "apple_diamond", 4),
    (600, "apple_diamond", 6),
    (800, "apple_diamond", 9),
    (1000, "apple_obsidian", 4),
    (1300, "apple_obsidian", 6),
    (1600, "apple_obsidian", 8),
]


def test_default_cost_table_matches_the_documented_table():
    table = _default_table()
    assert [(c.apples, c.item_key, c.item_quantity) for c in table] == EXPECTED_DEFAULT
    assert len(table) == MAX_CARD_LEVEL - 1


def test_default_cost_table_follows_gold_diamond_obsidian_progression():
    table = _default_table()
    tiers = [c.item_key for c in table]
    # Gold -> Diamond -> Obsidian, each tier contiguous and in order.
    assert tiers == ["apple_gold"] * 3 + ["apple_diamond"] * 3 + ["apple_obsidian"] * 3
    for start in (0, 3, 6):
        tier = table[start : start + 3]
        quantities = [c.item_quantity for c in tier]
        assert quantities == sorted(set(quantities))  # strictly increasing in a tier
    apples = [c.apples for c in table]
    assert apples == sorted(set(apples))  # Red/normal Apples always rise
    assert all(c.apples > 0 and c.item_quantity > 0 for c in table)


def test_default_table_is_priced_against_the_real_economy():
    """Total value per step (Apples + material at shop sell price) strictly rises."""
    from sib.services.config_store import CONFIG_DEFAULTS

    sell = CONFIG_DEFAULTS["item_sell_prices"]
    values = [c.apples + c.item_quantity * sell[c.item_key] for c in _default_table()]
    assert values == sorted(set(values))
    # The rarest tier is the most expensive in material value per unit.
    assert sell["apple_gold"] < sell["apple_diamond"] < sell["apple_obsidian"]


@pytest.mark.parametrize(
    "broken",
    [
        None,
        "text",
        [],
        EXPECTED_DEFAULT[:8],
        [{"apples": 1, "item": "apple_gold"}] * 9,
        [{"apples": 0, "item": "apple_gold", "quantity": 1}] * 9,
        [{"apples": 5, "item": "nope", "quantity": 1}] * 9,
        [{"apples": 5, "item": "apple_gold", "quantity": 0}] * 9,
    ],
)
def test_invalid_cost_tables_are_rejected(broken):
    with pytest.raises(InvalidUpgradeCostTable):
        parse_cost_table(broken)


@pytest.mark.asyncio
async def test_each_level_reads_its_own_row_and_max_level_has_none(db_session):
    for level in range(1, 10):
        cost = await CardUpgradeService.cost_for_level(db_session, level)
        assert (cost.apples, cost.item_key, cost.item_quantity) == EXPECTED_DEFAULT[level - 1]
    assert await CardUpgradeService.cost_for_level(db_session, 10) is None


@pytest.mark.asyncio
async def test_table_is_rebalanced_through_config_without_code_changes(db_session):
    rows = [{"apples": 7, "item": "apple_obsidian", "quantity": 2}] * 9
    await ConfigService.set(db_session, "card_upgrade_costs", rows)
    cost = await CardUpgradeService.cost_for_level(db_session, 1)
    assert cost == UpgradeCost(7, "apple_obsidian", 2)


@pytest.mark.asyncio
async def test_broken_config_falls_back_to_defaults(db_session):
    await ConfigService.set(db_session, "card_upgrade_costs", [{"apples": 1}])
    cost = await CardUpgradeService.cost_for_level(db_session, 4)
    assert (cost.apples, cost.item_key, cost.item_quantity) == EXPECTED_DEFAULT[3]


def test_missing_for_reports_exact_non_negative_shortfalls():
    cost = UpgradeCost(500, "apple_gold", 8)
    m = missing_for(cost, apples=380, item_quantity=6)
    assert (m.apples, m.item_quantity, m.is_empty) == (120, 2, False)
    assert missing_for(cost, 500, 8).is_empty
    assert missing_for(cost, 9999, 9999).is_empty
    only_items = missing_for(cost, 900, 1)
    assert (only_items.apples, only_items.item_quantity) == (0, 7)
