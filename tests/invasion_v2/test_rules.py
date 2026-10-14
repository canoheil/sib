"""Seeded, database-free rules for the Invasion v2 domain."""

from dataclasses import FrozenInstanceError
from random import Random

import pytest

from sib.services.invasion_v2 import (
    EnemyType,
    InvasionStatus,
    RegionSnapshot,
    RewardType,
    build_occurrence_key,
    choose_enemy_type,
    compute_damage,
    compute_enemy_hp,
    enemy_spec,
    find_region,
    is_duplicate_occurrence,
    lost_regions,
    roll_rewards,
)


class FixedRNG:
    """Deterministic boundary draws without relying on Random's internal state."""

    def __init__(self, *draws: float, factor: float = 1.0):
        self.draws = iter(draws)
        self.factor = factor

    def random(self) -> float:
        return next(self.draws)

    def uniform(self, start: float, end: float) -> float:
        assert (start, end) == (0.85, 1.15)
        return self.factor


@pytest.mark.parametrize(
    ("factor", "crit_draw", "expected"),
    [(0.85, 0.1, (85, False)), (1.15, 0.9, (115, False)),
     (0.85, 0.0, (170, True)), (1.15, 0.099, (230, True))],
)
def test_damage_ranges_and_crit(factor, crit_draw, expected):
    assert compute_damage(100, FixedRNG(crit_draw, factor=factor)) == expected


@pytest.mark.parametrize("power", [0, -10])
def test_zero_or_negative_power_still_consumes_both_draws(power):
    rng = FixedRNG(0.0, factor=1.15)
    assert compute_damage(power, rng) == (0, True)
    with pytest.raises(StopIteration):
        rng.random()


def test_damage_seed_is_replayable():
    first, second = Random(91), Random(91)
    assert [compute_damage(35, first) for _ in range(20)] == [
        compute_damage(35, second) for _ in range(20)
    ]


@pytest.mark.parametrize(
    ("enemy_type", "name", "multiplier", "weight"),
    [(EnemyType.SOLDIER, "شیاسیب پیاده", 1.0, .40),
     (EnemyType.SHADOW, "شیاسیب سایه", 1.6, .20),
     (EnemyType.COMMANDER, "شیاسیب سردار", 3.0, .20),
     (EnemyType.BRUTE, "شیاسیب سنگین", 5.0, .15),
     (EnemyType.KING, "شیاسیب پادشاه", 8.0, .05)],
)
def test_enemy_metadata_and_hp(enemy_type, name, multiplier, weight):
    spec = enemy_spec(enemy_type)
    assert (spec.name_fa, spec.power_multiplier, spec.spawn_weight) == (name, multiplier, weight)
    assert compute_enemy_hp(100, enemy_type, 4.0) == round(100 * multiplier * 4.0)
    assert compute_enemy_hp(0, enemy_type, 4.0) == 0
    assert compute_enemy_hp(-10, enemy_type, 4.0) == 0


def test_hp_rounding_clamps_negative_difficulty_and_rejects_nonfinite():
    assert compute_enemy_hp(0.5, EnemyType.SOLDIER, 1.0) == 0  # Python round: ties to even
    assert compute_enemy_hp(10, EnemyType.SOLDIER, -2.0) == 0
    with pytest.raises(ValueError):
        compute_enemy_hp(float("nan"), EnemyType.SOLDIER, 4.0)
    with pytest.raises(ValueError):
        compute_enemy_hp(10, EnemyType.SOLDIER, float("inf"))
    with pytest.raises(ValueError, match="Unknown enemy type"):
        compute_enemy_hp(10, "not-an-enemy", 4.0)


@pytest.mark.parametrize(
    ("draw", "enemy"),
    [(0.0, EnemyType.SOLDIER), (.399, EnemyType.SOLDIER),
     (.4, EnemyType.SHADOW), (.6, EnemyType.COMMANDER),
     (.8, EnemyType.BRUTE), (.95, EnemyType.KING), (1.0, EnemyType.KING)],
)
def test_enemy_type_weight_boundaries(draw, enemy):
    assert choose_enemy_type(FixedRNG(draw)) == enemy


def test_enemy_weights_over_ten_thousand_draws():
    rng = Random(2026)
    draws = [choose_enemy_type(rng) for _ in range(10_000)]
    for kind in EnemyType:
        assert abs(draws.count(kind) - 10_000 * enemy_spec(kind).spawn_weight) <= 10_000 * enemy_spec(kind).spawn_weight * .20


def test_reward_values_and_both_independent_rolls():
    assert roll_rewards(FixedRNG(.05, .01)) == [
        (RewardType.GOLDEN_APPLE, 3),
        (RewardType.DIAMOND_APPLE, 1),
        (RewardType.OBSIDIAN_APPLE, 1),
    ]
    assert roll_rewards(FixedRNG(.1, .02)) == [(RewardType.GOLDEN_APPLE, 3)]
    assert roll_rewards(FixedRNG(.9, .01)) == [
        (RewardType.GOLDEN_APPLE, 3), (RewardType.OBSIDIAN_APPLE, 1)
    ]
    assert roll_rewards(FixedRNG(.01, .9)) == [
        (RewardType.GOLDEN_APPLE, 3), (RewardType.DIAMOND_APPLE, 1)
    ]


def test_reward_distribution_ten_thousand_rolls_within_twenty_percent():
    rng = Random(314159)
    rewards = [roll_rewards(rng) for _ in range(10_000)]
    assert all((RewardType.GOLDEN_APPLE, 3) in row for row in rewards)
    for reward, expected in [(RewardType.DIAMOND_APPLE, 1000), (RewardType.OBSIDIAN_APPLE, 200)]:
        actual = sum(any(kind == reward for kind, _ in row) for row in rewards)
        assert abs(actual - expected) <= expected * .20
    replay = Random(314159)
    assert rewards == [roll_rewards(replay) for _ in range(10_000)]


def test_occurrence_key_idempotency_and_all_components():
    key = build_occurrence_key(-100, "moonfall_valley", 3600)
    assert key == "inv2:v1:group:-100:region:moonfall_valley:slot:3600"
    assert key == build_occurrence_key(-100, "moonfall_valley", 3600)
    assert is_duplicate_occurrence({key}, group_id=-100, region_code="moonfall_valley", slot_epoch=3600)
    assert not is_duplicate_occurrence((), group_id=-100, region_code="moonfall_valley", slot_epoch=3600)
    assert len({key, build_occurrence_key(-101, "moonfall_valley", 3600),
                build_occurrence_key(-100, "dark_marsh", 3600),
                build_occurrence_key(-100, "moonfall_valley", 7200)}) == 4


@pytest.mark.parametrize(
    ("group", "region", "epoch"),
    [(True, "dark_marsh", 0), ("x", "dark_marsh", 0),
     (1, "", 0), (1, "A", 0), (1, "x:y", 0), (1, "x" * 65, 0),
     (1, None, 0), (1, "dark_marsh", True), (1, "dark_marsh", -1),
     (1, "dark_marsh", "0")],
)
def test_occurrence_rejects_ambiguous_or_invalid_parts(group, region, epoch):
    with pytest.raises(ValueError):
        build_occurrence_key(group, region, epoch)


def test_region_snapshot_helpers_do_not_mutate_inputs():
    regions = (
        RegionSnapshot("moonfall_valley", "درّه ماه", "فرزندان ماه"),
        RegionSnapshot("ash_desert", "بیابان خاکستر", "بی‌طرف", True, 5),
    )
    assert find_region(regions, "ash_desert") is regions[1]
    assert find_region(iter(regions), "missing") is None
    assert lost_regions(regions) == (regions[1],)
    assert lost_regions(()) == ()
    with pytest.raises(FrozenInstanceError):
        regions[1].is_lost_to_enemy = False
    assert regions[1].is_lost_to_enemy


def test_wire_enum_values_match_schema():
    assert [status.value for status in InvasionStatus] == [
        "PENDING", "ACTIVE", "VICTORY", "DEFEAT", "EXPIRED"
    ]
    assert [reward.value for reward in RewardType] == [
        "golden_apple", "diamond_apple", "obsidian_apple"
    ]
