"""Read-only region snapshots; a later adapter will load them from inv2_regions.

Selection policy is deliberately not decided here: Phase 1 leaves the meaning
of a group's current region and the lost-region preference unresolved.
"""

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class RegionSnapshot:
    code: str
    name_fa: str
    controlling_faction: str
    is_lost_to_enemy: bool = False
    last_invasion_id: int | None = None


def find_region(regions: Iterable[RegionSnapshot], code: str) -> RegionSnapshot | None:
    """Return the matching region, or None; never read or update storage."""
    return next((region for region in regions if region.code == code), None)


def lost_regions(regions: Iterable[RegionSnapshot]) -> tuple[RegionSnapshot, ...]:
    """Return lost regions in input order, without mutating the input."""
    return tuple(region for region in regions if region.is_lost_to_enemy)
