"""One card's single attack; the caller supplies all randomness."""

import random


def compute_damage(card_power: int, rng: random.Random) -> tuple[int, bool]:
    """Return (rounded damage, critical hit) using the Phase 1 formula.

    A zero or negative power cannot cause negative damage. Even for zero power,
    both draws are consumed to keep the seeded RNG sequence predictable.
    """
    random_factor = rng.uniform(0.85, 1.15)
    is_crit = rng.random() < 0.10
    damage = round(max(0, card_power) * random_factor * (2 if is_crit else 1))
    return damage, is_crit
