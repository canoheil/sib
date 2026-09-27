"""Spec #15-17, #9-12: central asset registry + item system + drop table."""

import pytest
from sqlalchemy import select

from sib.database.models import BotAsset, User, UserItem
from sib.services.assets import AssetService
from sib.services.items import (
    DEFAULT_DROP_TABLE,
    DropService,
    ItemService,
    roll_drop,
)


async def _mkuser(session, user_id: int):
    if await session.get(User, user_id) is None:
        session.add(User(id=user_id, first_name=f"کاربر{user_id}", language_code="fa"))
        await session.flush()


# ---------------------------------------------------------------------------
# Asset registry (spec #15-17)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_and_get_asset(db_session):
    asset = await AssetService.register(
        db_session, "golden_apple", "file-abc", type="sticker", description="a"
    )
    await db_session.commit()
    assert asset.id is not None

    got = await AssetService.get(db_session, "golden_apple")
    assert got is not None and got.file_id == "file-abc"
    assert await AssetService.file_id(db_session, "golden_apple") == "file-abc"


@pytest.mark.asyncio
async def test_asset_replacement_without_touching_consumers(db_session):
    """Spec #17: swapping the file_id is an upsert under the same key."""
    await AssetService.register(db_session, "golden_apple", "file-1", type="sticker")
    await db_session.commit()
    # Replace.
    asset = await AssetService.register(db_session, "golden_apple", "file-2", type="photo")
    await db_session.commit()

    rows = (await db_session.execute(select(BotAsset))).scalars().all()
    assert len(rows) == 1  # single row, key is unique
    assert asset.file_id == "file-2"
    assert asset.type == "photo"
    assert await AssetService.file_id(db_session, "golden_apple") == "file-2"


@pytest.mark.asyncio
async def test_missing_asset_is_graceful(db_session):
    assert await AssetService.file_id(db_session, "nope") is None
    assert await AssetService.register(
        db_session, "x", "f", type="whatever"
    ) is not None  # type is free-form


# ---------------------------------------------------------------------------
# Item inventory (spec #9-11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_items_are_real_inventory_rows(db_session):
    await _mkuser(db_session, 7)
    await _mkuser(db_session, 8)
    await ItemService.grant(db_session, 7, "apple_gold", 1)
    await ItemService.grant(db_session, 7, "apple_gold", 2)
    await ItemService.grant(db_session, 8, "apple_normal", 5)
    await db_session.commit()

    assert await ItemService.get_total(db_session, 7, "apple_gold") == 3
    assert await ItemService.get_total(db_session, 8, "apple_gold") == 0
    items = await ItemService.list_items(db_session, 7)
    assert items == {"apple_gold": 3}
    # Persistence: a fresh query sees the rows.
    rows = (await db_session.execute(select(UserItem))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_unknown_item_rejected(db_session):
    with pytest.raises(ValueError):
        await ItemService.grant(db_session, 1, "not_an_item")


@pytest.mark.asyncio
async def test_summary_line(db_session):
    await _mkuser(db_session, 42)
    line = await ItemService.summary_line(db_session, 42)
    assert line == "—"
    await ItemService.grant(db_session, 42, "apple_normal", 2)
    line = await ItemService.summary_line(db_session, 42)
    assert "🍎×2" in line


# ---------------------------------------------------------------------------
# Drop table (spec #9-12)
# ---------------------------------------------------------------------------


def test_default_table_matches_spec():
    keys = {e["key"]: e["weight"] for e in DEFAULT_DROP_TABLE}
    assert keys == {"apple_normal": 94, "apple_gold": 5, "apple_obsidian": 1}
    # Spec messages verbatim.
    by_key = {e["key"]: e for e in DEFAULT_DROP_TABLE}
    assert "✨ شانست گرفت!" in by_key["apple_gold"]["message"]
    assert "یک سیب طلایی پیدا کردی!" in by_key["apple_gold"]["message"]
    assert "🖤 باورکردنی نیست..." in by_key["apple_obsidian"]["message"]
    assert "یک سیب ابسیدینی پیدا کردی!" in by_key["apple_obsidian"]["message"]


class _SeqRng:
    """Deterministic rng: roll() returns pre-set floats in [0, 1)."""

    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)


def test_roll_drop_probabilities():
    # 94/5/1 boundaries: [0, .94) normal, [.94, .99) gold, [.99, 1) obsidian.
    assert roll_drop(DEFAULT_DROP_TABLE, _SeqRng(0.0))["key"] == "apple_normal"
    assert roll_drop(DEFAULT_DROP_TABLE, _SeqRng(0.939))["key"] == "apple_normal"
    assert roll_drop(DEFAULT_DROP_TABLE, _SeqRng(0.94))["key"] == "apple_gold"
    assert roll_drop(DEFAULT_DROP_TABLE, _SeqRng(0.989))["key"] == "apple_gold"
    assert roll_drop(DEFAULT_DROP_TABLE, _SeqRng(0.99))["key"] == "apple_obsidian"
    assert roll_drop(DEFAULT_DROP_TABLE, _SeqRng(0.9999))["key"] == "apple_obsidian"


def test_roll_drop_relative_weights_and_extensibility():
    """Spec #12: new items (emerald 0.5%, fire 2%...) = config only.

    Weights may be arbitrary - they are RELATIVE and normalized.
    """
    table = [
        {"key": "apple_normal", "weight": 924, "message": "a"},
        {"key": "apple_gold", "weight": 50, "message": "b"},
        {"key": "apple_obsidian", "weight": 10, "message": "c"},
        {"key": "apple_fire", "weight": 20, "message": "d"},        # 2%
        {"key": "apple_emerald", "weight": 5, "message": "e"},      # 0.5%
    ]
    # Boundaries after normalization by 1009:
    # normal <0.9158, gold <0.9653, obsidian <0.9752, fire <0.9950, emerald <1.
    assert roll_drop(table, _SeqRng(0.0))["key"] == "apple_normal"
    assert roll_drop(table, _SeqRng(0.92))["key"] == "apple_gold"
    assert roll_drop(table, _SeqRng(0.97))["key"] == "apple_obsidian"
    assert roll_drop(table, _SeqRng(0.98))["key"] == "apple_fire"
    assert roll_drop(table, _SeqRng(0.996))["key"] == "apple_emerald"


@pytest.mark.asyncio
async def test_drop_service_persists_and_uses_config(db_session):
    await _mkuser(db_session, 11)
    drop = await DropService.roll_harvest_drop(db_session, 11, rng=_SeqRng(0.95))
    await db_session.commit()
    assert drop["key"] == "apple_gold"
    assert await ItemService.get_total(db_session, 11, "apple_gold") == 1

    # Config-driven: override the table and behavior changes (spec #9).
    from sib.services.config_store import ConfigService

    await ConfigService.set(
        db_session,
        "garden_harvest_drops",
        [{"key": "apple_obsidian", "weight": 1, "message": "custom"}],
    )  # data-driven: behavior changes without touching harvest logic
    drop2 = await DropService.roll_harvest_drop(db_session, 11, rng=_SeqRng(0.5))
    await db_session.commit()
    assert drop2["key"] == "apple_obsidian"
    assert drop2["message"] == "custom"
    assert await ItemService.get_total(db_session, 11, "apple_obsidian") == 1
