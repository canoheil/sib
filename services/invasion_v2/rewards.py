"""Independent per-joiner victory reward rolls; no item grants here."""

import random

from .enums import RewardType


def roll_rewards(rng: random.Random) -> list[tuple[RewardType, int]]:
    """Always grant 3 gold; independently roll 10% diamond, 2% obsidian.

    The caller decides *when* a joiner is eligible and performs persistence.
    This function only describes the outcome of two seeded random draws.
    """
    rewards = [(RewardType.GOLDEN_APPLE, 3)]
    if rng.random() < 0.10:
        rewards.append((RewardType.DIAMOND_APPLE, 1))
    if rng.random() < 0.02:
        rewards.append((RewardType.OBSIDIAN_APPLE, 1))
    return rewards
