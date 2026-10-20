"""One card's single attack; the caller supplies all randomness."""

import random


def compute_damage(
    card_power: int, rng: random.Random, *, crit_chance: float = 0.10
) -> tuple[int, bool]:
    """Return (rounded damage, critical hit) using the Phase 1 formula.

    A zero or negative power cannot cause negative damage. Even for zero power,
    both draws are consumed to keep the seeded RNG sequence predictable.

    ``crit_chance`` exists for IV3 random events (``crit_bonus``); the 10%
    default keeps the arithmetic *and* the RNG consumption identical to the
    pre-IV3 behaviour, so a battle with no active event is bit-for-bit the
    same as before.
    """
    random_factor = rng.uniform(0.85, 1.15)
    is_crit = rng.random() < max(0.0, min(1.0, crit_chance))
    damage = round(max(0, card_power) * random_factor * (2 if is_crit else 1))
    return damage, is_crit
