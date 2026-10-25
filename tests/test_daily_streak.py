"""Daily chest streak: consecutive-day maths, milestones and exactly-once pay.

The streak is DERIVED from ``daily_chest_claims`` (no new table, no new
column), so these tests pin two things at once:

* the arithmetic — consecutive UTC days ending today, or ending yesterday
  while today is still unclaimed;
* the money — a milestone pays its Apples through the wallet ledger exactly
  once per day, inside the chest transaction, and never on a non-milestone day.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from sib.database.models import AppleTransaction, DailyChestClaim
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.daily_chest import CLAIM_REASON, DailyChestService, utc_day
from sib.database.models.daily_chest import REWARD_APPLES
from sib.tests.test_daily_chest import StubRng
from sib.services.daily_streak import (
    DEFAULT_STREAK_REWARDS,
    STREAK_KEY_FORMAT,
    STREAK_REASON,
    STREAK_REWARDS_KEY,
    DailyStreakService,
)
from sib.tests.test_parties import create_test_user

USER_ID = 771122


async def _claim_days(session, days: list[str], user_id: int = USER_ID) -> None:
    for day in days:
        session.add(
            DailyChestClaim(
                user_id=user_id,
                claim_day=day,
                reward_type="apples",
                quantity=10,
            )
        )
    await session.commit()


def _day(offset: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset)).date().isoformat()


# --------------------------------------------------------------------------- #
# Arithmetic
# --------------------------------------------------------------------------- #
async def test_no_claims_means_no_streak(db_session):
    user = await create_test_user(db_session, "tester")
    summary = await DailyStreakService.summary(db_session, user.id)

    assert summary.days == 0
    assert summary.bonus_apples == 0
    assert summary.next_milestone == 3
    assert summary.days_to_next == 3


async def test_consecutive_days_including_today(db_session):
    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-2), _day(-1), _day(0)], user.id)

    assert await DailyStreakService.count_streak(db_session, user.id, day=_day(0)) == 3


async def test_unclaimed_today_counts_the_streak_up_to_yesterday(db_session):
    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-3), _day(-2), _day(-1)], user.id)

    # Today is still open: the LIVE streak is three days and is on the line.
    assert await DailyStreakService.count_streak(db_session, user.id, day=_day(0)) == 3


async def test_gap_breaks_the_streak(db_session):
    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-4), _day(-3), _day(-1), _day(0)], user.id)

    # Day -2 is missing, so only today and yesterday count; the older pair
    # (-4, -3) belongs to a dead streak.
    assert await DailyStreakService.count_streak(db_session, user.id, day=_day(0)) == 2


async def test_missed_yesterday_and_today_resets_to_zero(db_session):
    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-5), _day(-4)], user.id)

    assert await DailyStreakService.count_streak(db_session, user.id, day=_day(0)) == 0


# --------------------------------------------------------------------------- #
# Milestones
# --------------------------------------------------------------------------- #
async def test_milestone_pays_exactly_once(db_session):
    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-2), _day(-1), _day(0)], user.id)

    summary = await DailyStreakService.summary(db_session, user.id, pay_bonus=True)
    assert summary.days == 3
    assert summary.bonus_apples == DEFAULT_STREAK_REWARDS[3]
    assert summary.next_milestone == 7
    assert summary.days_to_next == 4
    assert await AppleWalletService.get_balance(db_session, user.id) == DEFAULT_STREAK_REWARDS[3]

    # A replay (duplicate claim, retry, second worker) must not pay again.
    replay = await DailyStreakService.summary(db_session, user.id, pay_bonus=True)
    assert replay.days == 3
    assert replay.bonus_apples == DEFAULT_STREAK_REWARDS[3]  # same operation, replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == DEFAULT_STREAK_REWARDS[3]

    rows = (
        await db_session.scalars(
            select(AppleTransaction).where(
                AppleTransaction.user_id == user.id,
                AppleTransaction.idempotency_key
                == STREAK_KEY_FORMAT.format(user_id=user.id, day=_day(0)),
            )
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].reason == STREAK_REASON


async def test_non_milestone_day_pays_nothing(db_session):
    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-1), _day(0)], user.id)

    summary = await DailyStreakService.summary(db_session, user.id, pay_bonus=True)

    assert summary.days == 2
    assert summary.bonus_apples == 0
    assert summary.next_milestone == 3
    assert summary.days_to_next == 1
    assert await AppleWalletService.get_balance(db_session, user.id) == 0


async def test_read_only_summary_never_pays(db_session):
    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-2), _day(-1), _day(0)], user.id)

    summary = await DailyStreakService.summary(db_session, user.id)

    assert summary.days == 3
    assert summary.bonus_apples == 0
    assert await AppleWalletService.get_balance(db_session, user.id) == 0


async def test_seven_day_milestone_uses_the_ladder(db_session):
    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-offset) for offset in range(7)], user.id)

    summary = await DailyStreakService.summary(db_session, user.id, pay_bonus=True)

    assert summary.days == 7
    assert summary.bonus_apples == DEFAULT_STREAK_REWARDS[7]
    assert summary.next_milestone == 14


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
async def test_custom_ladder_is_honoured(db_session):
    user = await create_test_user(db_session, "tester")
    async with db_session.begin():
        await ConfigService.set(db_session, STREAK_REWARDS_KEY, {"2": 55})
    await _claim_days(db_session, [_day(-1), _day(0)], user.id)

    summary = await DailyStreakService.summary(db_session, user.id, pay_bonus=True)

    assert summary.days == 2
    assert summary.bonus_apples == 55
    assert summary.next_milestone is None  # every milestone is behind


@pytest.mark.parametrize(
    "broken",
    [
        None,
        "not-an-object",
        [],
        {"0": 10},
        {"-1": 10},
        {"x": 10},
        {"3": "many"},
        {"3": -5},
        {"3": True},
        {},
    ],
)
def test_broken_ladder_falls_back_to_defaults(broken):
    assert DailyStreakService.parse_rewards(broken) == DEFAULT_STREAK_REWARDS


def test_valid_ladder_is_parsed():
    assert DailyStreakService.parse_rewards({"2": 10, "5": 0}) == {2: 10, 5: 0}


# --------------------------------------------------------------------------- #
# Integration with the chest claim
# --------------------------------------------------------------------------- #
async def test_chest_claim_reports_and_pays_the_streak(db_session):
    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-2), _day(-1)], user.id)

    # A deterministic chest roll (Apples) so the two credits can be counted
    # separately without the reward category changing between runs.
    result = await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_APPLES, 10)
    )
    await db_session.commit()

    assert result.status == "claimed"
    assert result.streak_days == 3
    assert result.streak_bonus == DEFAULT_STREAK_REWARDS[3]
    assert result.streak_next_milestone == 7

    # Both credits exist: the chest reward and the streak milestone.
    reasons = (
        await db_session.scalars(
            select(AppleTransaction.reason).where(
                AppleTransaction.user_id == user.id
            )
        )
    ).all()
    assert CLAIM_REASON in reasons
    assert STREAK_REASON in reasons


async def test_first_day_chest_has_no_streak_line(db_session):
    from sib.services.daily_chest import format_open_result

    user = await create_test_user(db_session, "tester")
    result = await DailyChestService.claim(db_session, user.id)
    await db_session.commit()

    assert result.streak_days == 1
    text = format_open_result(result, datetime.now(timezone.utc))
    assert "استریک" in text  # day one still states the streak count
    assert "پاداش استریک" not in text


async def test_streak_line_carries_the_bonus(db_session):
    from sib.services.daily_chest import format_streak_line

    line = format_streak_line(3, bonus=30, next_milestone=7, days_to_next=4)
    assert "استریک ۳ روزه" in line
    assert "۳۰ سیب" in line
    assert "فردا هم بیا" in line

    # No streak, no line at all.
    assert format_streak_line(0) == ""

    # A plain day advertises the next milestone instead of a bonus.
    plain = format_streak_line(4, next_milestone=7, days_to_next=3)
    assert "پاداش استریک" not in plain
    assert "۳ روز دیگه" in plain


async def test_chest_open_result_includes_streak_summary(db_session):
    from sib.services.daily_chest import format_open_result

    user = await create_test_user(db_session, "tester")
    await _claim_days(db_session, [_day(-2), _day(-1)], user.id)

    result = await DailyChestService.claim(db_session, user.id)
    await db_session.commit()

    text = format_open_result(result, datetime.now(timezone.utc))
    assert "استریک ۳ روزه" in text
    assert "پاداش استریک" in text
    assert utc_day() in (result.claim.claim_day if result.claim else utc_day())
