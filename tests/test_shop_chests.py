"""Shop chests: configuration, probabilities, atomic and idempotent openings.

SQLite runs everything that does not need row locks. The concurrency tests use
``requires_row_locks`` and are reported as SKIPPED on SQLite; they only run
against MariaDB.
"""

import asyncio
import json
from fractions import Fraction

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from sib.database.connection import get_async_sessionmaker
from sib.database.models import (
    AppleTransaction,
    DailyChestClaim,
    PlayerCard,
    PlayerProgression,
    ReleaseStatus,
    ShopChest,
    ShopChestOpening,
    ShopChestReward,
)
from sib.services import shop_chests as chests_module
from sib.services.apple_wallet import AppleWalletService
from sib.services.characters import CharacterService
from sib.services.daily_chest import DailyChestService
from sib.services.items import ITEM_CATALOG, ItemService
from sib.services.player_cards import PlayerCardService
from sib.services.shop_chests import (
    RewardSpec,
    ShopChestConfigError,
    ShopChestConflictError,
    ShopChestInactiveError,
    ShopChestInsufficientFundsError,
    ShopChestService,
    format_probability,
    pick_reward,
    probability_rows,
    validate_reward_table,
)
from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_user
from sib.tests.test_shop import _make_characters
from sib.tests.test_shop_sections import _run_handler, _sell_callback

ITEM_KEY = next(iter(ITEM_CATALOG))
KEY = "chest-open-0001"


class FixedRng:
    """Replays scripted roll values; asserts each roll is in range."""

    def __init__(self, values):
        self.values = list(values)

    def randrange(self, n):
        value = self.values.pop(0)
        assert 0 <= value < n
        return value


async def _balance(session, user_id: int) -> int:
    return await AppleWalletService.get_balance(session, user_id)


async def _ledger(session, user_id: int) -> list[tuple[str, int]]:
    rows = (
        await session.scalars(
            select(AppleTransaction)
            .where(AppleTransaction.user_id == user_id)
            .order_by(AppleTransaction.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [(row.reason, row.amount) for row in rows]


async def _openings(session, user_id: int) -> list[ShopChestOpening]:
    return list(
        (
            await session.scalars(
                select(ShopChestOpening)
                .where(ShopChestOpening.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _fund(session, user_id: int, amount: int) -> None:
    await AppleWalletService.earn(
        session, user_id, amount, reason="test_fund", idempotency_key=f"test-fund-{user_id}-{amount}"
    )
    await session.commit()


async def _chest(
    session,
    *,
    code="chest_alpha",
    price=50,
    duplicate=10,
    rewards=None,
    active=True,
    name="صندوقچه الف",
):
    if rewards is None:
        rewards = [RewardSpec(kind="apples", weight=1, apples=7)]
    chest = await ShopChestService.save_chest(
        session,
        code=code,
        name=name,
        price_apples=price,
        duplicate_apples=duplicate,
        is_active=active,
        rewards=rewards,
    )
    await session.commit()
    return chest


async def _user_with_apples(session, apples=100):
    user = await create_test_user(session, "chest_player")
    user_id = user.id
    await _fund(session, user_id, apples)
    return user_id


# ---------------------------------------------------------------------------
# Pure configuration and probabilities
# ---------------------------------------------------------------------------


def test_probabilities_are_exact_and_display_as_percentages():
    rewards = [
        RewardSpec(kind="apples", weight=1, apples=5),
        RewardSpec(kind="apples", weight=3, apples=9),
    ]
    rows = probability_rows(rewards)
    assert [p for _, p in rows] == [Fraction(1, 4), Fraction(3, 4)]
    assert format_probability(Fraction(1, 4)) == "25.00%"
    assert format_probability(Fraction(1, 3)) == "33.33%"
    assert format_probability(Fraction(1, 1_000_000)) == "< 0.01%"


def test_weighted_selection_is_deterministic_for_scripted_rolls():
    rewards = [
        RewardSpec(kind="apples", weight=1, apples=1),
        RewardSpec(kind="apples", weight=2, apples=2),
        RewardSpec(kind="apples", weight=3, apples=3),
    ]  # total weight 6: [0] -> 1, [1..2] -> 2, [3..5] -> 3
    picked = [pick_reward(rewards, FixedRng([r])).apples for r in (0, 1, 2, 3, 5)]
    assert picked == [1, 2, 2, 3, 3]


def test_reward_table_validation_rejects_bad_rows():
    with pytest.raises(ShopChestConfigError):
        validate_reward_table([])
    with pytest.raises(ShopChestConfigError):
        validate_reward_table([RewardSpec(kind="apples", weight=0, apples=5)])
    with pytest.raises(ShopChestConfigError):
        validate_reward_table([RewardSpec(kind="apples", weight=1, apples=0)])
    with pytest.raises(ShopChestConfigError):
        validate_reward_table([RewardSpec(kind="item", weight=1, item_key="not_in_catalog")])
    with pytest.raises(ShopChestConfigError):
        validate_reward_table([RewardSpec(kind="card", weight=1, character_id=None)])
    with pytest.raises(ShopChestConfigError):
        validate_reward_table([RewardSpec(kind="gold", weight=1, apples=1)])
    validate_reward_table([RewardSpec(kind="apples", weight=2, apples=3)])


# ---------------------------------------------------------------------------
# Management: validation on save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_rejects_invalid_fields(db_session):
    base = dict(name="x", price_apples=5, duplicate_apples=0, is_active=False)
    for bad_code in ("Bad Code", "1start", "ab", "x" * 41, ""):
        with pytest.raises(ShopChestConfigError):
            await ShopChestService.save_chest(db_session, code=bad_code, **base)
    with pytest.raises(ShopChestConfigError):
        await ShopChestService.save_chest(db_session, code="ok_code", **{**base, "price_apples": 0})
    with pytest.raises(ShopChestConfigError):
        await ShopChestService.save_chest(db_session, code="ok_code", **{**base, "duplicate_apples": -1})
    await db_session.rollback()


@pytest.mark.asyncio
async def test_activation_requires_a_valid_reward_table(db_session):
    await db_session.commit()
    chest = await ShopChestService.save_chest(
        db_session, code="empty_one", name="خالی", price_apples=5, is_active=False
    )
    await db_session.commit()
    with pytest.raises(ShopChestConfigError):
        await ShopChestService.set_active(db_session, chest.id, True)
    await db_session.rollback()

    # A card reward for a character that is not on sale is refused too.
    hidden = await CharacterService.create(
        db_session, code="hidden_one", name_fa="پنهان", power=20, speed=20, intelligence=20
    )
    await CharacterService.set_release_status(db_session, hidden.id, ReleaseStatus.RETIRED)
    await db_session.commit()
    with pytest.raises(ShopChestConfigError):
        await ShopChestService.save_chest(
            db_session,
            code="card_chest",
            name="کارت",
            price_apples=5,
            is_active=True,
            rewards=[RewardSpec(kind="card", weight=1, character_id=hidden.id)],
        )
    await db_session.rollback()

    # A valid table can be activated, and each change bumps the version.
    chest = await ShopChestService.save_chest(
        db_session,
        code="empty_one",
        name="خالی",
        price_apples=5,
        is_active=False,
        rewards=[RewardSpec(kind="apples", weight=1, apples=2)],
    )
    await db_session.commit()
    version = chest.version
    chest = await ShopChestService.set_active(db_session, chest.id, True, expected_version=version)
    await db_session.commit()
    assert chest.is_active and chest.version == version + 1


@pytest.mark.asyncio
async def test_stale_version_is_refused(db_session):
    chest = await _chest(db_session, code="versioned")
    with pytest.raises(ShopChestConfigError):
        await ShopChestService.set_active(db_session, chest.id, False, expected_version=chest.version + 5)
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Listing, detail and probabilities (service + rendered handler views)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shows_only_active_chests_in_display_order(db_session):
    await _chest(db_session, code="later_chest", name="دوم", active=True)
    await _chest(db_session, code="hidden_chest", name="پنهان", active=False)
    first = await ShopChestService.save_chest(
        db_session,
        code="first_chest",
        name="اول",
        price_apples=9,
        sort_order=-1,
        is_active=True,
        rewards=[RewardSpec(kind="apples", weight=1, apples=1)],
    )
    await db_session.commit()
    listed = await ShopChestService.list_active(db_session)
    assert [c.code for c in listed] == ["first_chest", "later_chest"]
    assert listed[0].id == first.id


@pytest.mark.asyncio
async def test_list_and_detail_render_probabilities_and_callbacks(db_session):
    from sib.handlers import shop as shop_handlers

    user_id = await _user_with_apples(db_session, 100)
    await _chest(
        db_session,
        code="render_chest",
        price=40,
        duplicate=5,
        rewards=[
            RewardSpec(kind="apples", weight=1, apples=3),
            RewardSpec(kind="apples", weight=3, apples=8),
        ],
    )
    async with get_async_sessionmaker()() as session:
        chest = await ShopChestService.get_by_code(session, "render_chest")
        chest_id = chest.id

    list_text, list_kb = await shop_handlers._chests_view(user_id)
    assert "صندوقچه‌های فروشگاه" in list_text
    assert "صندوقچه الف" in list_text and "40" in list_text
    buttons = [b for row in list_kb.inline_keyboard for b in row]
    assert any(b.callback_data == f"shop_chest:{chest_id}" for b in buttons)

    view = await shop_handlers._chest_detail_view(user_id, chest_id)
    assert view is not None
    detail_text, detail_kb, _photo = view
    assert "25.00%" in detail_text and "75.00%" in detail_text
    assert "کارت تکراری ← `5` سیب" in detail_text
    open_buttons = [
        b.callback_data for row in detail_kb.inline_keyboard for b in row if b.callback_data
    ]
    open_cb = next(cb for cb in open_buttons if cb.startswith("shop_chest_open:"))
    assert len(open_cb.encode()) <= 64

    assert await shop_handlers._chest_detail_view(user_id, 999_999) is None


@pytest.mark.asyncio
async def test_detail_hides_open_action_for_misconfigured_chest(db_session):
    from sib.handlers import shop as shop_handlers

    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, code="broken_chest")
    await db_session.execute(
        update(ShopChestReward)
        .where(ShopChestReward.chest_id == chest.id)
        .values(kind="item", apples=None, item_key="not_in_catalog")
    )
    await db_session.commit()

    view = await shop_handlers._chest_detail_view(user_id, chest.id)
    assert view is not None
    text, kb, _ = view
    assert "در دسترس نیست" in text
    assert not any(
        (b.callback_data or "").startswith("shop_chest_open:") for row in kb.inline_keyboard for b in row
    )
    list_text, _ = await shop_handlers._chests_view(user_id)
    assert "broken_chest" not in list_text and "صندوقچه الف" not in list_text


# ---------------------------------------------------------------------------
# Opening: rewards, duplicates, refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apple_reward_is_paid_once_with_one_charge(db_session):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, price=50, rewards=[RewardSpec(kind="apples", weight=1, apples=7)])

    result = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))

    assert result.reward_kind == "apples" and result.apples_awarded == 7
    assert result.balance_after == 57 and result.replayed is False
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 57
        reasons = [r for r, _ in await _ledger(session, user_id)]
        assert "shop_chest_purchase" in reasons and "shop_chest_reward" in reasons
        openings = await _openings(session, user_id)
        assert len(openings) == 1 and openings[0].balance_after == 57


@pytest.mark.asyncio
async def test_item_reward_grants_the_item(db_session):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(
        db_session, price=30, rewards=[RewardSpec(kind="item", weight=1, item_key=ITEM_KEY)]
    )

    result = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))

    assert result.reward_kind == "item" and result.item_key == ITEM_KEY
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 70
        assert await ItemService.get_total(session, user_id, ITEM_KEY) == 1


@pytest.mark.asyncio
async def test_new_card_reward_grants_card_and_collection_xp(db_session):
    chars = await _make_characters(db_session, 1)
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(
        db_session,
        price=20,
        duplicate=15,
        rewards=[RewardSpec(kind="card", weight=1, character_id=chars[0].id)],
    )

    result = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))

    assert result.reward_kind == "card" and result.duplicate_converted is False
    assert result.compensation_apples == 0 and result.balance_after == 80
    async with get_async_sessionmaker()() as session:
        card = await session.scalar(
            select(PlayerCard).where(PlayerCard.user_id == user_id, PlayerCard.character_id == chars[0].id)
        )
        assert card is not None
        progression = await session.scalar(
            select(PlayerProgression).where(PlayerProgression.user_id == user_id)
        )
        assert progression is not None and progression.total_xp > 0


@pytest.mark.asyncio
async def test_duplicate_card_converts_to_apples_with_no_collection_xp(db_session):
    chars = await _make_characters(db_session, 1)
    user_id = await _user_with_apples(db_session, 100)
    await PlayerCardService.acquire(db_session, user_id, chars[0].id)
    await db_session.commit()
    async with get_async_sessionmaker()() as session:
        xp_before = await session.scalar(
            select(PlayerProgression.total_xp).where(PlayerProgression.user_id == user_id)
        )
    chest = await _chest(
        db_session,
        price=20,
        duplicate=15,
        rewards=[RewardSpec(kind="card", weight=1, character_id=chars[0].id)],
    )

    result = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))

    assert result.duplicate_converted is True and result.compensation_apples == 15
    assert result.balance_after == 95  # 100 - 20 + 15
    async with get_async_sessionmaker()() as session:
        xp_after = await session.scalar(
            select(PlayerProgression.total_xp).where(PlayerProgression.user_id == user_id)
        )
        assert xp_after == xp_before
        cards = (
            await session.scalars(
                select(PlayerCard).where(PlayerCard.user_id == user_id, PlayerCard.character_id == chars[0].id)
            )
        ).all()
        assert len(cards) == 1


@pytest.mark.asyncio
async def test_duplicate_without_compensation_charges_and_pays_nothing_extra(db_session):
    chars = await _make_characters(db_session, 1)
    user_id = await _user_with_apples(db_session, 100)
    await PlayerCardService.acquire(db_session, user_id, chars[0].id)
    await db_session.commit()
    chest = await _chest(
        db_session,
        code="no_comp",
        price=20,
        duplicate=0,
        rewards=[RewardSpec(kind="card", weight=1, character_id=chars[0].id)],
    )
    result = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))
    assert result.duplicate_converted and result.compensation_apples == 0
    assert result.balance_after == 80


@pytest.mark.asyncio
async def test_insufficient_funds_refused_without_side_effects(db_session):
    user_id = await _user_with_apples(db_session, 10)
    chest = await _chest(db_session, price=50)

    with pytest.raises(ShopChestInsufficientFundsError):
        await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))

    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 10
        assert await _openings(session, user_id) == []
        assert "shop_chest_purchase" not in [r for r, _ in await _ledger(session, user_id)]


@pytest.mark.asyncio
async def test_inactive_chest_refused(db_session):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, code="off_chest", active=False)
    with pytest.raises(ShopChestInactiveError):
        await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 100
        assert await _openings(session, user_id) == []


@pytest.mark.asyncio
async def test_invalid_config_on_use_is_refused_without_charge(db_session):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, code="drifted_chest")
    await db_session.execute(
        update(ShopChestReward)
        .where(ShopChestReward.chest_id == chest.id)
        .values(kind="item", apples=None, item_key="removed_from_catalog")
    )
    await db_session.commit()

    with pytest.raises(ShopChestConfigError) as info:
        await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))
    assert "در دسترس نیست" in str(info.value)

    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 100
        assert await _openings(session, user_id) == []


@pytest.mark.asyncio
async def test_item_grant_failure_rolls_back_the_debit(db_session, monkeypatch):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(
        db_session, code="grant_fail", price=30, rewards=[RewardSpec(kind="item", weight=1, item_key=ITEM_KEY)]
    )

    async def boom(*_args, **_kwargs):
        raise RuntimeError("grant failed")

    monkeypatch.setattr(chests_module.ItemService, "grant", boom)
    with pytest.raises(RuntimeError):
        await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))

    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 100
        assert await _openings(session, user_id) == []
        assert await _ledger(session, user_id) == [("test_fund", 100)]


# ---------------------------------------------------------------------------
# Idempotency, replays, conflicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_key_replay_charges_and_rewards_once(db_session):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, price=50, rewards=[RewardSpec(kind="apples", weight=1, apples=7)])

    first = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))
    second = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))

    assert first.replayed is False and second.replayed is True
    assert second.opening_id == first.opening_id
    assert second.apples_awarded == 7 and second.balance_after == 57
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 57
        reasons = [r for r, _ in await _ledger(session, user_id)]
        assert reasons.count("shop_chest_purchase") == 1
        assert reasons.count("shop_chest_reward") == 1


@pytest.mark.asyncio
async def test_same_key_on_another_chest_is_a_conflict(db_session):
    user_id = await _user_with_apples(db_session, 200)
    a = await _chest(db_session, code="chest_one", price=10)
    b = await _chest(db_session, code="chest_two", price=10)
    await ShopChestService.open_chest_reliably(user_id, a.id, KEY, rng=FixedRng([0]))
    with pytest.raises(ShopChestConflictError):
        await ShopChestService.open_chest_reliably(user_id, b.id, KEY, rng=FixedRng([0]))
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 197  # 200 - 10 + 7 from chest A only


@pytest.mark.asyncio
async def test_distinct_keys_open_again_while_funds_suffice(db_session):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, price=40, rewards=[RewardSpec(kind="apples", weight=1, apples=1)])
    await ShopChestService.open_chest_reliably(user_id, chest.id, "chest-key-aaaa", rng=FixedRng([0]))
    await ShopChestService.open_chest_reliably(user_id, chest.id, "chest-key-bbbb", rng=FixedRng([0]))
    with pytest.raises(ShopChestInsufficientFundsError):
        await ShopChestService.open_chest_reliably(user_id, chest.id, "chest-key-cccc", rng=FixedRng([0]))
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 100 - 40 * 2 + 2
        assert len(await _openings(session, user_id)) == 2


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_same_key_requests_charge_once(db_session):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, price=50, rewards=[RewardSpec(kind="apples", weight=1, apples=7)])
    results = await asyncio.gather(
        *[ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0])) for _ in range(4)],
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) for r in results), results
    assert len({r.opening_id for r in results}) == 1
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 57


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_distinct_requests_do_not_overspend(db_session):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, price=60, rewards=[RewardSpec(kind="apples", weight=1, apples=1)])
    results = await asyncio.gather(
        *[
            ShopChestService.open_chest_reliably(user_id, chest.id, f"chest-race-{i:04d}", rng=FixedRng([0]))
            for i in range(3)
        ],
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) == 1
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) >= 0


# ---------------------------------------------------------------------------
# Ambiguous commit recovery (same key, never a new one)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_commit_that_did_commit_is_reconciled_not_repeated(db_session, monkeypatch):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, price=50, rewards=[RewardSpec(kind="apples", weight=1, apples=7)])
    real_commit = chests_module.commit_chest_transaction
    calls = {"n": 0}

    async def commit_then_lose_ack(session):
        calls["n"] += 1
        await real_commit(session)
        raise OperationalError("COMMIT", {}, RuntimeError("connection lost after COMMIT"))

    monkeypatch.setattr(chests_module, "commit_chest_transaction", commit_then_lose_ack)
    result = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))

    assert calls["n"] == 1  # reconciled from the database, never re-committed
    assert result.replayed is True and result.apples_awarded == 7
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 57
        assert len(await _openings(session, user_id)) == 1


@pytest.mark.asyncio
async def test_ambiguous_commit_that_did_not_commit_is_retried_with_same_key(db_session, monkeypatch):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, price=50, rewards=[RewardSpec(kind="apples", weight=1, apples=7)])
    real_commit = chests_module.commit_chest_transaction
    keys_seen = []
    calls = {"n": 0}

    async def fail_first_commit(session):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connection reset before COMMIT")  # nothing reached the database
        await real_commit(session)

    real_debit = AppleWalletService.debit_once

    async def spy_debit(*args, **kwargs):
        keys_seen.append(kwargs["idempotency_key"])
        return await real_debit(*args, **kwargs)

    monkeypatch.setattr(chests_module, "commit_chest_transaction", fail_first_commit)
    monkeypatch.setattr(AppleWalletService, "debit_once", spy_debit)

    result = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0, 0, 0]))

    assert len(keys_seen) == 2 and len(set(keys_seen)) == 1  # same key on the retry
    assert result.replayed is False and calls["n"] == 2
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 57
        assert len(await _openings(session, user_id)) == 1


# ---------------------------------------------------------------------------
# Independence: per-chest tables and keys, daily chest untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_chest_has_its_own_reward_table(db_session):
    a = await _chest(db_session, code="table_a", rewards=[RewardSpec(kind="apples", weight=1, apples=3)])
    b = await _chest(
        db_session,
        code="table_b",
        rewards=[RewardSpec(kind="item", weight=1, item_key=ITEM_KEY)],
    )
    async with get_async_sessionmaker()() as session:
        rows_a = await ShopChestService.load_rewards(session, a.id)
        rows_b = await ShopChestService.load_rewards(session, b.id)
    assert [r.kind for r in rows_a] == ["apples"] and [r.kind for r in rows_b] == ["item"]


@pytest.mark.asyncio
async def test_shop_chest_does_not_touch_the_daily_chest(db_session):
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, price=10, rewards=[RewardSpec(kind="apples", weight=1, apples=1)])
    await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))

    async with get_async_sessionmaker()() as session:
        claims = (
            await session.scalars(select(DailyChestClaim).where(DailyChestClaim.user_id == user_id))
        ).all()
        assert claims == []

    # The daily chest still claims normally afterwards, unaffected by the shop key.
    import random

    async with get_async_sessionmaker()() as session:
        result = await DailyChestService.claim(session, user_id, rng=random.Random(3))
        await session.commit()
    assert result.claim is not None
    async with get_async_sessionmaker()() as session:
        claims = (
            await session.scalars(select(DailyChestClaim).where(DailyChestClaim.user_id == user_id))
        ).all()
        assert len(claims) == 1


# ---------------------------------------------------------------------------
# Handler flow: open button -> commit -> rendered result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_handler_renders_result_and_is_replay_safe(db_session):
    from sib.handlers import shop as shop_handlers

    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, price=50, rewards=[RewardSpec(kind="apples", weight=1, apples=7)])
    data = f"shop_chest_open:{chest.id}:{chest.version}:{KEY}"

    cb = _sell_callback(user_id, data)
    await _run_handler(shop_handlers.handle_shop_chest_open, cb)
    cb2 = _sell_callback(user_id, data)
    await _run_handler(shop_handlers.handle_shop_chest_open, cb2)

    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 57
    assert len(data.encode()) <= 64


@pytest.mark.asyncio
async def test_open_handler_shows_refusal_for_insufficient_funds(db_session):
    from sib.handlers import shop as shop_handlers

    user_id = await _user_with_apples(db_session, 5)
    chest = await _chest(db_session, price=50)
    cb = _sell_callback(user_id, f"shop_chest_open:{chest.id}:{chest.version}:{KEY}")
    await _run_handler(shop_handlers.handle_shop_chest_open, cb)
    cb.answer.assert_awaited()
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 5
        assert await _openings(session, user_id) == []


def test_chest_callbacks_are_owner_scoped_and_do_not_collide():
    from sib.services.panel import OWNER_SCOPED_CALLBACKS

    assert "shop_chest:" in OWNER_SCOPED_CALLBACKS
    assert "shop_chest_open:" in OWNER_SCOPED_CALLBACKS
    chest_prefixes = ("shop_chest:", "shop_chest_open:")
    for prefix in chest_prefixes:
        assert not prefix.startswith(("shop_buy", "shop_good_buy", "shop_sell", "daily_chest"))
        assert not ("shop_chests".startswith(prefix))  # the list callback is not an open


# ---------------------------------------------------------------------------
# Management: bulk apply and the admin CLI
# ---------------------------------------------------------------------------


def _config_entry(code, *, rewards=None, active=False, price=25):
    return {
        "code": code,
        "name": f"chest {code}",
        "price_apples": price,
        "duplicate_apples": 3,
        "sort_order": 1,
        "is_active": active,
        "expected_version": 0,
        "rewards": rewards if rewards is not None else [{"kind": "apples", "weight": 2, "apples": 4}],
    }


@pytest.mark.asyncio
async def test_bulk_apply_writes_all_or_nothing(db_session):
    from sib.services.shop_chests import apply_chest_configs

    async with get_async_sessionmaker()() as session:
        codes = await apply_chest_configs(session, [_config_entry("bulk_one"), _config_entry("bulk_two", active=True)])
        await session.commit()
    assert codes == ["bulk_one", "bulk_two"]

    bad = [
        _config_entry("bulk_three"),
        _config_entry("bulk_four", rewards=[{"kind": "item", "weight": 1, "item_key": "not_in_catalog"}]),
    ]
    async with get_async_sessionmaker()() as session:
        with pytest.raises(ShopChestConfigError):
            await apply_chest_configs(session, bad)
        await session.rollback()
    async with get_async_sessionmaker()() as session:
        assert await ShopChestService.get_by_code(session, "bulk_three") is None
        assert await ShopChestService.get_by_code(session, "bulk_four") is None


def test_admin_cli_parser_accepts_documented_commands(tmp_path):
    from sib.scripts import shop_chests as cli

    parser = cli.build_parser()
    assert parser.parse_args(["list"]).command == "list"
    assert parser.parse_args(["show", "x_chest"]).code == "x_chest"
    args = parser.parse_args(["apply", str(tmp_path / "c.json"), "--dry-run"])
    assert args.dry_run is True
    args = parser.parse_args(["set-active", "x_chest", "off", "--expected-version", "4"])
    assert (args.state, args.expected_version) == ("off", 4)


def _test_database_name() -> str:
    import os

    from sqlalchemy.engine import make_url

    return make_url(os.environ["sib_TEST_DATABASE_URL"]).database


@pytest.mark.asyncio
async def test_admin_cli_commands_on_the_test_database(db_session, tmp_path, monkeypatch, capsys):
    from sib.scripts import shop_chests as cli

    # The CLI commands are async; the test drives them on the test database
    # (main() wraps them in asyncio.run and is exercised by the parser test).
    monkeypatch.setattr(cli, "get_async_sessionmaker", get_async_sessionmaker)
    config = tmp_path / "chests.json"
    config.write_text(json.dumps([_config_entry("cli_chest")]), encoding="utf-8")

    assert await cli._cmd_apply(config, True) == 0  # dry run
    async with get_async_sessionmaker()() as session:
        assert await ShopChestService.get_by_code(session, "cli_chest") is None

    db_name = _test_database_name()
    assert await cli._cmd_apply(config, False, confirm_database=db_name) == 0
    assert await cli._cmd_show("cli_chest") == 0
    assert "100.00%" in capsys.readouterr().out

    assert await cli._cmd_set_active("cli_chest", True, None, confirm_database=db_name) == 0
    assert await cli._cmd_list() == 0
    assert "cli_chest" in capsys.readouterr().out
    assert await cli._cmd_show("missing_chest") == 2


# ---------------------------------------------------------------------------
# Lock order for a chest opening with an item reward (user -> items -> wallet)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chest_item_reward_takes_inventory_before_wallet(db_session, monkeypatch):
    from sib.services import shop_chests as module
    from sib.tests.test_lock_ordering import _record_calls

    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, code="order_chest", price=10, rewards=[RewardSpec(kind="item", weight=1, item_key=ITEM_KEY)])
    events: list[str] = []
    _record_calls(
        monkeypatch,
        events,
        [
            (module.ItemService, "grant", "item"),
            (module.AppleWalletService, "debit_once", "wallet"),
        ],
    )
    await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))
    assert events == ["item", "wallet"], events
