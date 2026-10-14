"""Pure Invasion v2 rules; no database, Telegram, clock, or global RNG."""

from .damage import compute_damage
from .enemy import choose_enemy_type, compute_enemy_hp, enemy_spec
from .enums import EnemyType, InvasionStatus, RewardType
from .narrative import (
    render_announcement,
    render_defeat,
    render_expired,
    render_finisher,
    render_high_damage,
    render_join,
    render_victory,
)
from .occurrence import build_occurrence_key, is_duplicate_occurrence
from .regions import RegionSnapshot, find_region, lost_regions
from .rewards import roll_rewards

__all__ = [
    "EnemyType", "InvasionStatus", "RewardType", "RegionSnapshot",
    "compute_damage", "choose_enemy_type", "compute_enemy_hp", "enemy_spec",
    "find_region", "lost_regions", "roll_rewards",
    "build_occurrence_key", "is_duplicate_occurrence",
    "render_join", "render_high_damage", "render_announcement", "render_finisher",
    "render_victory", "render_defeat", "render_expired",
]
