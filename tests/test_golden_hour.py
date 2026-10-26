"""Golden-hour schedule and stable Apple-ledger credit behavior."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from sib.database.models import (
    AppleFriendship,
    AppleTransaction,
    DailyChestClaim,
    FriendshipStatus,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.daily_chest import DailyChestService
from sib.services.daily_streak import DailyStreakService
from sib.services.garden import GardenService
from sib.services.golden_hour import is_golden_hour, reward_multiplier
from sib.services.seedling import SeedlingService
from sib.services.sib_claim import SibClaimService, format_cooldown_message
from sib.services.sib_hug import SibHugService
from sib.services.sib_kiss import SibKissService
from sib.tests.test_parties import create_test_user

pytestmark = pytest.mark.golden_hour
GOLDEN_MOMENT = datetime(2026, 10, 26, 16, 45, tzinfo=timezone.utc)


class _FirstOutcomeRng:
    """Deterministically select the first configured outcome and low amount."""

    def random(self):
        return 0.0

    def randint(self, low, high):
        return low


# Tehran is UTC+03:30; the feature window is exactly 16:30–17:30 UTC.
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 26, 16, 29, 59, tzinfo=timezone.utc), False),
        (datetime(2026, 10, 26, 16, 30, tzinfo=timezone.utc), True),
        (datetime(2026, 10, 26, 17, 29, 59, tzinfo=timezone.utc), True),
        (datetime(2026, 10, 26, 17, 30, tzinfo=timezone.utc), False),
        (datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc), False),
    ],
)
def test_golden_hour_uses_tehran_boundaries(moment, expected):
    assert is_golden_hour(moment) is expected


def test_naive_moments_are_interpreted_as_utc():
    assert is_golden_hour(datetime(2026, 10, 26, 16, 30))


def test_only_allowlisted_rewards_are_doubled():
    assert reward_multiplier("daily_chest", GOLDEN_MOMENT) == 2
    assert reward_multiplier("sib_hug_received", GOLDEN_MOMENT) == 2
    assert reward_multiplier("item_sold", GOLDEN_MOMENT) == 1
    assert reward_multiplier("starter_pack", GOLDEN_MOMENT) == 1
    assert reward_multiplier("referral_inviter", GOLDEN_MOMENT) == 1
    assert reward_multiplier("referral_invitee", GOLDEN_MOMENT) == 1
    assert reward_multiplier("group_league_reward", GOLDEN_MOMENT) == 1
    assert reward_multiplier("seedling_sold", GOLDEN_MOMENT) == 1
    assert reward_multiplier("shop_chest_duplicate", GOLDEN_MOMENT) == 1
    outside = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
    assert reward_multiplier("daily_chest", outside) == 1


@pytest.mark.asyncio
async def test_ledger_keeps_original_multiplier_on_retry_after_golden_hour(db_session):
    user = await create_test_user(db_session, "ساعت طلایی")
    before_end = datetime(2026, 10, 26, 17, 29, 59, tzinfo=timezone.utc)
    after_end = datetime(2026, 10, 26, 17, 30, tzinfo=timezone.utc)
    key = f"golden-hour:{user.id}:daily-chest"

    await AppleWalletService.earn(
        db_session,
        user.id,
        10,
        reason="daily_chest",
        ref_type="daily_chest",
        ref_id=42,
        idempotency_key=key,
        occurred_at=before_end,
    )
    await db_session.commit()

    original = await AppleWalletService.get_transaction_by_idempotency_key(
        db_session, key
    )
    assert original is not None
    assert original.amount == 20
    assert original.reward_multiplier == 2
    assert await AppleWalletService.get_balance(db_session, user.id) == 20

    # Retry after 21:00 must replay the amount and multiplier from the first
    # ledger row, not the multiplier for the retry's current time.
    await AppleWalletService.earn(
        db_session,
        user.id,
        10,
        reason="daily_chest",
        ref_type="daily_chest",
        ref_id=42,
        idempotency_key=key,
        occurred_at=after_end,
    )
    await db_session.commit()

    rows = (
        await db_session.scalars(
            select(AppleTransaction).where(AppleTransaction.idempotency_key == key)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].amount == 20
    assert rows[0].reward_multiplier == 2
    assert await AppleWalletService.get_balance(db_session, user.id) == 20


@pytest.mark.asyncio
async def test_ineligible_wallet_reasons_remain_unmultiplied_in_golden_hour(db_session):
    user = await create_test_user(db_session, "جایزهٔ مستثنا")
    key = f"item-sold:{user.id}"

    await AppleWalletService.earn(
        db_session,
        user.id,
        25,
        reason="item_sold",
        idempotency_key=key,
        occurred_at=GOLDEN_MOMENT,
    )
    await db_session.commit()

    entry = await AppleWalletService.get_transaction_by_idempotency_key(
        db_session, key
    )
    assert entry is not None
    assert entry.amount == 25
    assert entry.reward_multiplier == 1
    assert await AppleWalletService.get_balance(db_session, user.id) == 25


@pytest.mark.asyncio
async def test_caller_cannot_apply_double_multiplier_to_excluded_reason(db_session):
    user = await create_test_user(db_session, "ضریب نامجاز")

    with pytest.raises(ValueError, match="does not match golden-hour policy"):
        await AppleWalletService.earn(
            db_session,
            user.id,
            25,
            reason="referral_invitee",
            reward_multiplier=2,
            occurred_at=GOLDEN_MOMENT,
        )


@pytest.mark.asyncio
async def test_direct_sib_claim_doubles_ledger_and_success_message(db_session, monkeypatch):
    from sib.services import sib_claim as claim_module

    user = await create_test_user(db_session, "سیب‌گو در ساعت طلایی")
    monkeypatch.setattr(claim_module, "roll_claim_amount", lambda: 8)

    message, awarded = await SibClaimService.claim(
        db_session, user.id, now=GOLDEN_MOMENT
    )
    await db_session.commit()

    entry = await db_session.scalar(
        select(AppleTransaction).where(
            AppleTransaction.user_id == user.id,
            AppleTransaction.reason == "sib_claim",
        )
    )
    assert awarded is True
    assert entry is not None
    assert entry.amount == 16
    assert entry.reward_multiplier == 2
    assert "۱۶" in message
    assert await AppleWalletService.get_balance(db_session, user.id) == 16


def test_sib_claim_cooldown_advertises_reward_for_the_next_eligible_time():
    two_minutes_before_golden_hour = datetime(
        2026, 10, 26, 16, 28, tzinfo=timezone.utc
    )
    message = format_cooldown_message(
        120, now=two_minutes_before_golden_hour
    )

    assert "۱۰" in message
    assert "۲۰" in message


@pytest.mark.asyncio
async def test_hug_and_kiss_double_friendship_bonus_for_both_players(db_session):
    sender = await create_test_user(db_session, "بوس و بغل‌کن")
    recipient = await create_test_user(db_session, "دوست بوس و بغل")
    db_session.add(
        AppleFriendship(
            user_id=sender.id,
            friend_id=recipient.id,
            status=FriendshipStatus.ACCEPTED,
        )
    )
    await db_session.flush()

    hug = await SibHugService.hug(
        db_session,
        sender_id=sender.id,
        target_id=recipient.id,
        message_key="golden-hug:1",
        now=GOLDEN_MOMENT,
    )
    kiss = await SibKissService.kiss(
        db_session,
        sender_id=sender.id,
        target_id=recipient.id,
        message_key="golden-kiss:1",
        now=GOLDEN_MOMENT,
    )
    await db_session.commit()

    assert hug.amount == 40  # (10 base + 10 friendship bonus) × 2
    assert hug.friendship_bonus is True
    assert kiss.amount == 40
    assert kiss.friendship_bonus is True
    rows = (
        await db_session.scalars(
            select(AppleTransaction).where(
                AppleTransaction.user_id.in_([sender.id, recipient.id]),
                AppleTransaction.reason.in_(
                    ["sib_hug", "sib_hug_received", "sib_kiss", "sib_kiss_received"]
                ),
            )
        )
    ).all()
    assert len(rows) == 4
    assert {row.amount for row in rows} == {40}
    assert {row.reward_multiplier for row in rows} == {2}
    assert await AppleWalletService.get_balance(db_session, sender.id) == 80
    assert await AppleWalletService.get_balance(db_session, recipient.id) == 80


@pytest.mark.asyncio
async def test_daily_chest_and_streak_double_and_report_actual_amounts(db_session):
    user = await create_test_user(db_session, "صندوق‌دار ساعت طلایی")
    db_session.add_all(
        [
            DailyChestClaim(
                user_id=user.id,
                claim_day="2026-10-24",
                reward_type="apples",
                quantity=10,
            ),
            DailyChestClaim(
                user_id=user.id,
                claim_day="2026-10-25",
                reward_type="apples",
                quantity=10,
            ),
        ]
    )
    await db_session.flush()

    result = await DailyChestService.claim(
        db_session,
        user.id,
        rng=_FirstOutcomeRng(),
        now=GOLDEN_MOMENT,
    )
    await db_session.commit()

    chest_reward = await db_session.scalar(
        select(AppleTransaction).where(
            AppleTransaction.user_id == user.id,
            AppleTransaction.reason == "daily_chest",
        )
    )
    streak_reward = await db_session.scalar(
        select(AppleTransaction).where(
            AppleTransaction.user_id == user.id,
            AppleTransaction.reason == "daily_streak",
        )
    )
    assert result.reward_type == "apples"
    assert result.quantity == 30
    assert chest_reward is not None and chest_reward.amount == 30
    assert chest_reward.reward_multiplier == 2
    assert result.streak_days == 3
    assert result.streak_bonus == 60
    assert streak_reward is not None and streak_reward.amount == 60
    assert streak_reward.reward_multiplier == 2
    assert await AppleWalletService.get_balance(db_session, user.id) == 90

    replay = await DailyStreakService.summary(
        db_session,
        user.id,
        day="2026-10-26",
        pay_bonus=True,
        now=datetime(2026, 10, 26, 17, 30, tzinfo=timezone.utc),
    )
    assert replay.bonus_apples == 60  # stored original multiplier survives retry
    assert await AppleWalletService.get_balance(db_session, user.id) == 90


@pytest.mark.asyncio
async def test_garden_harvest_doubles_and_returns_the_credited_amount(db_session):
    user = await create_test_user(db_session, "باغبان ساعت طلایی")
    await SeedlingService.admin_adjust(db_session, user.id, 1, reason="test_setup")
    plot = await GardenService.plant(db_session, user.id, 0)
    plot.planted_at = GOLDEN_MOMENT - timedelta(hours=48)
    plot.mature_at = GOLDEN_MOMENT - timedelta(hours=24)
    plot.fruit_ready_at = GOLDEN_MOMENT - timedelta(hours=1)
    await db_session.flush()

    outcome = await GardenService.harvest(
        db_session,
        user.id,
        0,
        rng=_FirstOutcomeRng(),
        now=GOLDEN_MOMENT,
    )
    await db_session.commit()

    base_amount = await ConfigService.get_int(db_session, "garden_fruit_amount")
    entry = await db_session.scalar(
        select(AppleTransaction).where(
            AppleTransaction.user_id == user.id,
            AppleTransaction.reason == "garden_harvest",
        )
    )
    assert outcome.apples == 2 * base_amount
    assert entry is not None and entry.amount == outcome.apples
    assert entry.reward_multiplier == 2
