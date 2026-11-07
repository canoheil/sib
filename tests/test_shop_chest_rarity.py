"""Tier odds, atomic per-user pity, and the closed -> open -> closed UI."""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import func, select

from sib.database.connection import get_async_sessionmaker, seed_characters
from sib.database.models import (
    AppleTransaction, Character, PlayerCard, ShopChest, ShopChestOpening, ShopChestPity,
)
from sib.services import shop_chests as chests
from sib.services.apple_wallet import AppleWalletService
from sib.services.shop_chests import ShopChestConfigError, ShopChestError, ShopChestService, pick_rarity
from sib.tests.conftest import requires_row_locks
from sib.tests.test_shop_chests import FixedRng, _chest, _user_with_apples

EXPECTED_ODDS = {
    "BRONZE": {"COMMON": 75, "RARE": 20, "EPIC": 4, "LEGENDARY": 1},
    "SILVER": {"COMMON": 55, "RARE": 32, "EPIC": 10, "LEGENDARY": 3},
    "GOLD": {"COMMON": 30, "RARE": 45, "EPIC": 20, "LEGENDARY": 5},
    "DIAMOND": {"COMMON": 10, "RARE": 35, "EPIC": 45, "LEGENDARY": 10},
}


@pytest.fixture
async def tiers(db_session):
    await seed_characters(db_session)
    assert await ShopChestService.seed_tiers(db_session) == 4
    await db_session.commit()
    return {chest.tier: chest for chest in await ShopChestService.list_active(db_session)}


@pytest.mark.parametrize("tier", EXPECTED_ODDS)
def test_mocked_rng_exhaustively_matches_each_tiers_odds(tier):
    assert Counter(pick_rarity(tier, FixedRng([roll])) for roll in range(100)) == EXPECTED_ODDS[tier]
    high = {rarity: weight for rarity, weight in EXPECTED_ODDS[tier].items()
            if rarity in ("EPIC", "LEGENDARY")}
    assert Counter(pick_rarity(tier, FixedRng([roll]), guaranteed=True)
                   for roll in range(sum(high.values()))) == high


@pytest.mark.parametrize("tier", EXPECTED_ODDS)
@pytest.mark.parametrize("rarity", ("COMMON", "RARE", "EPIC", "LEGENDARY"))
async def test_tier_roll_then_uniform_eligible_card_selection(db_session, tiers, tier, rarity):
    user_id = await _user_with_apples(db_session, 20_000)
    pool = await ShopChestService.tier_card_pool(db_session, tier)
    assert not {card.code for cards in pool.values() for card in cards} & {"amber", "hero"}
    roll = list(EXPECTED_ODDS[tier]).index(rarity)
    boundary = sum(list(EXPECTED_ODDS[tier].values())[:roll])
    result = await ShopChestService.open_chest_reliably(
        user_id, tiers[tier].id, "tier-rarity-0001",
        rng=FixedRng([boundary, len(pool[rarity]) - 1]),
    )
    assert result.reward_kind == "card" and result.reward_rarity == rarity
    assert result.character_id == pool[rarity][-1].id
    assert not result.pity_guaranteed
    assert await ShopChestService.pity_counter(db_session, user_id) == (0 if rarity in ("EPIC", "LEGENDARY") else 1)


async def test_startup_registers_tiers_after_seeding_the_roster(db_session):
    from sib.database.connection import init_db

    await init_db(db_session.bind)
    active = await ShopChestService.list_active(db_session)
    assert {chest.tier for chest in active} == set(EXPECTED_ODDS)
    await init_db(db_session.bind)
    assert len(await ShopChestService.list_active(db_session)) == 4


async def test_missing_rarity_seeds_inactive_tiers_without_breaking_startup(db_session):
    from sib.database.connection import init_db

    await seed_characters(db_session)
    igris = await db_session.scalar(select(Character).where(Character.code == "igris"))
    igris.is_active = False  # The operator has disabled the only eligible Legendary.
    await db_session.commit()
    await init_db(db_session.bind)
    registered = (await db_session.scalars(select(ShopChest))).all()
    assert {chest.tier for chest in registered} == set(EXPECTED_ODDS)
    assert all(not chest.is_active for chest in registered)


async def test_tier_seeding_preserves_operator_prices_and_activation(db_session, tiers):
    bronze = tiers["BRONZE"]
    bronze.price_apples, bronze.is_active = 777, False
    await db_session.commit()
    assert await ShopChestService.seed_tiers(db_session) == 0
    await db_session.refresh(bronze)
    assert bronze.price_apples == 777 and not bronze.is_active
    assert bronze.tier == "BRONZE"
    rows = await ShopChestService.describe(db_session, tiers["DIAMOND"])
    assert {label: float(probability) for _, probability, label in rows}["🎴 کارت افسانه‌ای"] == 0.10


async def test_tier_config_can_be_applied_without_a_custom_reward_table(db_session, tiers):
    from sib.services.shop_chests import apply_chest_configs

    await apply_chest_configs(db_session, [{
        "code": "operator_tier", "name": "صندوق سفارشی طلایی", "price_apples": 3000,
        "tier": "GOLD", "is_active": True, "expected_version": 0,
    }])
    chest = await ShopChestService.get_by_code(db_session, "operator_tier")
    assert chest.tier == "GOLD" and chest.price_apples == 3000
    assert await ShopChestService.load_rewards(db_session, chest.id) == []
    await ShopChestService.validate_chest(db_session, chest)
    with pytest.raises(ShopChestConfigError):
        await ShopChestService.save_chest(
            db_session, code="invalid_tier", name="صندوق", price_apples=500,
            tier="MYTHIC", is_active=True,
        )


async def test_ambiguous_commit_replay_does_not_increment_pity_twice(db_session, tiers, monkeypatch):
    user_id = await _user_with_apples(db_session, 10_000)
    original_commit = chests.commit_chest_transaction

    async def committed_but_disconnected(session):
        await original_commit(session)
        raise RuntimeError("connection lost after commit")

    monkeypatch.setattr(chests, "commit_chest_transaction", committed_but_disconnected)
    result = await ShopChestService.open_chest_reliably(
        user_id, tiers["BRONZE"].id, "ambiguous-pity-01", rng=FixedRng([0, 0]),
    )
    assert result.replayed and result.reward_rarity == "COMMON"
    assert await ShopChestService.pity_counter(db_session, user_id) == 1
    assert await AppleWalletService.get_balance(db_session, user_id) == 9500
    assert await db_session.scalar(select(func.count(ShopChestOpening.id))) == 1


async def test_nine_misses_then_tenth_is_epic_and_counter_is_per_user(db_session, tiers):
    user_id = await _user_with_apples(db_session, 100_000)
    other_id = await _user_with_apples(db_session, 20_000)
    bronze = tiers["BRONZE"]
    for index in range(9):
        result = await ShopChestService.open_chest_reliably(
            user_id, bronze.id, f"pity-miss-{index:04d}", rng=FixedRng([0, 0]),
        )
        assert result.reward_rarity == "COMMON" and not result.pity_guaranteed
        assert await ShopChestService.pity_counter(db_session, user_id) == index + 1
    # The same paid press must neither draw again nor advance the counter.
    replay = await ShopChestService.open_chest_reliably(
        user_id, bronze.id, "pity-miss-0008", rng=FixedRng([]),
    )
    assert replay.replayed and not replay.pity_guaranteed
    assert await ShopChestService.pity_counter(db_session, user_id) == 9
    assert await ShopChestService.pity_counter(db_session, other_id) == 0
    other = await ShopChestService.open_chest_reliably(
        other_id, bronze.id, "other-user-0001", rng=FixedRng([0, 0]),
    )
    assert other.reward_rarity == "COMMON" and not other.pity_guaranteed
    assert await ShopChestService.pity_counter(db_session, other_id) == 1

    # Switching tiers cannot escape or reset pity: this user's tenth is Epic+.
    tenth = await ShopChestService.open_chest_reliably(
        user_id, tiers["DIAMOND"].id, "pity-tenth-0001", rng=FixedRng([0, 0]),
    )
    assert tenth.reward_rarity == "EPIC" and tenth.pity_guaranteed
    assert await ShopChestService.pity_counter(db_session, user_id) == 0
    assert await ShopChestService.pity_counter(db_session, other_id) == 1
    # Snapshot survives a later rarity edit, and replay cannot change pity.
    card = await db_session.get(Character, tenth.character_id)
    card.rarity = "COMMON"
    await db_session.commit()
    replay = await ShopChestService.open_chest_reliably(
        user_id, tiers["DIAMOND"].id, "pity-tenth-0001", rng=FixedRng([]),
    )
    assert replay.replayed and replay.pity_guaranteed and replay.reward_rarity == "EPIC"
    assert await ShopChestService.pity_counter(db_session, user_id) == 0
    assert await db_session.scalar(select(func.count(ShopChestOpening.id)).where(ShopChestOpening.user_id == user_id)) == 10


@pytest.mark.parametrize("rarity, roll", [("EPIC", 95), ("LEGENDARY", 99)])
async def test_natural_high_rarity_duplicate_also_resets_pity(db_session, tiers, rarity, roll):
    from sib.services.player_cards import PlayerCardService

    user_id = await _user_with_apples(db_session, 10_000)
    pool = await ShopChestService.tier_card_pool(db_session, "BRONZE")
    await PlayerCardService.acquire(db_session, user_id, pool[rarity][0].id)
    db_session.add(ShopChestPity(user_id=user_id, pity_counter=7))
    await db_session.commit()
    result = await ShopChestService.open_chest_reliably(
        user_id, tiers["BRONZE"].id, "natural-high-001", rng=FixedRng([roll, 0]),
    )
    assert result.reward_rarity == rarity and result.duplicate_converted
    assert not result.pity_guaranteed
    assert await ShopChestService.pity_counter(db_session, user_id) == 0


async def test_custom_chests_count_toward_the_same_pity(db_session, tiers):
    user_id = await _user_with_apples(db_session, 10_000)
    chest = await _chest(db_session)  # Legacy, apple-only table remains valid.
    db_session.add(ShopChestPity(user_id=user_id, pity_counter=8))
    await db_session.commit()
    ninth = await ShopChestService.open_chest_reliably(
        user_id, chest.id, "custom-ninth-01", rng=FixedRng([0]),
    )
    assert ninth.reward_kind == "apples" and not ninth.pity_guaranteed
    tenth = await ShopChestService.open_chest_reliably(
        user_id, chest.id, "custom-tenth-01", rng=FixedRng([0, 0]),
    )
    assert tenth.reward_rarity == "EPIC" and tenth.pity_guaranteed
    assert await ShopChestService.pity_counter(db_session, user_id) == 0


@pytest.mark.parametrize("failure", ["funds", "stale", "missing_rarity"])
async def test_refusals_never_consume_pity_or_charge(db_session, tiers, failure):
    user_id = await _user_with_apples(db_session, 0 if failure == "funds" else 10_000)
    db_session.add(ShopChestPity(user_id=user_id, pity_counter=9))
    if failure == "missing_rarity":
        igris = await db_session.scalar(select(Character).where(Character.code == "igris"))
        igris.is_active = False
    await db_session.commit()
    chest = tiers["BRONZE"]
    async with get_async_sessionmaker()() as session:
        with pytest.raises(ShopChestError):
            await ShopChestService.open_chest(
                session, user_id, chest.id, "refused-pity-001", rng=FixedRng([0, 0]),
                expected_version=chest.version + (failure == "stale"),
            )
        await session.commit()  # Even callers swallowing a refusal persist nothing.
    assert await ShopChestService.pity_counter(db_session, user_id) == 9
    assert await db_session.scalar(select(func.count(ShopChestOpening.id))) == 0
    assert await db_session.scalar(select(func.count(AppleTransaction.id)).where(AppleTransaction.reason == "shop_chest_purchase")) == 0


async def test_exception_after_pity_assignment_rolls_back_the_whole_opening(db_session, tiers, monkeypatch):
    from sib.services.player_xp import PlayerXPService

    user_id = await _user_with_apples(db_session, 10_000)
    db_session.add(ShopChestPity(user_id=user_id, pity_counter=9))
    await db_session.commit()
    monkeypatch.setattr(PlayerXPService, "award_character_collection", AsyncMock(side_effect=RuntimeError("XP failed")))
    with pytest.raises(RuntimeError, match="XP failed"):
        await ShopChestService.open_chest_reliably(
            user_id, tiers["BRONZE"].id, "rollback-pity-01", rng=FixedRng([0, 0]),
        )
    assert await ShopChestService.pity_counter(db_session, user_id) == 9
    assert await AppleWalletService.get_balance(db_session, user_id) == 10_000
    assert await db_session.scalar(select(func.count(ShopChestOpening.id))) == 0
    assert await db_session.scalar(select(func.count(PlayerCard.id))) == 0


@requires_row_locks
async def test_concurrent_opens_serialize_the_pity_counter(db_session, tiers):
    import asyncio

    user_id = await _user_with_apples(db_session, 10_000)
    db_session.add(ShopChestPity(user_id=user_id, pity_counter=8))
    await db_session.commit()
    results = await asyncio.gather(*(
        ShopChestService.open_chest_reliably(
            user_id, tiers["BRONZE"].id, f"parallel-pity-{index}", rng=FixedRng([0, 0]),
        ) for index in range(2)
    ))
    assert sorted(result.reward_rarity for result in results) == ["COMMON", "EPIC"]
    assert sum(result.pity_guaranteed for result in results) == 1
    assert await ShopChestService.pity_counter(db_session, user_id) == 0


@pytest.mark.parametrize("initial_pity,rarity,counter_after", [(0, "COMMON", 1), (9, "EPIC", 0)])
async def test_visual_opening_swaps_media_and_grants_reward_once(
    db_session, tiers, monkeypatch, initial_pity, rarity, counter_after
):
    from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, User as TelegramUser
    from sib.handlers import shop

    user_id = await _user_with_apples(db_session, 10_000)
    if initial_pity:
        db_session.add(ShopChestPity(user_id=user_id, pity_counter=initial_pity))
        await db_session.commit()
    monkeypatch.setattr(shop, "_shop_actor", AsyncMock(return_value=user_id))
    monkeypatch.setattr(shop, "safe_answer_callback", AsyncMock())
    monkeypatch.setattr(chests.random, "SystemRandom", lambda: FixedRng([0, 0]))
    message = Message(
        message_id=55, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type="private"),
        photo=[PhotoSize(file_id="closed-photo", file_unique_id="closed", width=1024, height=1024)],
    )
    callback = CallbackQuery(
        id="detail", from_user=TelegramUser(id=user_id, is_bot=False, first_name="خریدار"),
        chat_instance="test", message=message, data=f"shop_chest:{tiers['BRONZE'].id}",
    )
    bot = AsyncMock()
    bot.edit_message_media.return_value = message
    await shop.handle_shop_chest(callback, bot)
    closed = bot.edit_message_media.await_args.kwargs
    assert Path(closed["media"].media.path).name == "chest_closed.jpg"
    open_button = next(button for row in closed["reply_markup"].inline_keyboard for button in row
                       if button.callback_data.startswith("shop_chest_open:"))
    assert open_button.text == "🔓 باز کردن" and len(open_button.callback_data.encode()) <= 64
    assert await db_session.scalar(select(func.count(ShopChestOpening.id))) == 0

    opened_callback = callback.model_copy(update={"id": "open", "data": open_button.callback_data})
    await shop.handle_shop_chest_open(opened_callback, bot)
    opened = bot.edit_message_media.await_args.kwargs
    assert opened["message_id"] == closed["message_id"] == 55  # Actual edit, not a new text panel.
    assert Path(opened["media"].media.path).name == "chest_open.jpg"
    assert "کارت جدید" in opened["media"].caption
    assert chests.RARITY_LABELS[rarity] in opened["media"].caption
    assert ("جایزهٔ تضمینی" in opened["media"].caption) == bool(initial_pity)
    assert await db_session.scalar(select(func.count(ShopChestOpening.id))) == 1
    assert await ShopChestService.pity_counter(db_session, user_id) == counter_after
    # Double tap: stored reward, no new draw, charge or pity increment.
    await shop.handle_shop_chest_open(opened_callback, bot)
    assert "دوباره کسر نشد" in bot.edit_message_media.await_args.kwargs["media"].caption
    assert await db_session.scalar(select(func.count(ShopChestOpening.id))) == 1
    assert await ShopChestService.pity_counter(db_session, user_id) == counter_after
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
    again = next(button for row in opened["reply_markup"].inline_keyboard for button in row
                 if button.callback_data.startswith("shop_chest:"))
    await shop.handle_shop_chest(callback.model_copy(update={"data": again.callback_data}), bot)
    assert Path(bot.edit_message_media.await_args.kwargs["media"].media.path).name == "chest_closed.jpg"


def test_closed_and_open_art_are_distinct_square_jpegs():
    root = Path(__file__).resolve().parents[1] / "assets/chests"
    assert (root / "chest_closed.jpg").read_bytes() != (root / "chest_open.jpg").read_bytes()
    for name in ("chest_closed.jpg", "chest_open.jpg"):
        with Image.open(root / name) as image:
            assert image.format == "JPEG" and image.mode == "RGB"
            assert not image.info.get("progressive")
            assert image.width == image.height and image.width >= 512
