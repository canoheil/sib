"""Wire values for the new invasion domain; no persistence dependencies."""

from enum import Enum


class EnemyType(str, Enum):
    SOLDIER = "soldier"
    SHADOW = "shadow"
    COMMANDER = "commander"
    BRUTE = "brute"
    KING = "king"


class InvasionStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    VICTORY = "VICTORY"
    DEFEAT = "DEFEAT"
    EXPIRED = "EXPIRED"


class RewardType(str, Enum):
    GOLDEN_APPLE = "golden_apple"
    DIAMOND_APPLE = "diamond_apple"
    OBSIDIAN_APPLE = "obsidian_apple"
