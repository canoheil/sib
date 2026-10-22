"""Daily Sib Treasure Chest tests (task spec #3-#10).

One claim per player per UTC day, six reward categories delivered through
the canonical wallet/seedling/item/card services, DB-enforced duplicate
protection, validated weighted configuration, and a float-selectable
card jackpot (tested with an injected deterministic RNG — never by mass
random trials).
"""

import math
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_user

from sib.database.models import (
    AppleTransaction,
    Character,
    DailyChestClaim,
    PlayerCard,
    PlayerProgression,
    ReleaseStatus,
    SeedlingAward,
    SeedlingInventory,
    UserItem,
)
from sib.database.models.daily_chest import (
    REWARD_APPLES,
    REWARD_CARD,
    REWARD_DIAMOND,
    REWARD_GOLD,
    REWARD_OBSIDIAN,
    REWARD_SEEDLINGS,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.daily_chest import (
    CLAIM_REASON,
    ChestConfigError,
    DailyChestError,
    DailyChestService,
    format_reward_line,
    next_reset_at,
    progression_apples_bonus,
    roll_outcome,
    utc_day,
    validate_chest_config,
)
from sib.services.items import ITEM_CATALOG
from sib.services.sib_claim import MAX_CLAIM_AMOUNT, MIN_CLAIM_AMOUNT, SibClaimService

# ---------------------------------------------------------------------------
# Deterministic RNG (injectable; makes the rarest slice hittable without
# thousands of trials)
# ---------------------------------------------------------------------------
# Cumulative slices of the default normalized pool (sum = 1.0):
#   apples [0, 0.7) | seedlings [0.7, 0.9) | apple_gold [0.9, 0.96)
#   apple_diamond [0.96, 0.985) | apple_obsidian [0.985, 0.9995)
#   card [0.9995, 1.0)      <- 0.05% = 1 in 2,000
FORCE_RANDOM = {
    REWARD_APPLES: 0.0,
    REWARD_SEEDLINGS: 0.8,
    REWARD_GOLD: 0.95,
    REWARD_DIAMOND: 0.97,
    REWARD_OBSIDIAN: 0.99,
    REWARD_CARD: 0.99987,
}


class StubRng:
    """Minimal ``random.Random`` stand-in: fixed ``random()``/``randint()``."""

    def __init__(self, reward: str = REWARD_APPLES, quantity: int = None):
        self.value = FORCE_RANDOM[reward]
        self.quantity = quantity

    def random(self) -> float:
        return self.value

    def randint(self, a: int, b: int) -> int:
        return self.quantity if self.quantity is not None else a


async def _make_character(session, code: str, name: str = "قهرمان") -> Character:
    character = Character(
        code=code,
        name_fa=name,
        is_active=True,
        release_status=ReleaseStatus.RELEASED,
        shop_enabled=True,
        price=500,
    )
    session.add(character)
    await session.commit()
    return character


# ---------------------------------------------------------------------------
# Configuration validation (task spec #5/9)
# ---------------------------------------------------------------------------
def test_default_config_is_valid_and_normalizes_to_one():
    from sib.services.config_store import CONFIG_DEFAULTS

    config = validate_chest_config(
        CONFIG_DEFAULTS["daily_chest_weights"],
        CONFIG_DEFAULTS["daily_chest_quantities"],
        CONFIG_DEFAULTS["daily_chest_progression"],
    )
    assert math.isclose(sum(config.weights.values()), 1.0)
    # Strict rising rarity order of the DEFAULT probabilities.
    order = [
        config.weights[REWARD_APPLES],
        config.weights[REWARD_SEEDLINGS],
        config.weights[REWARD_GOLD],
        config.weights[REWARD_DIAMOND],
        config.weights[REWARD_OBSIDIAN],
        config.weights[REWARD_CARD],
    ]
    assert order == sorted(order, reverse=True)
    assert order[0] > order[-1] > 0


def test_validation_rejects_unknown_reward_identifiers():
    with pytest.raises(ChestConfigError, match="unknown reward"):
        validate_chest_config({"apples": 1, "golden_apple": 1}, {"apples": 1})
    with pytest.raises(ChestConfigError, match="unknown reward"):
        validate_chest_config({"apples": 1}, {"apples": 1, "mystery": 1})


def test_validation_rejects_negative_and_non_finite_weights():
    with pytest.raises(ChestConfigError, match="negative"):
        validate_chest_config({"apples": -1, "card": 1}, {"apples": 1, "card": 1})
    with pytest.raises(ChestConfigError, match="finite"):
        validate_chest_config({"apples": float("nan")}, {"apples": 1})
    with pytest.raises(ChestConfigError, match="finite"):
        validate_chest_config({"apples": float("inf")}, {"apples": 1})
    with pytest.raises(ChestConfigError, match="finite"):
        validate_chest_config({"apples": "70"}, {"apples": 1})


def test_validation_rejects_empty_or_zero_total_distribution():
    with pytest.raises(ChestConfigError):
        validate_chest_config({}, {"apples": 1})
    with pytest.raises(ChestConfigError, match="positive total"):
        validate_chest_config({"apples": 0, "card": 0}, {"apples": 1, "card": 1})


def test_validation_rejects_invalid_quantities():
    with pytest.raises(ChestConfigError, match="quantity"):
        validate_chest_config({"apples": 1}, {"apples": 0})
    with pytest.raises(ChestConfigError, match="quantity"):
        validate_chest_config({"apples": 1}, {"apples": -3})
    with pytest.raises(ChestConfigError, match="range"):
        validate_chest_config({"apples": 1}, {"apples": {"min": 5, "max": 2}})
    with pytest.raises(ChestConfigError, match="range"):
        validate_chest_config({"apples": 1}, {"apples": {"min": 0, "max": 2}})
    with pytest.raises(ChestConfigError):
        validate_chest_config({"apples": 1}, {"apples": {"min": 1.5, "max": 2}})


def test_validation_requires_quantity_for_every_rewardable_outcome():
    with pytest.raises(ChestConfigError, match="missing quantity"):
        validate_chest_config({"apples": 1, "card": 1}, {"apples": 1})


def test_validation_rejects_invalid_progression_scaling():
    with pytest.raises(ChestConfigError, match="tier"):
        validate_chest_config(
            {"apples": 1}, {"apples": 1}, {"enabled": True, "apples_per_tier": 5, "tier_levels": [0]}
        )
    with pytest.raises(ChestConfigError, match="tier"):
        validate_chest_config(
            {"apples": 1}, {"apples": 1}, {"apples_per_tier": 5, "tier_levels": [31]}
        )
    with pytest.raises(ChestConfigError, match="apples_per_tier"):
        validate_chest_config(
            {"apples": 1}, {"apples": 1}, {"apples_per_tier": -1, "tier_levels": [10]}
        )


# ---------------------------------------------------------------------------
# Selection (task spec #5/10)
# ---------------------------------------------------------------------------
def test_roll_outcome_rolls_each_category_deterministically():
    config = validate_chest_config(
        {"apples": 70.0, "seedlings": 20.0, "apple_gold": 6.0,
         "apple_diamond": 2.5, "apple_obsidian": 1.45, "card": 0.05},
        {"apples": {"min": 15, "max": 25}, "seedlings": {"min": 1, "max": 2},
         "apple_gold": {"min": 1, "max": 2}, "apple_diamond": 1,
         "apple_obsidian": 1, "card": 1},
    )
    for reward in (REWARD_APPLES, REWARD_SEEDLINGS, REWARD_GOLD,
                   REWARD_DIAMOND, REWARD_OBSIDIAN, REWARD_CARD):
        rolled, _quantity = roll_outcome(config, StubRng(reward))
        assert rolled == reward, f"stub for {reward} selected {rolled}"


def test_extremely_rare_card_stay_selectable_without_mass_trials():
    """The 0.05% slice must remain hittable (float selection, no rounding)."""
    config = validate_chest_config(
        {"apples": 70.0, "seedlings": 20.0, "apple_gold": 6.0,
         "apple_diamond": 2.5, "apple_obsidian": 1.45, "card": 0.05},
        {"apples": 1, "seedlings": 1, "apple_gold": 1, "apple_diamond": 1,
         "apple_obsidian": 1, "card": 1},
    )
    assert config.weights[REWARD_CARD] > 0
    # 0.99987 is inside the card slice [0.9995, 1.0): ONE deterministic roll.
    assert roll_outcome(config, StubRng(REWARD_CARD))[0] == REWARD_CARD
    # The very top of the float range also lands on the card.
    assert roll_outcome(config, StubRng(REWARD_CARD, quantity=1))[0] == REWARD_CARD


def test_roll_outcome_respects_quantity_ranges():
    config = validate_chest_config(
        {"apples": 1.0},
        {"apples": {"min": 15, "max": 25}},
    )
    assert roll_outcome(config, StubRng(REWARD_APPLES, quantity=25)) == (REWARD_APPLES, 25)
    assert roll_outcome(config, StubRng(REWARD_APPLES, quantity=15)) == (REWARD_APPLES, 15)


def test_roll_outcome_on_empty_deliverable_pool_fails_clearly():
    config = validate_chest_config({"card": 1.0}, {"card": 1})
    with pytest.raises(ChestConfigError, match="deliverable"):
        roll_outcome(config, StubRng(REWARD_CARD), deliverable=())


# ---------------------------------------------------------------------------
# Daily boundary (task spec #3)
# ---------------------------------------------------------------------------
def test_daily_boundary_is_the_utc_calendar_day():
    now = datetime(2026, 10, 8, 21, 30, tzinfo=timezone.utc)
    assert utc_day(now) == "2026-10-08"
    reset = next_reset_at(now)
    assert reset == datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)


def test_progression_bonus_at_level_boundaries():
    config = validate_chest_config(
        {"apples": 1.0},
        {"apples": 1},
        {"enabled": True, "apples_per_tier": 5, "tier_levels": [10, 20]},
    )
    assert progression_apples_bonus(1, config) == 0
    assert progression_apples_bonus(9, config) == 0
    assert progression_apples_bonus(10, config) == 5   # first tier
    assert progression_apples_bonus(19, config) == 5
    assert progression_apples_bonus(20, config) == 10  # second tier
    assert progression_apples_bonus(30, config) == 10
    disabled = validate_chest_config(
        {"apples": 1.0}, {"apples": 1}, {"enabled": False, "apples_per_tier": 5, "tier_levels": [1]}
    )
    assert progression_apples_bonus(30, disabled) == 0


# ---------------------------------------------------------------------------
# Persian result presentation (task spec #8)
# ---------------------------------------------------------------------------
def test_result_messages_distinguish_all_six_categories():
    lines = {
        reward: format_reward_line(reward, 2)
        for reward in (REWARD_APPLES, REWARD_SEEDLINGS, REWARD_GOLD,
                       REWARD_DIAMOND, REWARD_OBSIDIAN, REWARD_CARD)
    }
    assert len(set(lines.values())) == 6
    assert lines[REWARD_APPLES] == "🍎 **۲ سیب معمولی** گیرت اومد!"
    assert "سیب معمولی" in lines[REWARD_APPLES]
    assert "نهال" in lines[REWARD_SEEDLINGS]
    assert "سیب طلایی" in lines[REWARD_GOLD]
    assert "سیب الماسی" in lines[REWARD_DIAMOND]
    assert "سیب ابسیدینی" in lines[REWARD_OBSIDIAN]
    assert "کارت" in lines[REWARD_CARD]


# ---------------------------------------------------------------------------
# Claim lifecycle against the database (task spec #3/7)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_first_claim_succeeds_and_records_history(db_session):
    user = await create_test_user(db_session, "صندوق‌باز اول")
    result = await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_APPLES, quantity=15)
    )
    await db_session.commit()

    assert result.status == "claimed"
    assert result.reward_type == REWARD_APPLES
    assert result.quantity == 15

    claim = await db_session.scalar(
        select(DailyChestClaim).where(DailyChestClaim.user_id == user.id)
    )
    assert claim is not None
    assert claim.claim_day == utc_day()
    assert claim.reward_type == REWARD_APPLES
    assert claim.quantity == 15
    assert await AppleWalletService.get_balance(db_session, user.id) == 15


@pytest.mark.asyncio
async def test_second_claim_in_same_cycle_is_rejected(db_session):
    user = await create_test_user(db_session, "صندوق‌باز دوم")
    first = await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_APPLES, quantity=15)
    )
    await db_session.commit()
    assert first.status == "claimed"

    second = await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_APPLES, quantity=25)
    )
    await db_session.commit()

    assert second.status == "already_claimed"
    assert second.next_reset > datetime.now(timezone.utc)
    rows = (await db_session.scalars(select(DailyChestClaim))).all()
    assert len(rows) == 1
    assert await AppleWalletService.get_balance(db_session, user.id) == 15


@pytest.mark.asyncio
async def test_claim_available_again_after_the_daily_boundary(db_session):
    user = await create_test_user(db_session, "صندوق‌باز مرز")
    user_id = user.id
    eve = datetime(2026, 10, 8, 23, 59, 59, tzinfo=timezone.utc)
    morning = datetime(2026, 10, 9, 0, 0, 1, tzinfo=timezone.utc)

    first = await DailyChestService.claim(
        db_session, user_id, rng=StubRng(REWARD_APPLES, quantity=15), now=eve
    )
    await db_session.commit()
    assert first.status == "claimed"

    # Still the same day (same instant, a duplicate update): rejected.
    refused = await DailyChestService.claim(
        db_session, user_id, rng=StubRng(REWARD_APPLES), now=eve
    )
    assert refused.status == "already_claimed"

    # Next UTC day: available again, with its own durable row.
    second = await DailyChestService.claim(
        db_session, user_id, rng=StubRng(REWARD_APPLES, quantity=25), now=morning
    )
    await db_session.commit()
    assert second.status == "claimed"
    rows = (
        await db_session.scalars(
            select(DailyChestClaim)
            .where(DailyChestClaim.user_id == user_id)
            .order_by(DailyChestClaim.id.asc())
        )
    ).all()
    assert [row.claim_day for row in rows] == ["2026-10-08", "2026-10-09"]
    assert await AppleWalletService.get_balance(db_session, user_id) == 40


@pytest.mark.asyncio
async def test_disabled_chest_refuses_without_writes(db_session):
    user = await create_test_user(db_session, "صندوق‌باز بسته")
    await ConfigService.set(db_session, "daily_chest_enabled", False)
    await db_session.commit()

    with pytest.raises(Exception, match="فعلاً بسته‌ست"):
        await DailyChestService.claim(db_session, user.id, rng=StubRng(REWARD_APPLES))
    rows = (await db_session.scalars(select(DailyChestClaim))).all()
    assert rows == []


# ---------------------------------------------------------------------------
# The six reward deliveries (task spec #4)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_apples_reward_credits_wallet_with_ledger_entry(db_session):
    user = await create_test_user(db_session, "سیب‌چی")
    result = await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_APPLES, quantity=25)
    )
    await db_session.commit()

    assert result.reward_type == REWARD_APPLES
    assert await AppleWalletService.get_balance(db_session, user.id) == 25
    entry = await db_session.scalar(
        select(AppleTransaction).where(AppleTransaction.user_id == user.id)
    )
    assert entry is not None
    assert entry.amount == 25
    assert entry.reason == CLAIM_REASON
    assert entry.balance_after == 25
    assert entry.idempotency_key == f"{CLAIM_REASON}:{user.id}:{utc_day()}"


@pytest.mark.asyncio
async def test_seedling_reward_uses_the_seedling_system(db_session):
    user = await create_test_user(db_session, "نهال‌دار")
    result = await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_SEEDLINGS, quantity=2)
    )
    await db_session.commit()

    assert result.reward_type == REWARD_SEEDLINGS
    assert result.quantity == 2
    assert await SeedlingService_total(db_session, user.id) == 2
    award = await db_session.scalar(
        select(SeedlingAward).where(SeedlingAward.user_id == user.id)
    )
    assert award is not None
    assert award.amount == 2
    assert award.reason == "daily_chest"
    assert award.source == "chest"


async def SeedlingService_total(session, user_id: int) -> int:
    inv = await session.scalar(
        select(SeedlingInventory).where(SeedlingInventory.user_id == user_id)
    )
    return inv.total if inv else 0


@pytest.mark.asyncio
async def test_special_apple_rewards_use_canonical_item_keys(db_session):
    cases = [
        (REWARD_GOLD, 2, "سیب طلایی"),
        (REWARD_DIAMOND, 1, "سیب الماسی"),
        (REWARD_OBSIDIAN, 1, "سیب ابسیدینی"),
    ]
    for reward, quantity, label in cases:
        user = await create_test_user(db_session, f"دارنده {label}")
        assert reward in ITEM_CATALOG  # canonical inventory item key
        result = await DailyChestService.claim(
            db_session, user.id, rng=StubRng(reward, quantity=quantity)
        )
        await db_session.commit()
        assert result.reward_type == reward
        assert result.quantity == quantity
        row = await db_session.scalar(
            select(UserItem).where(UserItem.user_id == user.id)
        )
        assert row is not None
        assert row.item_key == reward
        assert row.quantity == quantity
        # Never credited as normal red Sib currency.
        assert await AppleWalletService.get_balance(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_card_reward_grants_real_ownership_with_identity(db_session):
    hero = await _make_character(db_session, "hero_1", "آرش کمانگیر")
    user = await create_test_user(db_session, "کارت‌باز")

    result = await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_CARD)
    )
    await db_session.commit()

    assert result.status == "claimed"
    assert result.reward_type == REWARD_CARD
    assert result.character is not None and result.character.id == hero.id
    assert result.card is not None

    card = await db_session.scalar(
        select(PlayerCard).where(PlayerCard.user_id == user.id)
    )
    assert card is not None
    assert card.character_id == hero.id
    assert card.level == 1
    assert result.claim.ref_id == hero.id  # audit snapshot of the granted card
    # Existing one-time collection XP milestone fired (same as shop/trade).
    grants = (
        await db_session.scalars(
            select(PlayerProgression).where(PlayerProgression.user_id == user.id)
        )
    ).all()
    assert len(grants) == 1 and grants[0].total_xp > 0


@pytest.mark.asyncio
async def test_card_selection_skips_owned_characters(db_session):
    owned = await _make_character(db_session, "hero_owned", "مال من")
    fresh = await _make_character(db_session, "hero_fresh", "تازه")
    user = await create_test_user(db_session, "کارت‌دار")
    await PlayerCardService_acquire(db_session, user.id, owned.id)

    result = await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_CARD)
    )
    await db_session.commit()
    assert result.character is not None and result.character.id == fresh.id


async def PlayerCardService_acquire(session, user_id: int, character_id: int):
    from sib.services.player_cards import PlayerCardService

    return await PlayerCardService.acquire(session, user_id, character_id)


@pytest.mark.asyncio
async def test_full_collection_drops_card_from_pool_without_reroll(db_session):
    """Duplicate-card policy: one card per character. With every eligible
    character owned, the card weight is excluded BEFORE the single roll —
    no fabricated card, no reroll, and never a card reported as granted."""
    character = await _make_character(db_session, "hero_only", "تک‌قهرمان")
    user = await create_test_user(db_session, "کلکسیونر")
    await PlayerCardService_acquire(db_session, user.id, character.id)

    result = await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_SEEDLINGS, quantity=1)
    )
    await db_session.commit()
    assert result.status == "claimed"
    assert result.reward_type == REWARD_SEEDLINGS
    assert result.reward_type != REWARD_CARD
    cards = (await db_session.scalars(select(PlayerCard))).all()
    assert len(cards) == 1  # still exactly the one owned card


# ---------------------------------------------------------------------------
# Failure handling (task spec #7/10)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failed_card_delivery_leaves_no_claim_and_no_partial_reward(
    db_session, monkeypatch
):
    """Delivery failure (duplicate-card race backstop) must roll back the
    consumed claim together with the reward: the retry gets a fresh roll."""
    await _make_character(db_session, "hero_raced", "همزمان")
    user = await create_test_user(db_session, "شکست‌خورده")
    user_id = user.id

    async def boom(*args, **kwargs):
        raise DailyChestError("این کارت همزمان جای دیگه‌ای ثبت شد")

    from sib.services import player_cards as player_cards_module

    monkeypatch.setattr(player_cards_module.PlayerCardService, "acquire", boom)

    with pytest.raises(DailyChestError):
        await DailyChestService.claim(db_session, user_id, rng=StubRng(REWARD_CARD))
    await db_session.rollback()  # what get_session() does on error

    assert (await db_session.scalars(select(DailyChestClaim))).all() == []
    assert (await db_session.scalars(select(PlayerCard))).all() == []
    # The claim stays available: a retry opens a fresh chest.
    monkeypatch.undo()
    retry = await DailyChestService.claim(
        db_session, user_id, rng=StubRng(REWARD_APPLES, quantity=15)
    )
    await db_session.commit()
    assert retry.status == "claimed"


@pytest.mark.asyncio
async def test_invalid_config_override_fails_clearly_without_writes(db_session):
    user = await create_test_user(db_session, "پیکربندی خراب")
    await ConfigService.set(
        db_session, "daily_chest_weights", {"apples": -5, "unknown_reward": 3}
    )
    await db_session.commit()

    with pytest.raises(ChestConfigError):
        await DailyChestService.claim(db_session, user.id, rng=StubRng(REWARD_APPLES))
    assert (await db_session.scalars(select(DailyChestClaim))).all() == []
    assert await AppleWalletService.get_balance(db_session, user.id) == 0


# ---------------------------------------------------------------------------
# Duplicate protection: DB constraint + stale-read race (task spec #3/10)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_database_constraint_blocks_duplicate_daily_claim_rows(db_session):
    user = await create_test_user(db_session, "دکمه‌زن")
    user_id = user.id
    await DailyChestService.claim(
        db_session, user_id, rng=StubRng(REWARD_APPLES, quantity=15)
    )
    await db_session.commit()

    db_session.add(
        DailyChestClaim(
            user_id=user_id,
            claim_day=utc_day(),
            reward_type=REWARD_APPLES,
            quantity=25,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    rows = (await db_session.scalars(select(DailyChestClaim))).all()
    assert len(rows) == 1
    assert await AppleWalletService.get_balance(db_session, user_id) == 15


@pytest.mark.asyncio
async def test_stale_eligibility_read_still_cannot_double_grant(
    db_session, monkeypatch
):
    """Even a lying application-level eligibility check cannot beat the DB
    gate: the unique constraint rejects the insert and NO reward is paid."""
    user = await create_test_user(db_session, "نخ‌کش")
    user_id = user.id
    await DailyChestService.claim(
        db_session, user_id, rng=StubRng(REWARD_APPLES, quantity=15)
    )
    await db_session.commit()

    async def no_claim(*args, **kwargs):
        return None

    monkeypatch.setattr(DailyChestService, "get_today_claim", no_claim)
    result = await DailyChestService.claim(
        db_session, user_id, rng=StubRng(REWARD_APPLES, quantity=25)
    )
    await db_session.commit()

    assert result.status == "already_claimed"
    rows = (await db_session.scalars(select(DailyChestClaim))).all()
    assert len(rows) == 1
    assert await AppleWalletService.get_balance(db_session, user_id) == 15


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_open_grants_exactly_one_reward(db_session):
    """True concurrency (MySQL/MariaDB row locks): two simultaneous opens
    must produce exactly one reward."""
    import asyncio

    from sib.database.connection import get_session

    user = await create_test_user(db_session, "همزمان‌باز")
    user_id = user.id

    async def press():
        async with get_session() as session:
            return await DailyChestService.claim(
                session, user_id, rng=StubRng(REWARD_APPLES, quantity=15)
            )

    results = await asyncio.gather(press(), press(), return_exceptions=True)
    statuses = sorted(
        r.status for r in results if not isinstance(r, Exception)
    )
    assert statuses == ["already_claimed", "claimed"]
    rows = (await db_session.scalars(select(DailyChestClaim))).all()
    assert len(rows) == 1
    assert await AppleWalletService.get_balance(db_session, user_id) == 15


# ---------------------------------------------------------------------------
# Progression scaling (task spec #6)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_progression_scales_only_the_apples_quantity(db_session):
    from sib.services.player_xp import PlayerXPService

    low = await create_test_user(db_session, "سطح نه")
    high = await create_test_user(db_session, "سطح بیست")
    db_session.add(
        PlayerProgression(
            user_id=high.id,
            total_xp=PlayerXPService.cumulative_xp_for_level(20),
        )
    )
    await db_session.commit()

    low_result = await DailyChestService.claim(
        db_session, low.id, rng=StubRng(REWARD_APPLES, quantity=15)
    )
    high_result = await DailyChestService.claim(
        db_session, high.id, rng=StubRng(REWARD_APPLES, quantity=15)
    )
    await db_session.commit()

    assert low_result.level_bonus == 0 and low_result.quantity == 15
    assert high_result.player_level == 20
    assert high_result.level_bonus == 10
    assert high_result.quantity == 25  # 15 base + 10 progression
    assert await AppleWalletService.get_balance(db_session, high.id) == 25
    entry = await db_session.scalar(
        select(AppleTransaction).where(AppleTransaction.user_id == high.id)
    )
    assert entry.amount == 25  # the ledger records the delivered total

    # A rare outcome at the same level is untouched by progression.
    rare_user = await create_test_user(db_session, "سطح بیست ابسیدین")
    db_session.add(
        PlayerProgression(
            user_id=rare_user.id,
            total_xp=PlayerXPService.cumulative_xp_for_level(20),
        )
    )
    await db_session.commit()
    rare = await DailyChestService.claim(
        db_session, rare_user.id, rng=StubRng(REWARD_OBSIDIAN, quantity=1)
    )
    await db_session.commit()
    assert rare.quantity == 1 and rare.level_bonus == 0


# ---------------------------------------------------------------------------
# Coexistence with the existing economy (task spec #11)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_existing_daily_reward_paths_stay_untouched(db_session):
    user = await create_test_user(db_session, "قدیمی‌کار")
    await DailyChestService.claim(
        db_session, user.id, rng=StubRng(REWARD_SEEDLINGS, quantity=1)
    )
    await db_session.commit()

    # The «سیب» wake-word claim keeps its own cooldown and ledger reason.
    # (N7: its reward is a random 5–10, not a fixed 5.)
    _msg, awarded = await SibClaimService.claim(db_session, user.id)
    await db_session.commit()
    assert awarded is True
    balance = await AppleWalletService.get_balance(db_session, user.id)
    assert MIN_CLAIM_AMOUNT <= balance <= MAX_CLAIM_AMOUNT
    reasons = {
        row.reason
        for row in (await db_session.scalars(select(AppleTransaction))).all()
    }
    assert reasons == {"sib_claim"}

    # Seedling match awards still enforce their own (match_id, user_id) id.
    from sib.services.seedling import SeedlingService

    total = await SeedlingService.get_total(db_session, user.id)
    assert total == 1  # only the chest seedling so far


# ---------------------------------------------------------------------------
# Entry-point wiring (task spec #8)
# ---------------------------------------------------------------------------
def test_private_main_menu_offers_the_chest():
    from sib.keyboards.common import get_private_welcome_keyboard

    kb = get_private_welcome_keyboard("sib_bot")
    callbacks = [
        button.callback_data for row in kb.inline_keyboard for button in row
    ]
    assert "apple_chest" in callbacks


def test_chest_screen_keyboard_opens_once():
    from sib.handlers.daily_chest import _keyboard

    open_kb = _keyboard(can_open=True)
    assert [b.callback_data for b in open_kb.inline_keyboard[0]] == ["daily_chest:open"]
    done_kb = _keyboard(can_open=False)
    flat = [b.callback_data for row in done_kb.inline_keyboard for b in row]
    assert "daily_chest:open" not in flat
    assert "main_menu" in flat


def test_open_control_is_owner_scoped_and_panel_is_personal():
    from sib.services.panel import (
        OWNER_SCOPED_CALLBACKS,
        PERSONAL_PANEL_TYPES,
        is_owner_scoped_callback,
    )

    assert "daily_chest:open" in OWNER_SCOPED_CALLBACKS
    assert is_owner_scoped_callback("daily_chest:open")
    assert "chest" in PERSONAL_PANEL_TYPES
