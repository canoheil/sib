"""Card upgrade service: levels, costs, insufficient resources, atomicity."""

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, update

from sib.database.connection import get_session
from sib.database.models import (
    AppleBattleQueueEntry,
    AppleTransaction,
    Character,
    PlayerCard,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.card_progression import MAX_CARD_LEVEL, CardAttributes, card_attributes
from sib.services.card_upgrade import (
    CardMaxLevelError,
    CardNotOwnedError,
    CardUpgradeService,
    InsufficientResourcesError,
    StaleUpgradeError,
)
from sib.services.characters import CharacterService
from sib.services.items import ItemService
from sib.services.player_cards import PlayerCardService
from sib.tests.conftest import _using_sqlite, requires_row_locks
from sib.tests.test_card_progression import EXPECTED_DEFAULT
from sib.tests.test_parties import create_test_user

GOLD, DIAMOND, OBSIDIAN = "apple_gold", "apple_diamond", "apple_obsidian"
ALL_ITEMS = (GOLD, DIAMOND, OBSIDIAN)


async def _new_card(db_session, name="ارتقاکار", code="up_hero", base=(22, 20, 18)):
    user = await create_test_user(db_session, name)
    character = await CharacterService.create(
        db_session,
        code=code,
        name_fa=f"قهرمان {code}",
        power=base[0],
        speed=base[1],
        intelligence=base[2],
    )
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()
    return user, character, card


async def _fund(db_session, user, apples=0, **items):
    if apples:
        await AppleWalletService.earn(db_session, user.id, apples, reason="test_grant")
    for key, qty in items.items():
        await ItemService.grant(db_session, user.id, f"apple_{key}", qty)
    await db_session.commit()


async def _holdings(db_session, user):
    uid = user if isinstance(user, int) else user.id
    return (
        await AppleWalletService.get_balance(db_session, uid),
        {k: await ItemService.get_total(db_session, uid, k) for k in ALL_ITEMS},
    )


async def _level(db_session, card_id):
    return await db_session.scalar(
        select(PlayerCard.level)
        .where(PlayerCard.id == card_id)
        .execution_options(populate_existing=True)
    )


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_new_cards_start_at_level_one_with_base_attributes(db_session):
    _user, character, card = await _new_card(db_session)
    assert card.level == 1
    assert card_attributes(card, character) == CardAttributes(22, 20, 18)


@pytest.mark.asyncio
async def test_level_1_to_2_then_2_to_3(db_session):
    user, character, card = await _new_card(db_session)
    await _fund(db_session, user, apples=10_000, gold=50)

    first = await CardUpgradeService.upgrade(db_session, user.id, card.id)
    await db_session.commit()
    assert (first.from_level, first.to_level) == (1, 2)
    assert first.before == CardAttributes(22, 20, 18)
    assert first.after == CardAttributes(32, 30, 28)
    assert await _level(db_session, card.id) == 2

    second = await CardUpgradeService.upgrade(db_session, user.id, card.id)
    await db_session.commit()
    assert (second.from_level, second.to_level) == (2, 3)
    assert second.after == CardAttributes(42, 40, 38)
    assert await _level(db_session, card.id) == 3
    assert card_attributes(card, character).total == 120


@pytest.mark.asyncio
async def test_full_progression_to_level_10_spends_each_tier_exactly(db_session):
    user, character, card = await _new_card(db_session)
    await _fund(db_session, user, apples=100_000, gold=100, diamond=100, obsidian=100)
    apples, items = await _holdings(db_session, user)

    for level in range(1, MAX_CARD_LEVEL):
        apples_cost, key, qty = EXPECTED_DEFAULT[level - 1]
        result = await CardUpgradeService.upgrade(
            db_session, user.id, card.id, expected_level=level
        )
        await db_session.commit()
        new_apples, new_items = await _holdings(db_session, user)

        assert result.to_level == level + 1
        assert new_apples == apples - apples_cost  # Red/normal Apples
        for item in ALL_ITEMS:  # ONLY the tier's material is charged
            expected = items[item] - (qty if item == key else 0)
            assert new_items[item] == expected, (level, item)
        apples, items = new_apples, new_items
        stats = card_attributes(await db_session.get(PlayerCard, card.id), character)
        assert stats.total == 60 + 30 * level

    assert await _level(db_session, card.id) == MAX_CARD_LEVEL
    assert sum(c[0] for c in EXPECTED_DEFAULT) == 100_000 - apples
    spent = {k: 100 - items[k] for k in ALL_ITEMS}
    assert spent == {GOLD: 16, DIAMOND: 19, OBSIDIAN: 18}


@pytest.mark.asyncio
async def test_level_10_cannot_be_upgraded_and_nothing_is_spent(db_session):
    user, _character, card = await _new_card(db_session)
    await db_session.execute(
        update(PlayerCard).where(PlayerCard.id == card.id).values(level=MAX_CARD_LEVEL)
    )
    await _fund(db_session, user, apples=99_999, gold=99, diamond=99, obsidian=99)
    before = await _holdings(db_session, user)

    with pytest.raises(CardMaxLevelError):
        await CardUpgradeService.upgrade(db_session, user.id, card.id)
    with pytest.raises(CardMaxLevelError):
        await CardUpgradeService.upgrade(
            db_session, user.id, card.id, expected_level=MAX_CARD_LEVEL
        )
    assert await _level(db_session, card.id) == MAX_CARD_LEVEL
    assert await _holdings(db_session, user) == before

    preview = await CardUpgradeService.preview(db_session, user.id, card.id)
    assert preview.is_max_level and preview.cost is None and preview.after is None


@pytest.mark.asyncio
async def test_one_action_never_skips_levels(db_session):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=100_000, gold=100, diamond=100, obsidian=100)
    for expected in range(2, 11):
        result = await CardUpgradeService.upgrade(db_session, user.id, card.id)
        await db_session.commit()
        assert result.to_level - result.from_level == 1
        assert await _level(db_session, card.id) == expected


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cannot_upgrade_someone_elses_card(db_session):
    _owner, _c, card = await _new_card(db_session)
    thief = await create_test_user(db_session, "دزد ارتقا")
    await _fund(db_session, thief, apples=10_000, gold=50)
    before = await _holdings(db_session, thief)

    with pytest.raises(CardNotOwnedError):
        await CardUpgradeService.upgrade(db_session, thief.id, card.id)
    with pytest.raises(CardNotOwnedError):
        await CardUpgradeService.preview(db_session, thief.id, card.id)
    with pytest.raises(CardNotOwnedError):
        await CardUpgradeService.upgrade(db_session, thief.id, 987654321)
    assert await _level(db_session, card.id) == 1
    assert await _holdings(db_session, thief) == before


# ---------------------------------------------------------------------------
# Insufficient resources
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "apples, gold, miss_apples, miss_gold",
    [
        (0, 0, 100, 3),      # nothing at all
        (80, 3, 20, 0),      # only Apples short
        (100, 1, 0, 2),      # only the material short
        (40, 2, 60, 1),      # both short
    ],
)
async def test_insufficient_resources_reports_exact_missing_and_changes_nothing(
    db_session, apples, gold, miss_apples, miss_gold
):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=apples, gold=gold)
    before = await _holdings(db_session, user)
    ledger_before = await db_session.scalar(select(func.count(AppleTransaction.id)))

    with pytest.raises(InsufficientResourcesError) as info:
        await CardUpgradeService.upgrade(db_session, user.id, card.id, expected_level=1)

    missing = info.value.missing
    assert (missing.apples, missing.item_quantity) == (miss_apples, miss_gold)
    assert missing.item_key == GOLD
    assert await _level(db_session, card.id) == 1
    assert await _holdings(db_session, user) == before
    assert await db_session.scalar(select(func.count(AppleTransaction.id))) == ledger_before
    apples_now, _ = before
    assert apples_now >= 0

    preview = await CardUpgradeService.preview(db_session, user.id, card.id)
    assert not preview.can_afford
    assert (preview.missing.apples, preview.missing.item_quantity) == (miss_apples, miss_gold)


@pytest.mark.asyncio
async def test_other_special_apples_do_not_pay_for_the_tier_material(db_session):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=5_000, diamond=50, obsidian=50)  # no gold
    with pytest.raises(InsufficientResourcesError) as info:
        await CardUpgradeService.upgrade(db_session, user.id, card.id)
    assert info.value.missing.item_key == GOLD
    assert info.value.missing.item_quantity == 3
    assert info.value.missing.apples == 0


@pytest.mark.asyncio
async def test_balances_never_go_negative_at_the_exact_boundary(db_session):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=100, gold=3)  # exactly the L1->2 price
    await CardUpgradeService.upgrade(db_session, user.id, card.id)
    await db_session.commit()
    assert await _holdings(db_session, user) == (0, {GOLD: 0, DIAMOND: 0, OBSIDIAN: 0})
    with pytest.raises(InsufficientResourcesError):
        await CardUpgradeService.upgrade(db_session, user.id, card.id)
    assert await _holdings(db_session, user) == (0, {GOLD: 0, DIAMOND: 0, OBSIDIAN: 0})


# ---------------------------------------------------------------------------
# Atomicity / double requests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_success_deducts_and_levels_together_with_one_ledger_row(db_session):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=500, gold=10)
    await CardUpgradeService.upgrade(db_session, user.id, card.id, expected_level=1)
    await db_session.commit()

    assert await _level(db_session, card.id) == 2
    assert await _holdings(db_session, user) == (400, {GOLD: 7, DIAMOND: 0, OBSIDIAN: 0})
    rows = (
        await db_session.scalars(
            select(AppleTransaction).where(AppleTransaction.reason == "card_upgrade")
        )
    ).all()
    assert [(r.amount, r.ref_type, r.ref_id) for r in rows] == [(-100, "card", card.id)]


@pytest.mark.asyncio
async def test_double_tap_with_the_same_seen_level_charges_once(db_session):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=10_000, gold=50)

    await CardUpgradeService.upgrade(db_session, user.id, card.id, expected_level=1)
    await db_session.commit()
    after_first = await _holdings(db_session, user)

    with pytest.raises(StaleUpgradeError) as info:  # the replayed/double-tapped button
        await CardUpgradeService.upgrade(db_session, user.id, card.id, expected_level=1)
    assert info.value.current_level == 2
    assert await _level(db_session, card.id) == 2
    assert await _holdings(db_session, user) == after_first


@pytest.mark.asyncio
async def test_failure_after_charging_rolls_everything_back(db_session, monkeypatch):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=500, gold=10)
    before = await _holdings(db_session, user)
    card_id, user_id = card.id, user.id  # the rollback expires ORM instances

    async def boom(*args, **kwargs):
        raise RuntimeError("inventory exploded")

    monkeypatch.setattr(ItemService, "consume", boom)
    with pytest.raises(RuntimeError):
        await CardUpgradeService.upgrade(db_session, user_id, card_id, expected_level=1)

    assert await _level(db_session, card_id) == 1
    assert await _holdings(db_session, user_id) == before
    spent = await db_session.scalar(
        select(func.count(AppleTransaction.id)).where(AppleTransaction.reason == "card_upgrade")
    )
    assert spent == 0


@pytest.mark.asyncio
async def test_retired_ledger_keys_cannot_make_an_upgrade_free(db_session):
    """The old system used ``card_upgrade:<card>:<level>``; rows may still exist."""
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=500, gold=10)
    db_session.add(
        AppleTransaction(
            user_id=user.id, amount=-100, reason="card_upgrade", ref_type="card",
            ref_id=card.id, idempotency_key=f"card_upgrade:{card.id}:2", balance_after=0,
        )
    )
    await db_session.commit()

    await CardUpgradeService.upgrade(db_session, user.id, card.id)
    await db_session.commit()
    assert await _holdings(db_session, user) == (400, {GOLD: 7, DIAMOND: 0, OBSIDIAN: 0})


@pytest.mark.asyncio
async def test_battle_loadout_level_mirrors_the_upgrade(db_session):
    user, _c, card = await _new_card(db_session)
    db_session.add(
        AppleBattleQueueEntry(
            user_id=user.id, card_id=card.id, level=1, queued_at=datetime.now(timezone.utc)
        )
    )
    await _fund(db_session, user, apples=500, gold=10)
    await CardUpgradeService.upgrade(db_session, user.id, card.id)
    await db_session.commit()
    level = await db_session.scalar(
        select(AppleBattleQueueEntry.level)
        .where(AppleBattleQueueEntry.card_id == card.id)
        .execution_options(populate_existing=True)
    )
    assert level == 2


@pytest.mark.asyncio
@pytest.mark.skipif(not _using_sqlite(), reason="without row locks only: the CAS is the guard")
async def test_compare_and_swap_stops_a_lost_update_without_row_locks(
    db_session, monkeypatch
):
    """Another request bumps the level AFTER our validation: CAS refuses to charge."""
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=10_000, gold=50)
    before = await _holdings(db_session, user)
    real = ItemService.get_total

    async def racing_get_total(session, user_id, key, for_update=False):
        value = await real(session, user_id, key, for_update=for_update)
        # A competing request wins first (same connection: SQLite cannot
        # interleave two writers, and the CAS only sees the row state).
        await session.execute(
            update(PlayerCard).where(PlayerCard.id == card.id).values(level=2)
        )
        return value

    monkeypatch.setattr(ItemService, "get_total", staticmethod(racing_get_total))
    with pytest.raises(StaleUpgradeError):
        await CardUpgradeService.upgrade(db_session, user.id, card.id, expected_level=1)
    assert await _level(db_session, card.id) == 2  # only the competing change
    assert await _holdings(db_session, user) == before  # nothing charged


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_requests_upgrade_once_and_spend_once(db_session):
    user, _c, card = await _new_card(db_session)
    await _fund(db_session, user, apples=10_000, gold=50)
    before = await _holdings(db_session, user)

    async def press():
        async with get_session() as session:
            try:
                await CardUpgradeService.upgrade(
                    session, user.id, card.id, expected_level=1
                )
                return "ok"
            except StaleUpgradeError:
                return "stale"

    results = await asyncio.gather(*(press() for _ in range(8)))
    assert results.count("ok") == 1 and results.count("stale") == 7
    assert await _level(db_session, card.id) == 2
    apples, items = await _holdings(db_session, user)
    assert (apples, items[GOLD]) == (before[0] - 100, before[1][GOLD] - 3)


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_upgrades_of_two_cards_cannot_double_spend(db_session):
    """Two different cards race for ONE set of resources: only one is paid."""
    user, _c, card_a = await _new_card(db_session, code="race_a")
    character_b = await CharacterService.create(
        db_session, code="race_b", name_fa="قهرمان race_b", power=20, speed=20, intelligence=20
    )
    card_b = await PlayerCardService.acquire(db_session, user.id, character_b.id)
    await _fund(db_session, user, apples=100, gold=3)  # exactly ONE upgrade

    async def press(card_id):
        async with get_session() as session:
            try:
                await CardUpgradeService.upgrade(session, user.id, card_id)
                return "ok"
            except InsufficientResourcesError:
                return "poor"

    results = await asyncio.gather(press(card_a.id), press(card_b.id))
    assert sorted(results) == ["ok", "poor"]
    assert await _holdings(db_session, user) == (0, {GOLD: 0, DIAMOND: 0, OBSIDIAN: 0})


# ---------------------------------------------------------------------------
# Future cards need no special logic
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_brand_new_card_type_upgrades_with_zero_special_code(db_session):
    user, character, card = await _new_card(
        db_session, code="future_card", base=(10, 25, 25)
    )
    await _fund(db_session, user, apples=100_000, gold=100, diamond=100, obsidian=100)
    for _ in range(MAX_CARD_LEVEL - 1):
        await CardUpgradeService.upgrade(db_session, user.id, card.id)
        await db_session.commit()
    assert card_attributes(await db_session.get(PlayerCard, card.id), character) == CardAttributes(
        100, 115, 115
    )
    # The shared definition was never touched.
    fresh = await db_session.get(Character, character.id)
    assert (fresh.power, fresh.speed, fresh.intelligence) == (10, 25, 25)
