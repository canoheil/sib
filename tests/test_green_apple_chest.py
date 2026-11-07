"""Green Apple Chest ("صندوق سیبی"): the first shop chest.

Pins the shipped configuration (``data/shop_chests/green_apple_chest.json``),
its reward table and artwork reference for historical compatibility. Purchase,
duplicate, idempotency and daily-chest isolation regressions use the same reward
table under a clearly separate custom fixture code: the original is retired.
Live tier purchases and legacy sale refusal are tested in the tier/retirement suites.

SQLite runs everything except the two concurrency tests, which use
``requires_row_locks`` and are reported as SKIPPED on SQLite. A skip is not a
pass; those tests run only against MariaDB.
"""

import asyncio
import json
import random
from fractions import Fraction
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from sib.database.connection import get_async_sessionmaker, seed_characters
from sib.database.models import (
    AppleTransaction,
    Character,
    DailyChestClaim,
    PlayerCard,
    PlayerProgression,
    ReleaseStatus,
    ShopChest,
    ShopChestOpening,
)
from sib.services import shop_chests as chests_module
from sib.services.apple_wallet import AppleWalletService
from sib.services.assets import AssetService, bundled_item_path
from sib.services.characters import CharacterService
from sib.services.daily_chest import DailyChestService
from sib.services.items import ITEM_CATALOG, ItemService
from sib.services.player_cards import PlayerCardService
from sib.services.shop_chests import (
    RewardSpec,
    ShopChestConfigError,
    ShopChestInsufficientFundsError,
    ShopChestService,
    apply_chest_configs,
    probability_rows,
    validate_reward_table,
)
from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_user

REPO_ROOT = Path(__file__).resolve().parents[1]
CHEST_FILE = REPO_ROOT / "data" / "shop_chests" / "green_apple_chest.json"
CHEST_CODE = "green_apple_chest"
CHEST_NAME = "صندوق سیبی"
CUSTOM_CODE = "custom_apple_rewards"
IMAGE_FILE = REPO_ROOT / "assets" / "items" / "green_apple_chest.jpg"

CARD_CODES = ("sib_sorkh", "sib_sabz", "sib_talayi", "sib_mahi")
RING_KEY = "wedding_ring"
KEY = "green-apple-open-0001"

# Cumulative table order: cards 0-14, 15-29, 30-44, 45-59, ring 60, apples 61-99.
ROLL_BY_CARD = {"sib_sorkh": 0, "sib_sabz": 15, "sib_talayi": 30, "sib_mahi": 45}
ROLL_RING = 60
ROLL_APPLES = 61


class FixedRoll:
    """An RNG whose ``randrange`` always returns the same scripted roll."""

    def __init__(self, value: int):
        self.value = value

    def randrange(self, n: int) -> int:
        assert 0 <= self.value < n
        return self.value


def _config(*, custom=False) -> list:
    data = json.loads(CHEST_FILE.read_text(encoding="utf-8"))
    if custom:
        data[0].update(code=CUSTOM_CODE, name="Custom apple rewards")
    return data


def _jpeg_size(path: Path) -> tuple[int, int, int]:
    """(width, height, SOF marker) from a JPEG header, without Pillow."""
    data = path.read_bytes()
    assert data[:2] == b"\xff\xd8", "not a JPEG"
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height, marker
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # standalone markers
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    raise AssertionError("no SOF marker in JPEG")


async def _seed_cards(session) -> dict[str, int]:
    """Load the real starter roster (the four card codes) into this test DB."""
    await seed_characters(session)
    await session.commit()
    rows = (
        await session.execute(
            select(Character.code, Character.id).where(Character.code.in_(CARD_CODES))
        )
    ).all()
    ids = {code: cid for code, cid in rows}
    assert set(ids) == set(CARD_CODES), ids
    return ids


async def _apply_custom_rewards(session) -> list[str]:
    """Exercise the historical reward engine without re-enabling a retired code."""
    codes = await apply_chest_configs(session, _config(custom=True))
    await session.commit()
    return codes


async def _chest(session) -> ShopChest:
    chest = await ShopChestService.get_by_code(session, CUSTOM_CODE)
    assert chest is not None
    return chest


async def _user_with_apples(session, apples: int) -> int:
    user = await create_test_user(session, "apple_chest_player")
    user_id = user.id
    await AppleWalletService.earn(
        session, user_id, apples, reason="test_fund", idempotency_key=f"test-fund-{user_id}-{apples}"
    )
    await session.commit()
    return user_id


async def _balance(session, user_id: int) -> int:
    return await AppleWalletService.get_balance(session, user_id)


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


async def _cards_owned(session, user_id: int) -> list[int]:
    return [
        row
        for row in (
            await session.scalars(
                select(PlayerCard.character_id).where(PlayerCard.user_id == user_id)
            )
        ).all()
    ]


async def _open(user_id: int, chest_id: int, key: str, roll: int):
    return await ShopChestService.open_chest_reliably(user_id, chest_id, key, rng=FixedRoll(roll))


# ---------------------------------------------------------------------------
# Configuration and probabilities (no database)
# ---------------------------------------------------------------------------


def test_config_defines_exactly_one_active_chest_with_the_agreed_values():
    config = _config()
    assert isinstance(config, list) and len(config) == 1
    chest = config[0]
    assert chest["code"] == CHEST_CODE
    assert chest["name"] == CHEST_NAME
    assert chest["price_apples"] == 150
    assert chest["duplicate_apples"] == 50
    assert chest["image_key"] == CHEST_CODE
    assert chest["is_active"] is True
    assert chest["expected_version"] == 0


def test_reward_table_weights_are_exactly_the_agreed_split_and_total_100():
    rewards = _config()[0]["rewards"]
    weights = {}
    for entry in rewards:
        label = entry.get("character_code") or entry.get("item_key") or f"apples:{entry['apples']}"
        assert label not in weights, f"duplicate reward row {label}"
        weights[label] = entry["weight"]

    assert weights == {
        "sib_sorkh": 15,
        "sib_sabz": 15,
        "sib_talayi": 15,
        "sib_mahi": 15,
        "wedding_ring": 1,
        "apples:100": 39,
    }
    assert sum(weights.values()) == 100

    # Exact probabilities, through the same validator and probability code the
    # chest uses on save and on display.
    specs = []
    for index, entry in enumerate(rewards):
        if entry["kind"] == "card":
            specs.append(RewardSpec(kind="card", weight=entry["weight"], character_id=index + 1))
        elif entry["kind"] == "item":
            specs.append(RewardSpec(kind="item", weight=entry["weight"], item_key=entry["item_key"]))
        else:
            specs.append(RewardSpec(kind="apples", weight=entry["weight"], apples=entry["apples"]))
    validate_reward_table(specs)
    rows = probability_rows(specs)
    probabilities = [p for _spec, p in rows]
    assert probabilities == [
        Fraction(15, 100),
        Fraction(15, 100),
        Fraction(15, 100),
        Fraction(15, 100),
        Fraction(1, 100),
        Fraction(39, 100),
    ]
    assert sum(probabilities) == 1


def test_card_codes_and_ring_key_are_real_audited_identifiers():
    seed = json.loads((REPO_ROOT / "data" / "characters_seed.json").read_text(encoding="utf-8"))
    by_code = {entry["code"]: entry for entry in seed}
    for code in CARD_CODES:
        assert code in by_code, code
        assert by_code[code]["release_status"] == ReleaseStatus.RELEASED
        assert by_code[code]["shop_enabled"] is True
    assert RING_KEY in ITEM_CATALOG


def test_artwork_is_a_square_jpeg_and_is_the_image_the_chest_references():
    chest = _config()[0]
    # The image key is a bare key; the loader maps it to assets/items/<key>.jpg.
    assert "/" not in chest["image_key"] and ".." not in chest["image_key"]
    assert bundled_item_path(chest["image_key"]) == IMAGE_FILE
    assert IMAGE_FILE.is_file()

    width, height, marker = _jpeg_size(IMAGE_FILE)
    assert (width, height) == (1024, 1024)
    assert marker == 0xC0  # baseline, like the other item icons


# ---------------------------------------------------------------------------
# Registration through the existing apply path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_reward_fixture_registers_with_resolved_card_ids(db_session):
    ids = await _seed_cards(db_session)
    assert await _apply_custom_rewards(db_session) == [CUSTOM_CODE]

    active = (
        await db_session.scalars(select(ShopChest).where(ShopChest.is_active.is_(True)))
    ).all()
    assert [c.code for c in active] == [CUSTOM_CODE]

    chest = await _chest(db_session)
    assert chest.name == "Custom apple rewards" and chest.price_apples == 150
    assert chest.duplicate_apples == 50 and chest.image_key == CHEST_CODE
    rewards = await ShopChestService.validate_chest(db_session, chest)
    by_card = {r.character_id: r.weight for r in rewards if r.kind == "card"}
    assert by_card == {ids[code]: 15 for code in CARD_CODES}
    assert [(r.kind, r.weight) for r in rewards if r.kind != "card"] == [("item", 1), ("apples", 39)]
    assert next(r for r in rewards if r.kind == "apples").apples == 100
    assert next(r for r in rewards if r.kind == "item").item_key == RING_KEY


@pytest.mark.asyncio
async def test_character_code_missing_from_this_database_is_refused_and_writes_nothing(db_session):
    await _seed_cards(db_session)
    config = _config(custom=True)
    config[0]["rewards"][0]["character_code"] = "sib_does_not_exist"

    with pytest.raises(ShopChestConfigError):
        await apply_chest_configs(db_session, config)
    await db_session.rollback()

    async with get_async_sessionmaker()() as session:
        assert await ShopChestService.get_by_code(session, CUSTOM_CODE) is None


@pytest.mark.asyncio
async def test_character_code_and_character_id_together_are_refused(db_session):
    ids = await _seed_cards(db_session)
    config = _config(custom=True)
    config[0]["rewards"][0]["character_id"] = ids["sib_sorkh"]

    with pytest.raises(ShopChestConfigError):
        await apply_chest_configs(db_session, config)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_unreleased_card_code_is_refused_by_eligibility(db_session):
    ids = await _seed_cards(db_session)
    await CharacterService.set_release_status(db_session, ids["sib_mahi"], ReleaseStatus.DRAFT)
    await db_session.commit()

    with pytest.raises(ShopChestConfigError):
        await apply_chest_configs(db_session, _config(custom=True))
    await db_session.rollback()

    async with get_async_sessionmaker()() as session:
        assert await ShopChestService.get_by_code(session, CUSTOM_CODE) is None


@pytest.mark.asyncio
async def test_cli_dry_run_accepts_the_shipped_file_and_writes_nothing(db_session, monkeypatch, capsys):
    from sib.scripts import shop_chests as cli

    await _seed_cards(db_session)
    monkeypatch.setattr(cli, "get_async_sessionmaker", get_async_sessionmaker)

    assert await cli._cmd_apply(CHEST_FILE, True) == 0
    assert "dry run ok" in capsys.readouterr().out
    async with get_async_sessionmaker()() as session:
        assert await ShopChestService.get_by_code(session, CHEST_CODE) is None


# ---------------------------------------------------------------------------
# Purchases and outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purchase_charges_150_and_credits_100_apples(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 150)

    result = await _open(user_id, chest.id, KEY, ROLL_APPLES)

    assert result.reward_kind == "apples" and result.apples_awarded == 100
    assert result.price_apples == 150 and result.replayed is False
    assert result.balance_after == 100
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 100
        openings = await _openings(session, user_id)
        assert len(openings) == 1 and openings[0].reward_apples == 100
        assert openings[0].price_apples == 150


@pytest.mark.asyncio
async def test_duplicate_card_converts_to_50_apples_once(db_session):
    ids = await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 150)
    await PlayerCardService.acquire(db_session, user_id, ids["sib_sorkh"])
    await db_session.commit()

    result = await _open(user_id, chest.id, KEY, ROLL_BY_CARD["sib_sorkh"])

    assert result.reward_kind == "card" and result.character_id == ids["sib_sorkh"]
    assert result.duplicate_converted is True and result.compensation_apples == 50
    assert result.balance_after == 50  # 150 - 150 + 50, credited exactly once
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 50
        assert await _cards_owned(session, user_id) == [ids["sib_sorkh"]]  # no second copy
        openings = await _openings(session, user_id)
        assert len(openings) == 1 and openings[0].compensation_apples == 50


@pytest.mark.asyncio
async def test_new_card_is_granted_normally_with_collection_xp(db_session):
    ids = await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 150)

    result = await _open(user_id, chest.id, KEY, ROLL_BY_CARD["sib_mahi"])

    assert result.reward_kind == "card" and result.character_id == ids["sib_mahi"]
    assert result.duplicate_converted is False and result.compensation_apples == 0
    assert result.balance_after == 0
    async with get_async_sessionmaker()() as session:
        assert await _cards_owned(session, user_id) == [ids["sib_mahi"]]
        progression = await session.scalar(
            select(PlayerProgression).where(PlayerProgression.user_id == user_id)
        )
        assert progression is not None and progression.total_xp > 0


@pytest.mark.asyncio
async def test_ring_is_granted_through_the_item_mechanism(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 150)

    result = await _open(user_id, chest.id, KEY, ROLL_RING)

    assert result.reward_kind == "item" and result.item_key == RING_KEY
    assert result.apples_awarded == 0 and result.balance_after == 0
    async with get_async_sessionmaker()() as session:
        assert await ItemService.get_total(session, user_id, RING_KEY) == 1


# ---------------------------------------------------------------------------
# Refusals: nothing charged, granted or recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_balance_writes_no_charge_reward_or_success_record(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 149)

    for roll in (ROLL_BY_CARD["sib_sorkh"], ROLL_RING, ROLL_APPLES):
        with pytest.raises(ShopChestInsufficientFundsError):
            await _open(user_id, chest.id, f"{KEY}-{roll:03d}", roll)

    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 149
        assert await _openings(session, user_id) == []
        assert await _cards_owned(session, user_id) == []
        assert await ItemService.get_total(session, user_id, RING_KEY) == 0
        charges = (
            await session.scalars(
                select(AppleTransaction).where(AppleTransaction.user_id == user_id)
            )
        ).all()
        assert [t.amount for t in charges] == [149]  # only the funding row


@pytest.mark.asyncio
async def test_same_key_replay_has_no_effect_even_with_a_different_draw(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 300)

    first = await _open(user_id, chest.id, KEY, ROLL_APPLES)
    replay = await _open(user_id, chest.id, KEY, ROLL_BY_CARD["sib_sorkh"])

    assert replay.replayed is True and replay.opening_id == first.opening_id
    assert replay.reward_kind == "apples" and replay.apples_awarded == 100
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 250  # 300 - 150 + 100, once
        assert len(await _openings(session, user_id)) == 1
        assert await _cards_owned(session, user_id) == []


@pytest.mark.asyncio
async def test_distinct_keys_each_open_and_each_charge(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 300)

    first = await _open(user_id, chest.id, "green-apple-open-a", ROLL_APPLES)
    second = await _open(user_id, chest.id, "green-apple-open-b", ROLL_APPLES)

    assert first.opening_id != second.opening_id
    assert second.replayed is False
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 200  # 300 - 150 + 100 - 150 + 100
        assert len(await _openings(session, user_id)) == 2


@pytest.mark.asyncio
async def test_ambiguous_commit_that_did_commit_is_reconciled_not_repeated(db_session, monkeypatch):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 150)
    real_commit = chests_module.commit_chest_transaction
    calls = {"n": 0}

    async def commit_then_lose_ack(session):
        calls["n"] += 1
        await real_commit(session)
        raise OperationalError("COMMIT", {}, RuntimeError("connection lost after COMMIT"))

    monkeypatch.setattr(chests_module, "commit_chest_transaction", commit_then_lose_ack)
    result = await _open(user_id, chest.id, KEY, ROLL_APPLES)

    assert calls["n"] == 1  # reconciled from the database, never re-committed
    assert result.replayed is True and result.apples_awarded == 100
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 100
        assert len(await _openings(session, user_id)) == 1


@pytest.mark.asyncio
async def test_ambiguous_commit_that_did_not_commit_is_retried_with_the_same_key(db_session, monkeypatch):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 150)
    real_commit = chests_module.commit_chest_transaction
    calls = {"n": 0}
    keys_seen = []

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
    result = await _open(user_id, chest.id, KEY, ROLL_APPLES)

    assert len(keys_seen) == 2 and len(set(keys_seen)) == 1  # same key on the retry
    assert result.replayed is False and calls["n"] == 2
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 100
        assert len(await _openings(session, user_id)) == 1


# ---------------------------------------------------------------------------
# Independence and presentation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_reward_chest_does_not_touch_the_daily_chest(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 150)

    await _open(user_id, chest.id, KEY, ROLL_APPLES)

    async with get_async_sessionmaker()() as session:
        claims = (
            await session.scalars(select(DailyChestClaim).where(DailyChestClaim.user_id == user_id))
        ).all()
        assert claims == []

    # The daily chest still claims normally, under its own key namespace.
    async with get_async_sessionmaker()() as session:
        result = await DailyChestService.claim(session, user_id, rng=random.Random(3))
        await session.commit()
    assert result.claim is not None


@pytest.mark.asyncio
async def test_chest_image_reference_resolves_to_the_bundled_artwork(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)

    assert chest.image_key == CHEST_CODE
    file_id, path = await AssetService.resolve_photo(db_session, chest.image_key)
    assert file_id is None
    assert path == IMAGE_FILE and path.is_file()


# ---------------------------------------------------------------------------
# Concurrency (MariaDB only; SKIPPED on SQLite, never reported as passed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_same_key_requests_charge_once(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 150)

    results = await asyncio.gather(
        *[_open(user_id, chest.id, KEY, ROLL_APPLES) for _ in range(4)],
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) for r in results), results
    assert len({r.opening_id for r in results}) == 1
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 100


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_distinct_keys_cannot_overspend(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 150)

    results = await asyncio.gather(
        *[_open(user_id, chest.id, f"green-apple-race-{i:04d}", ROLL_APPLES) for i in range(3)],
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) == 1
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 100


# ---------------------------------------------------------------------------
# Stale pages, stale callbacks and version-checked admin writes
# ---------------------------------------------------------------------------


async def _reprice(session, price: int, expected_version: int) -> None:
    """Apply the shipped file with a new price, as an operator would."""
    config = _config(custom=True)
    config[0]["price_apples"] = price
    config[0]["expected_version"] = expected_version
    await apply_chest_configs(session, config)
    await session.commit()


async def _detail_open_callback(user_id: int, chest_id: int) -> str:
    from sib.handlers import shop as shop_handlers

    view = await shop_handlers._chest_detail_view(user_id, chest_id)
    assert view is not None
    _text, kb, _photo = view
    return next(
        b.callback_data
        for row in kb.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("shop_chest_open:")
    )


@pytest.mark.asyncio
async def test_stale_open_page_after_a_price_change_is_refused_without_charge(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 500)
    shown_version = chest.version

    async with get_async_sessionmaker()() as session:
        await _reprice(session, 300, expected_version=shown_version)

    with pytest.raises(chests_module.ShopChestError) as refused:
        await ShopChestService.open_chest_reliably(
            user_id, chest.id, KEY, rng=FixedRoll(ROLL_APPLES), expected_version=shown_version
        )
    assert str(refused.value) == chests_module.STALE_OPEN_TEXT

    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 500
        assert await _openings(session, user_id) == []


@pytest.mark.asyncio
async def test_open_with_the_current_version_charges_the_current_price(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 500)
    async with get_async_sessionmaker()() as session:
        await _reprice(session, 300, expected_version=chest.version)
    async with get_async_sessionmaker()() as session:
        current = await _chest(session)

    result = await ShopChestService.open_chest_reliably(
        user_id, current.id, KEY, rng=FixedRoll(ROLL_APPLES), expected_version=current.version
    )

    assert result.price_apples == 300 and result.balance_after == 300  # 500 - 300 + 100
    assert result.chest_version == current.version


@pytest.mark.asyncio
async def test_replay_of_a_recorded_opening_survives_later_changes(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 300)
    first = await ShopChestService.open_chest_reliably(
        user_id, chest.id, KEY, rng=FixedRoll(ROLL_APPLES), expected_version=chest.version
    )
    async with get_async_sessionmaker()() as session:
        await _reprice(session, 999, expected_version=chest.version)

    replay = await ShopChestService.open_chest_reliably(
        user_id, chest.id, KEY, rng=FixedRoll(ROLL_APPLES), expected_version=chest.version
    )

    assert replay.replayed is True and replay.opening_id == first.opening_id
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 250  # charged once, at 150


@pytest.mark.asyncio
async def test_handler_refuses_a_stale_open_button_and_charges_nothing(db_session):
    from sib.handlers import shop as shop_handlers
    from sib.tests.test_shop_sections import _run_handler, _sell_callback

    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 500)
    open_cb = await _detail_open_callback(user_id, chest.id)  # the page the player saw

    async with get_async_sessionmaker()() as session:
        await _reprice(session, 300, expected_version=chest.version)

    cb = _sell_callback(user_id, open_cb)
    edit, _banner = await _run_handler(shop_handlers.handle_shop_chest_open, cb)

    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.await_args
    assert args[0] == chests_module.STALE_OPEN_TEXT and kwargs.get("show_alert") is True
    edit.assert_not_awaited()
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 500
        assert await _openings(session, user_id) == []


@pytest.mark.asyncio
async def test_legacy_open_callback_without_a_version_is_refused(db_session):
    from sib.handlers import shop as shop_handlers
    from sib.tests.test_shop_sections import _run_handler, _sell_callback

    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 500)

    cb = _sell_callback(user_id, f"shop_chest_open:{chest.id}:{KEY}")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_chest_open, cb)

    args, _kwargs = cb.answer.await_args
    assert args[0] == chests_module.STALE_OPEN_TEXT
    edit.assert_not_awaited()
    async with get_async_sessionmaker()() as session:
        assert await _balance(session, user_id) == 500
        assert await _openings(session, user_id) == []


@pytest.mark.asyncio
async def test_open_callback_carries_the_version_and_fits_telegram_limit(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    user_id = await _user_with_apples(db_session, 500)

    open_cb = await _detail_open_callback(user_id, chest.id)

    parts = open_cb.split(":")
    assert len(parts) == 4 and parts[1] == str(chest.id) and parts[2] == str(chest.version)
    assert len(open_cb.encode()) <= 64
    # Bounds: ids below 10**12, versions up to INT max (10 digits), and the
    # 12-character nonce from secrets.token_urlsafe(9). That is 52 bytes.
    worst = f"shop_chest_open:{10**12 - 1}:{2**31 - 1}:{'x' * 12}"
    assert len(worst.encode()) <= 64


@pytest.mark.asyncio
async def test_set_active_refuses_a_stale_version_even_if_the_row_was_loaded_earlier(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    factory = get_async_sessionmaker()

    async with factory() as stale:
        loaded = await ShopChestService.get_chest(stale, chest.id)  # version 1, kept in memory
        assert loaded.version == 1
        async with factory() as other:
            await ShopChestService.set_active(other, chest.id, False, expected_version=1)
            await other.commit()  # now version 2, inactive

        with pytest.raises(ShopChestConfigError):
            await ShopChestService.set_active(stale, chest.id, True, expected_version=1)
        await stale.rollback()

    async with factory() as session:
        fresh = await ShopChestService.get_by_code(session, CUSTOM_CODE)
        assert fresh.version == 2 and fresh.is_active is False


@pytest.mark.asyncio
async def test_save_chest_refuses_a_stale_version_even_if_the_row_was_loaded_earlier(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    factory = get_async_sessionmaker()

    async with factory() as stale:
        assert (await ShopChestService.get_chest(stale, chest.id)).version == 1
        async with factory() as other:
            await _reprice(other, 300, expected_version=1)  # version 2

        with pytest.raises(ShopChestConfigError):
            await _reprice(stale, 400, expected_version=1)
        await stale.rollback()

    async with factory() as session:
        fresh = await ShopChestService.get_by_code(session, CUSTOM_CODE)
        assert fresh.version == 2 and fresh.price_apples == 300


@pytest.mark.asyncio
async def test_cli_set_active_with_a_version_takes_the_row_lock(db_session, monkeypatch):
    from sib.scripts import shop_chests as cli

    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    seen = []
    real_get_by_code = ShopChestService.get_by_code

    async def spy(session, code, *, lock=False):
        seen.append(lock)
        return await real_get_by_code(session, code, lock=lock)

    monkeypatch.setattr(cli, "get_async_sessionmaker", get_async_sessionmaker)
    monkeypatch.setattr(ShopChestService, "get_by_code", staticmethod(spy))
    await cli._cmd_set_active(CUSTOM_CODE, True, 1, confirm_database=_test_database_name())

    assert seen and all(seen)  # the lookup used for a versioned write is locked


def _test_database_name() -> str:
    import os

    from sqlalchemy.engine import make_url

    return make_url(os.environ["sib_TEST_DATABASE_URL"]).database


@pytest.mark.asyncio
async def test_locked_code_lookup_returns_the_stored_row_not_a_stale_copy(db_session):
    await _seed_cards(db_session)
    await _apply_custom_rewards(db_session)
    chest = await _chest(db_session)
    factory = get_async_sessionmaker()

    async with factory() as stale:
        assert (await ShopChestService.get_chest(stale, chest.id)).version == 1
        async with factory() as other:
            await _reprice(other, 300, expected_version=1)  # stored version 2
        locked = await ShopChestService.get_by_code(stale, CUSTOM_CODE, lock=True)
        version, price = locked.version, locked.price_apples  # read before rollback expires it
        await stale.rollback()

    assert version == 2 and price == 300
