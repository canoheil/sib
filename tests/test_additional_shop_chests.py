"""Approved four-chest expansion: data, art, registration and real reward delivery.

Archived definitions remain inspectable; only the Garden chest is still sold.
Reward-engine regressions use separate custom fixture codes for retired tables.
"""

import hashlib
import json
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import func, select

from sib.database.connection import seed_characters
from sib.database.models import Character, DailyChestClaim, PlayerCard, ShopChestOpening
from sib.handlers import shop as shop_handlers
from sib.scripts import shop_chests as cli
from sib.services.apple_wallet import AppleWalletService
from sib.services.assets import bundled_item_path
from sib.services.config_store import CONFIG_DEFAULTS
from sib.services.items import ITEM_CATALOG, ItemService
from sib.services.player_cards import PlayerCardService
from sib.services.shop_chests import (
    RETIRED_CHEST_CODES,
    ShopChestInactiveError,
    ShopChestInsufficientFundsError,
    ShopChestService,
    apply_chest_configs,
)
from sib.tests.test_green_apple_chest import FixedRoll
from sib.tests.test_parties import create_test_user

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "data/shop_chests/additional_chests.json"
STARTER_FILE = ROOT / "data/shop_chests/green_apple_chest.json"
CONFIGS = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
EXPECTED = {
    "enchanted_garden_chest": ("صندوق باغ افسون", 80, 0, {
        "magic_powder": 40, "apple_gold": 30, "apple_diamond": 8,
        "apple_obsidian": 2, "apples:40": 20,
    }),
    "empire_chest": ("صندوق امپراتوری", 200, 60, {
        "sib_sorkh": 14, "filo": 14, "goje": 14, "igris": 14,
        "zarush": 14, "flower": 19, "wedding_ring": 1, "apples:120": 10,
    }),
    "sages_chest": ("صندوق فرزانگان", 200, 60, {
        "sib_sabz": 14, "sib_talayi": 14, "green_knight": 14,
        "sage_king": 14, "treyni": 14, "magic_powder": 15,
        "apple_gold": 5, "apples:120": 10,
    }),
    "moonlight_chest": ("صندوق مهتاب", 200, 60, {
        "sib_mahi": 14, "mmung": 14, "moon_knight": 14,
        "moon_lily": 14, "wizard": 14, "apple_diamond": 15,
        "apple_obsidian": 5, "apples:120": 10,
    }),
}
FACTIONS = {
    "empire_chest": "empire",
    "sages_chest": "sages",
    "moonlight_chest": "moon_descendants",
}


def reward_key(reward):
    return (reward.get("character_code") or reward.get("item_key")
            or f"apples:{reward['apples']}")


async def register(session):
    await seed_characters(session)
    starter = json.loads(STARTER_FILE.read_text(encoding="utf-8"))
    await apply_chest_configs(session, starter)
    codes = await apply_chest_configs(session, CONFIGS)
    await session.commit()
    return codes


async def custom_reward_fixture(session, config):
    """Retain reward-table coverage without treating retired codes as buyable."""
    if config["code"] in RETIRED_CHEST_CODES:
        config = {**config, "code": f"custom_{config['code']}", "name": "Custom reward table"}
        await apply_chest_configs(session, [config])
        await session.commit()
    return await ShopChestService.get_by_code(session, config["code"])


async def funded_user(session, amount):
    user = await create_test_user(session, "expanded_chest_player")
    await AppleWalletService.earn(
        session, user.id, amount, reason="test_fund",
        idempotency_key=f"expansion-fund-{user.id}",
    )
    await session.commit()
    return user.id


def test_expansion_defines_exactly_the_four_approved_chests():
    assert [c["code"] for c in CONFIGS] == list(EXPECTED)
    assert [c["sort_order"] for c in CONFIGS] == [2, 3, 4, 5]
    for config in CONFIGS:
        name, price, duplicate, rewards = EXPECTED[config["code"]]
        assert config["name"] == name
        assert config["price_apples"] == price
        assert config["duplicate_apples"] == duplicate
        assert config["image_key"] == config["code"]
        assert config["is_active"] is True
        assert config["expected_version"] == 0
        assert len(config["rewards"]) == len(rewards)
        assert {reward_key(r): r["weight"] for r in config["rewards"]} == rewards
        assert sum(rewards.values()) == 100


def test_rewards_use_existing_items_and_released_cards_from_the_right_faction():
    seed = json.loads((ROOT / "data/characters_seed.json").read_text(encoding="utf-8"))
    characters = {c["code"]: c for c in seed}
    for config in CONFIGS:
        for reward in config["rewards"]:
            if reward["kind"] == "item":
                assert reward["item_key"] in ITEM_CATALOG
            elif reward["kind"] == "card":
                card = characters[reward["character_code"]]
                assert card["release_status"] == "RELEASED"
                assert card["shop_enabled"] is True
                assert card["faction"] == FACTIONS[config["code"]]


def test_each_chest_has_a_distinct_square_baseline_jpeg():
    fingerprints = set()
    for config in CONFIGS:
        path = ROOT / "assets/items" / f"{config['code']}.jpg"
        assert bundled_item_path(config["image_key"]) == path
        with Image.open(path) as image:
            assert image.format == "JPEG"
            assert image.size == (1024, 1024)
            assert image.mode == "RGB"
            assert not image.info.get("progressive")
            image.verify()
        fingerprints.add(hashlib.sha256(path.read_bytes()).hexdigest())
    assert len(fingerprints) == 4


def test_default_resale_expected_return_is_below_the_purchase_price():
    # A balance guard for repository defaults, not a guarantee about runtime
    # overrides or the subjective collection value of a new card.
    prices = CONFIG_DEFAULTS["item_sell_prices"]
    for config in CONFIGS:
        value = sum(
            Fraction(reward["weight"], 100) * (
                reward["apples"] if reward["kind"] == "apples" else
                prices.get(reward["item_key"], 0) if reward["kind"] == "item" else
                config["duplicate_apples"]
            )
            for reward in config["rewards"]
        )
        assert value < config["price_apples"]


@pytest.mark.asyncio
async def test_registration_keeps_historical_rows_but_only_renders_the_garden_chest(db_session):
    assert await register(db_session) == list(EXPECTED)
    user_id = await funded_user(db_session, 1000)
    chests = await ShopChestService.list_active(db_session)
    assert [c.code for c in chests] == ["enchanted_garden_chest"]
    text, keyboard = await shop_handlers._chests_view(user_id)
    callbacks = {b.callback_data for row in keyboard.inline_keyboard for b in row}
    for chest in chests:
        assert chest.name in text
        assert f"shop_chest:{chest.id}" in callbacks
        detail, kb, photo = await shop_handlers._chest_detail_view(user_id, chest.id)
        assert chest.name in detail
        assert photo is not None
        assert Path(photo.path) == bundled_item_path(chest.image_key)
        for spec, probability, label in await ShopChestService.describe(db_session, chest):
            assert label in detail
            assert probability == Fraction(spec.weight, 100)
        open_callback = next(
            b.callback_data for row in kb.inline_keyboard for b in row
            if b.callback_data.startswith("shop_chest_open:")
        )
        assert open_callback.startswith(f"shop_chest_open:{chest.id}:{chest.version}:")
        assert len(open_callback.encode()) <= 64


@pytest.mark.asyncio
@pytest.mark.parametrize("config", CONFIGS, ids=lambda c: c["code"])
async def test_every_reward_is_delivered_once_and_replay_never_charges_again(db_session, config):
    await register(db_session)
    chest = await custom_reward_fixture(db_session, config)
    roll = 0
    for index, reward in enumerate(config["rewards"]):
        user_id = await funded_user(db_session, 1000)
        key = f"expansion-{user_id}-{index}"
        result = await ShopChestService.open_chest_reliably(
            user_id, chest.id, key, rng=FixedRoll(roll), expected_version=chest.version,
        )
        assert result.reward_kind == reward["kind"]
        assert result.price_apples == config["price_apples"]
        expected_balance = 1000 - config["price_apples"]
        if reward["kind"] == "apples":
            assert result.apples_awarded == reward["apples"]
            expected_balance += reward["apples"]
        elif reward["kind"] == "item":
            assert result.item_key == reward["item_key"]
            assert await ItemService.get_total(db_session, user_id, reward["item_key"]) == 1
        else:
            character_id = await db_session.scalar(
                select(Character.id).where(Character.code == reward["character_code"])
            )
            assert not result.duplicate_converted
            assert await db_session.scalar(select(PlayerCard.id).where(
                PlayerCard.user_id == user_id, PlayerCard.character_id == character_id,
            )) is not None
        assert result.balance_after == expected_balance
        replay = await ShopChestService.open_chest_reliably(
            user_id, chest.id, key, rng=FixedRoll(99),
        )
        assert replay.replayed
        assert replay.balance_after == expected_balance
        assert await AppleWalletService.get_balance(db_session, user_id) == expected_balance
        if reward["kind"] == "item":
            assert await ItemService.get_total(db_session, user_id, reward["item_key"]) == 1
        assert await db_session.scalar(select(func.count()).select_from(ShopChestOpening).where(
            ShopChestOpening.user_id == user_id,
        )) == 1
        roll += reward["weight"]
    assert roll == 100
    assert await db_session.scalar(select(func.count()).select_from(DailyChestClaim)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("config", CONFIGS[1:], ids=lambda c: c["code"])
async def test_faction_chest_duplicate_converts_to_60_apples(db_session, config):
    await register(db_session)
    user_id = await funded_user(db_session, 1000)
    chest = await custom_reward_fixture(db_session, config)
    character_id = await db_session.scalar(select(Character.id).where(
        Character.code == config["rewards"][0]["character_code"],
    ))
    await PlayerCardService.acquire(db_session, user_id, character_id)
    await db_session.commit()
    result = await ShopChestService.open_chest_reliably(
        user_id, chest.id, f"duplicate-{user_id}", rng=FixedRoll(0),
    )
    assert result.duplicate_converted
    assert result.compensation_apples == 60
    assert await AppleWalletService.get_balance(db_session, user_id) == 860
    assert await db_session.scalar(select(func.count()).select_from(PlayerCard).where(
        PlayerCard.user_id == user_id,
    )) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("config", CONFIGS, ids=lambda c: c["code"])
async def test_insufficient_funds_cannot_open_new_chests(db_session, config):
    await register(db_session)
    user_id = await funded_user(db_session, config["price_apples"] - 1)
    chest = await ShopChestService.get_by_code(db_session, config["code"])
    error = ShopChestInactiveError if config["code"] in RETIRED_CHEST_CODES else ShopChestInsufficientFundsError
    with pytest.raises(error):
        await ShopChestService.open_chest_reliably(
            user_id, chest.id, f"no-funds-{user_id}", rng=FixedRoll(0),
        )
    assert await AppleWalletService.get_balance(db_session, user_id) == config["price_apples"] - 1
    assert await db_session.scalar(select(func.count()).select_from(ShopChestOpening)) == 0


@pytest.mark.asyncio
async def test_cli_dry_run_validates_expansion_without_registering_it(db_session, capsys):
    await seed_characters(db_session)
    await db_session.commit()
    assert await cli._cmd_apply(CONFIG_FILE, True) == 0
    assert await ShopChestService.list_active(db_session) == []
    output = capsys.readouterr().out
    for code in EXPECTED:
        assert code in output
