"""Daily, bounded Apple-reward boost for the golden hour.

The bonus is based on the action's UTC timestamp, converted explicitly to
Asia/Tehran. Only repeatable gameplay rewards that mint normal Apples are
eligible; transfers, administrative grants, one-off payouts, compensation and
selling inventory are deliberately not included.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
GOLDEN_HOUR_START_HOUR = 20
GOLDEN_HOUR_END_HOUR = 21  # exclusive
GOLDEN_HOUR_MULTIPLIER = 2

# Explicit allow-list: a new wallet reason is not boosted until it is reviewed.
# ``sib_claim`` is credited directly in SibClaimService rather than through
# AppleWalletService.earn, but it shares the same schedule.
GOLDEN_HOUR_APPLE_REASONS = frozenset(
    {
        "sib_claim",
        "sib_hug",
        "sib_hug_received",
        "sib_kiss",
        "sib_kiss_received",
        "apple_mine",
        "garden_harvest",
        "daily_chest",
        "daily_streak",
        "apple_battle_win",
        "friendly_match_reward",
        "castle_spring",
        "inv2_event_loot",
        "shop_chest_reward",
    }
)


def _as_utc(moment: datetime | None) -> datetime:
    """Use UTC for naive database timestamps; never consult server local time."""
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def is_golden_hour(now: datetime | None = None) -> bool:
    """Whether ``now`` falls in the daily [20:00, 21:00) Tehran window."""
    local = _as_utc(now).astimezone(TEHRAN)
    return GOLDEN_HOUR_START_HOUR <= local.hour < GOLDEN_HOUR_END_HOUR


def reward_multiplier(reason: str, now: datetime | None = None) -> int:
    """Return 2 for eligible rewards during golden hour, otherwise 1."""
    if reason not in GOLDEN_HOUR_APPLE_REASONS or not is_golden_hour(now):
        return 1
    return GOLDEN_HOUR_MULTIPLIER
