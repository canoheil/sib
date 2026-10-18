"""Independent per-joiner victory reward rolls; no item grants here."""

import random

from .enums import RewardType


def roll_rewards(rng: random.Random) -> list[tuple[RewardType, int]]:
    """Always grant 5 gold; independently roll 15% diamond, 5% obsidian, 10% seedling.

    The caller decides *when* a joiner is eligible and performs persistence.
    This function only describes the outcome of three seeded random draws.
    """
    rewards = [(RewardType.GOLDEN_APPLE, 5)]
    if rng.random() < 0.15:
        rewards.append((RewardType.DIAMOND_APPLE, 1))
    if rng.random() < 0.05:
        rewards.append((RewardType.OBSIDIAN_APPLE, 1))
    if rng.random() < 0.10:
        rewards.append((RewardType.SEEDLING, 1))
    return rewards
