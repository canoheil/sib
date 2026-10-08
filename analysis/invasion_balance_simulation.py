#!/usr/bin/env python3
"""Offline, deterministic Invasion V1 balance matrix.

This analysis-only tool reads the repository's seeded Character definitions and
uses the existing pure card-progression rules, Invasion scheduler constants,
Brace constants, and reward defaults. It does not open a database, call
Telegram, or change production state. Outcome comparisons mirror
InvasionService.resolve's current integer comparisons.

Run from the repository root:
    python analysis/invasion_balance_simulation.py

The command writes analysis/invasion_balance_scenario_matrix.csv. All
probabilities are exact-enumeration results (printed to finite decimal
precision) under the explicitly synthetic equal-weight/card and Brace-uptake
assumptions listed in the report; they are not measured player forecasts.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
# The repository's historic namespace layout has root modules under ``sib``
# and a nested ``sib/bots`` package. Include both namespace roots when the
# script is launched directly from its own directory.
for import_root in (REPO_ROOT, REPO_ROOT.parent):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from sib.database.models.invasion import InvasionOutcome  # noqa: E402
from sib.services.card_progression import (  # noqa: E402
    ATTRIBUTE_STEP,
    MAX_CARD_LEVEL,
    MIN_CARD_LEVEL,
    card_attributes,
)
from sib.services.config_store import CONFIG_DEFAULTS  # noqa: E402
from sib.services.invasion import (  # noqa: E402
    BRACE_BASE_CONTRIBUTION,
    BRACE_POWER_DIVISOR,
)
from sib.services.invasion_rewards import APPLE_REWARD_CONFIG_KEYS  # noqa: E402
from sib.services.maintenance.service import MaintenanceService  # noqa: E402


GROUP_SIZES = (1, 2, 3, 5, 10, 20, 30, 50)
PARTICIPATION_PCTS = (10, 25, 50, 75, 100)
ENEMY_POWER_POINTS = (100, 125, 150, 175, 200)
ENEMY_POWER_RANGE = range(100, 201)
WAVE_COUNT = MaintenanceService.INVASION_WAVE_COUNT
UTC = timezone.utc

BRACE_PROFILES: dict[str, tuple[tuple[int, float], ...]] = {
    # (number of Braces over the event, probability for a participant)
    "none": ((0, 1.0),),
    "partial_50_one": ((0, 0.5), (1, 0.5)),
    "one_each": ((1, 1.0),),
    "mixed_50_per_wave": ((0, 0.25), (1, 0.5), (2, 0.25)),
    "full_two_waves": ((2, 1.0),),
}
BRACE_PROFILE_DESCRIPTIONS = {
    "none": "No Brace actions",
    "partial_50_one": "50% of participants take one Brace total",
    "one_each": "Every participant takes exactly one Brace total",
    "mixed_50_per_wave": "Each participant has 50% Brace uptake in each Wave independently",
    "full_two_waves": "Every participant Braces once in each of the two Waves",
}

OUTCOME_REWARDS = {
    InvasionOutcome.TOTAL_VICTORY: int(
        CONFIG_DEFAULTS[APPLE_REWARD_CONFIG_KEYS[InvasionOutcome.TOTAL_VICTORY]]
    ),
    InvasionOutcome.NARROW_DEFENSE: int(
        CONFIG_DEFAULTS[APPLE_REWARD_CONFIG_KEYS[InvasionOutcome.NARROW_DEFENSE]]
    ),
    InvasionOutcome.DEFEAT: int(
        CONFIG_DEFAULTS[APPLE_REWARD_CONFIG_KEYS[InvasionOutcome.DEFEAT]]
    ),
}


def load_seed_cards() -> list[dict]:
    return json.loads((REPO_ROOT / "data" / "characters_seed.json").read_text(encoding="utf-8"))


def snapshot_power(character: dict, level: int) -> int:
    """Ask the production pure progression helper for the snapshot Power."""
    card = SimpleNamespace(level=level)
    definition = SimpleNamespace(
        power=character["power"],
        speed=character["speed"],
        intelligence=character["intelligence"],
    )
    return int(card_attributes(card, definition).power)


def build_strength_profiles(seed_cards: list[dict]) -> dict[str, tuple[int, ...]]:
    by_code = {entry["code"]: entry for entry in seed_cards}
    return {
        # Low-end seed card at Level 1 is an explicit weak/new proxy.
        "weak_new": (snapshot_power(by_code["sib_mahi"], 1),),
        # Equal weighting is a transparent synthetic card-choice prior, not
        # an assertion about real ownership or Main Card selection.
        "average_l1_seed_mix": tuple(snapshot_power(entry, 1) for entry in seed_cards),
        "strong_l5_seed_mix": tuple(snapshot_power(entry, 5) for entry in seed_cards),
        "highly_developed_l10_seed_mix": tuple(
            snapshot_power(entry, MAX_CARD_LEVEL) for entry in seed_cards
        ),
        # No Main Card is explicitly supported by InvasionService.join.
        "no_main_card": (0,),
    }


STRENGTH_DESCRIPTIONS = {
    "weak_new": "Weak/new proxy: Moon Apple at Level 1 (seed-minimum Power)",
    "average_l1_seed_mix": "Average proxy: equal-weight four seeded cards at Level 1",
    "strong_l5_seed_mix": "Strong proxy: equal-weight four seeded cards at Level 5",
    "highly_developed_l10_seed_mix": "Highly developed proxy: equal-weight four seeded cards at Level 10",
    "no_main_card": "No Main Card: null snapshot, Brace uses power 0",
}


def production_enemy_snapshot(enemy_power: int) -> tuple[dict, list[dict]]:
    """Obtain and verify one event snapshot through the real scheduler builder."""
    if not 100 <= enemy_power <= 200:
        raise ValueError("enemy_power must be in the current V1 range 100..200")
    scheduled_at = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    interval = int(CONFIG_DEFAULTS["invasion_occurrence_interval_seconds"])
    duration = int(CONFIG_DEFAULTS["invasion_event_duration_seconds"])
    matches_played = enemy_power - MaintenanceService.INVASION_BASE_POWER
    computed, config, _spec, waves = MaintenanceService._build_invasion_snapshot(
        group_id=1,
        matches_played=matches_played,
        slot_epoch=int(scheduled_at.timestamp()),
        scheduled_at=scheduled_at,
        interval_seconds=interval,
        duration_seconds=duration,
    )
    assert computed == enemy_power
    assert sum(wave["enemy_power"] for wave in waves) == enemy_power
    assert len(waves) == WAVE_COUNT
    assert config["wave_durations_seconds"] == [duration // WAVE_COUNT] * WAVE_COUNT
    return config, waves


def validate_source_rules(seed_cards: list[dict]) -> None:
    """Fail loudly if the source rules/data no longer match this analysis."""
    assert MaintenanceService.INVASION_BASE_POWER == 100
    assert MaintenanceService.INVASION_MAX_ACTIVITY_BONUS == 100
    assert tuple(ENEMY_POWER_RANGE) == tuple(range(100, 201))
    assert WAVE_COUNT == 2
    assert (MIN_CARD_LEVEL, MAX_CARD_LEVEL, ATTRIBUTE_STEP) == (1, 10, 10)
    assert (BRACE_BASE_CONTRIBUTION, BRACE_POWER_DIVISOR) == (1, 10)
    assert (int(CONFIG_DEFAULTS["invasion_occurrence_interval_seconds"])) == 8 * 60 * 60
    assert (int(CONFIG_DEFAULTS["invasion_event_duration_seconds"])) == 20 * 60
    assert OUTCOME_REWARDS == {
        InvasionOutcome.TOTAL_VICTORY: 10,
        InvasionOutcome.NARROW_DEFENSE: 5,
        InvasionOutcome.DEFEAT: 1,
    }
    assert len(seed_cards) == 4
    assert all(
        entry["power"] + entry["speed"] + entry["intelligence"]
        == int(CONFIG_DEFAULTS["card_base_attribute_budget"])
        for entry in seed_cards
    )
    for played, expected in ((0, 100), (25, 125), (50, 150), (75, 175), (100, 200), (1000, 200)):
        assert MaintenanceService._enemy_power_for_activity(played) == expected
    for enemy_power in ENEMY_POWER_POINTS:
        production_enemy_snapshot(enemy_power)


def brace_amount(power_snapshot: int) -> int:
    """Same V1 expression as perform_defense_action, using its source constants."""
    return BRACE_BASE_CONTRIBUTION + power_snapshot // BRACE_POWER_DIVISOR


def _participant_contribution_distribution(
    powers: tuple[int, ...], brace_profile: str
) -> dict[int, float]:
    distribution: dict[int, float] = defaultdict(float)
    card_probability = 1.0 / len(powers)
    action_distribution = BRACE_PROFILES[brace_profile]
    for power in powers:
        per_brace = brace_amount(power)
        for brace_count, action_probability in action_distribution:
            distribution[brace_count * per_brace] += card_probability * action_probability
    return dict(distribution)


@lru_cache(maxsize=None)
def total_defense_distribution(
    participant_count: int,
    powers: tuple[int, ...],
    brace_profile: str,
) -> tuple[tuple[int, float], ...]:
    """Exact convolution of the synthetic per-participant discrete scenarios."""
    per_participant = _participant_contribution_distribution(powers, brace_profile)
    aggregate: dict[int, float] = {0: 1.0}
    for _ in range(participant_count):
        next_aggregate: dict[int, float] = defaultdict(float)
        for current_total, current_probability in aggregate.items():
            for contribution, contribution_probability in per_participant.items():
                next_aggregate[current_total + contribution] += (
                    current_probability * contribution_probability
                )
        aggregate = dict(next_aggregate)
    if not math.isclose(sum(aggregate.values()), 1.0, rel_tol=1e-10, abs_tol=1e-10):
        raise AssertionError("probability mass was lost during convolution")
    return tuple(sorted(aggregate.items()))


def outcome_for_defense(defense: int, enemy_power: int) -> str:
    """Mirror InvasionService.resolve's exact integer outcome comparisons."""
    if defense >= enemy_power:
        return InvasionOutcome.TOTAL_VICTORY
    if defense * 100 >= enemy_power * 75:
        return InvasionOutcome.NARROW_DEFENSE
    return InvasionOutcome.DEFEAT


def outcome_rates(
    defense_distribution: Iterable[tuple[int, float]], enemy_power: int
) -> dict[str, float]:
    rates = {
        InvasionOutcome.TOTAL_VICTORY: 0.0,
        InvasionOutcome.NARROW_DEFENSE: 0.0,
        InvasionOutcome.DEFEAT: 0.0,
    }
    for defense, probability in defense_distribution:
        rates[outcome_for_defense(defense, enemy_power)] += probability
    return rates


def expected_apples_per_participant(rates: dict[str, float]) -> float:
    return sum(rates[outcome] * OUTCOME_REWARDS[outcome] for outcome in OUTCOME_REWARDS)


def participants_from_rate(group_size: int, rate_pct: int) -> int:
    """Round to nearest whole participant, half up; CSV also carries effective rate."""
    return (group_size * rate_pct + 50) // 100


def scenario_rows(strength_profiles: dict[str, tuple[int, ...]]) -> Iterable[dict[str, object]]:
    for group_size in GROUP_SIZES:
        for target_rate in PARTICIPATION_PCTS:
            participants = participants_from_rate(group_size, target_rate)
            effective_rate = (100.0 * participants / group_size) if group_size else 0.0
            for strength_name, powers in strength_profiles.items():
                for brace_name in BRACE_PROFILES:
                    distribution = total_defense_distribution(participants, powers, brace_name)
                    expected_defense = sum(defense * probability for defense, probability in distribution)
                    contribution_per_participant = (
                        expected_defense / participants if participants else None
                    )
                    for enemy_power in ENEMY_POWER_POINTS:
                        rates = outcome_rates(distribution, enemy_power)
                        reward_per_participant = (
                            expected_apples_per_participant(rates) if participants else None
                        )
                        yield {
                            "group_size": group_size,
                            "target_participation_pct": target_rate,
                            "participants": participants,
                            "effective_participation_pct": f"{effective_rate:.2f}",
                            "enemy_power": enemy_power,
                            "narrow_defense_minimum": (75 * enemy_power + 99) // 100,
                            "strength_profile": strength_name,
                            "strength_profile_description": STRENGTH_DESCRIPTIONS[strength_name],
                            "seed_power_values": ";".join(str(value) for value in powers),
                            "brace_profile": brace_name,
                            "brace_profile_description": BRACE_PROFILE_DESCRIPTIONS[brace_name],
                            "waves": WAVE_COUNT,
                            "expected_brace_actions_per_participant": sum(
                                count * probability for count, probability in BRACE_PROFILES[brace_name]
                            ),
                            "expected_defense": f"{expected_defense:.6f}",
                            "expected_contribution_per_participant": (
                                "" if contribution_per_participant is None
                                else f"{contribution_per_participant:.6f}"
                            ),
                            "total_victory_rate_pct": f"{100 * rates[InvasionOutcome.TOTAL_VICTORY]:.6f}",
                            "narrow_defense_rate_pct": f"{100 * rates[InvasionOutcome.NARROW_DEFENSE]:.6f}",
                            "defeat_rate_pct": f"{100 * rates[InvasionOutcome.DEFEAT]:.6f}",
                            "expected_apples_per_participant": (
                                "" if reward_per_participant is None
                                else f"{reward_per_participant:.6f}"
                            ),
                            "expected_total_apples": (
                                "" if reward_per_participant is None
                                else f"{reward_per_participant * participants:.6f}"
                            ),
                        }


def write_csv(path: Path, rows: Iterable[dict[str, object]]) -> int:
    iterator = iter(rows)
    first = next(iterator, None)
    if first is None:
        raise ValueError("scenario matrix is empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(first), lineterminator="\n")
        writer.writeheader()
        writer.writerow(first)
        count += 1
        for row in iterator:
            writer.writerow(row)
            count += 1
    return count


def participation_table() -> str:
    header = "| Group size | " + " | ".join(f"{rate}%" for rate in PARTICIPATION_PCTS) + " |"
    separator = "|---:|" + "---:|" * len(PARTICIPATION_PCTS)
    rows = [header, separator]
    for size in GROUP_SIZES:
        counts = [participants_from_rate(size, rate) for rate in PARTICIPATION_PCTS]
        rows.append(f"| {size} | " + " | ".join(str(count) for count in counts) + " |")
    return "\n".join(rows)


def strength_table(strength_profiles: dict[str, tuple[int, ...]]) -> str:
    header = "| Proxy | Seed Power snapshots | Brace per Wave | Max over 2 Waves |"
    rows = [header, "|---|---:|---:|---:|"]
    for name, powers in strength_profiles.items():
        actions = sorted({brace_amount(power) for power in powers})
        max_total = max(2 * brace_amount(power) for power in powers)
        values = ", ".join(str(power) for power in powers)
        per_wave = ", ".join(str(amount) for amount in actions)
        rows.append(
            f"| {STRENGTH_DESCRIPTIONS[name]} | {values} | {per_wave} | {max_total} |"
        )
    return "\n".join(rows)


def print_source_summary(seed_cards: list[dict], strength_profiles: dict[str, tuple[int, ...]]) -> None:
    default_interval = int(CONFIG_DEFAULTS["invasion_occurrence_interval_seconds"])
    default_duration = int(CONFIG_DEFAULTS["invasion_event_duration_seconds"])
    print("V1 source-rule check: PASS")
    print(
        "Enemy power points via MaintenanceService._enemy_power_for_activity: "
        + ", ".join(
            f"{matches} matches→{MaintenanceService._enemy_power_for_activity(matches)}"
            for matches in (0, 25, 50, 75, 100)
        )
    )
    print(f"Waves={WAVE_COUNT}; default interval={default_interval}s; duration={default_duration}s")
    print("Seeded card Level-1 Power:", ", ".join(
        f"{entry['code']}={snapshot_power(entry, 1)}" for entry in seed_cards
    ))
    print("\nParticipants by nominal participation rate (half-up integer rounding):")
    print(participation_table())
    print("\nStrength/action values:")
    print(strength_table(strength_profiles))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "analysis" / "invasion_balance_scenario_matrix.csv",
        help="CSV path for the deterministic scenario matrix",
    )
    args = parser.parse_args()

    seed_cards = load_seed_cards()
    validate_source_rules(seed_cards)
    strength_profiles = build_strength_profiles(seed_cards)
    print_source_summary(seed_cards, strength_profiles)
    rows_written = write_csv(args.output, scenario_rows(strength_profiles))
    print(f"\nWrote {rows_written:,} deterministic rows to {args.output}")
    print("No database, Telegram, or application state was accessed.")


if __name__ == "__main__":
    main()
