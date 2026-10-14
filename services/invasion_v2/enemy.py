"""Frozen enemy roster and pure weighted selection/HP calculations."""

import math
import random
from dataclasses import dataclass
from decimal import Decimal

from .enums import EnemyType


@dataclass(frozen=True)
class EnemySpec:
    type: EnemyType
    name_fa: str
    power_multiplier: float
    spawn_weight: float


_ENEMIES = (
    EnemySpec(EnemyType.SOLDIER, "شیاسیب پیاده", 1.0, 0.40),
    EnemySpec(EnemyType.SHADOW, "شیاسیب سایه", 1.6, 0.20),
    EnemySpec(EnemyType.COMMANDER, "شیاسیب سردار", 3.0, 0.20),
    EnemySpec(EnemyType.BRUTE, "شیاسیب سنگین", 5.0, 0.15),
    EnemySpec(EnemyType.KING, "شیاسیب پادشاه", 8.0, 0.05),
)


def enemy_spec(enemy_type: EnemyType) -> EnemySpec:
    """Look up an enemy without exposing mutable global configuration."""
    for spec in _ENEMIES:
        if spec.type == enemy_type:
            return spec
    raise ValueError(f"Unknown enemy type: {enemy_type!r}")


def choose_enemy_type(rng: random.Random) -> EnemyType:
    """Draw once from the Phase 1 weights (40/20/20/15/5 percent)."""
    # Decimal strings preserve the specified 0.40/0.20/... boundaries:
    # repeated binary-float subtraction misclassifies exact 0.60 and 0.95.
    draw = Decimal(str(rng.random()))
    threshold = Decimal("0")
    for spec in _ENEMIES:
        threshold += Decimal(str(spec.spawn_weight))
        if draw < threshold:
            return spec.type
    # Floating-point boundary protection; Random.random() is always < 1.
    return _ENEMIES[-1].type


def compute_enemy_hp(
    avg_group_card_power: float,
    enemy_type: EnemyType,
    difficulty_multiplier: float,
) -> int:
    """Round average power × type multiplier × difficulty, clamped to zero."""
    if not (math.isfinite(avg_group_card_power) and math.isfinite(difficulty_multiplier)):
        raise ValueError("Power and difficulty must be finite")
    multiplier = enemy_spec(enemy_type).power_multiplier
    return round(max(0.0, avg_group_card_power) * multiplier * max(0.0, difficulty_multiplier))
