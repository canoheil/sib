"""Optional authoritative group-power provider for the inv2 scheduler.

Phase 4.5 supplies DbGroupPowerResolver using inv2_group_members populated
by group-interaction hooks. Registration occurs at bot startup; a missing
resolver still means no invasion spawn.
"""

from typing import Optional, Protocol


class GroupPowerResolver(Protocol):
    async def average_main_card_power(self, *, group_id: int, session) -> Optional[float]:
        """Average active members' main-card power, or None if unknown."""
        ...


_active_resolver: Optional[GroupPowerResolver] = None


def set_power_resolver(resolver: Optional[GroupPowerResolver]) -> None:
    global _active_resolver
    _active_resolver = resolver


def get_power_resolver() -> Optional[GroupPowerResolver]:
    return _active_resolver
