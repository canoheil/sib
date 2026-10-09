"""Spec #15-17, #9-12: central asset registry + item system + drop table."""

import pytest
from sqlalchemy import select

from sib.database.models import BotAsset, User, UserItem
from sib.services.assets import AssetService, bundled_item_path
from sib.services.items import (
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
# Bundled item artwork fallback (spec #16: assets ship with the code)
# ---------------------------------------------------------------------------


def test_bundled_item_path_for_known_items():
    for key in ("magic_powder", "golden_apple", "diamond_apple", "obsidian_apple"):
        path = bundled_item_path(key)
        assert path is not None and path.is_file(), key
        assert path.suffix == ".jpg"


def test_bundled_item_path_rejects_traversal():
    assert bundled_item_path("../alembic.ini") is None
    assert bundled_item_path("a/b") is None
    assert bundled_item_path("") is None
    assert bundled_item_path("not_a_bundled_key") is None


@pytest.mark.asyncio
async def test_resolve_photo_prefers_registered_file_id(db_session):
    """A DB registration overrides the bundled file (runtime replaceable)."""
    await AssetService.register(db_session, "golden_apple", "file-9", type="photo")
    await db_session.commit()

    file_id, fs_path = await AssetService.resolve_photo(db_session, "golden_apple")
    assert file_id == "file-9"
    assert fs_path is None


@pytest.mark.asyncio
async def test_resolve_photo_falls_back_to_bundled_file(db_session):
    file_id, fs_path = await AssetService.resolve_photo(db_session, "golden_apple")
    assert file_id is None
    assert fs_path is not None and fs_path.is_file()


@pytest.mark.asyncio
async def test_resolve_photo_unknown_key_is_graceful(db_session):
    assert await AssetService.resolve_photo(db_session, "unknown_key") == (None, None)


# ---------------------------------------------------------------------------
# Item inventory (spec #9-11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_items_are_real_inventory_rows(db_session):
    await _mkuser(db_session, 7)
    await _mkuser(db_session, 8)
    await ItemService.grant(db_session, 7, "apple_gold", 1)
    await ItemService.grant(db_session, 7, "apple_gold", 2)
    await ItemService.grant(db_session, 8, "apple_diamond", 5)
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
    await ItemService.grant(db_session, 42, "apple_gold", 2)
    line = await ItemService.summary_line(db_session, 42)
    assert "🌕×2" in line


@pytest.mark.asyncio
async def test_consume_rejects_short_inventory(db_session):
    await _mkuser(db_session, 9)
    await ItemService.grant(db_session, 9, "magic_powder", 1)
    await db_session.commit()

    with pytest.raises(ValueError):
        await ItemService.consume(db_session, 9, "magic_powder", 2)
    await db_session.rollback()

    remaining = await ItemService.consume(db_session, 9, "magic_powder", 1)
    assert remaining == 0


# ---------------------------------------------------------------------------
# Drop table (spec #9-12): per-magic-level weights, config-driven
# ---------------------------------------------------------------------------


class _SeqRng:
    """Deterministic rng: roll() returns pre-set floats in [0, 1)."""

    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)


def test_roll_drop_relative_weights():
    """Weights are RELATIVE and normalized - arbitrary numbers work."""
    table = {"apple_normal": 70, "apple_gold": 20, "apple_diamond": 9, "apple_obsidian": 1}
    # Boundaries after normalization by 100:
    assert roll_drop(table, _SeqRng(0.0))[0] == "apple_normal"
    assert roll_drop(table, _SeqRng(0.699))[0] == "apple_normal"
    assert roll_drop(table, _SeqRng(0.70))[0] == "apple_gold"
    assert roll_drop(table, _SeqRng(0.899))[0] == "apple_gold"
    assert roll_drop(table, _SeqRng(0.90))[0] == "apple_diamond"
    assert roll_drop(table, _SeqRng(0.989))[0] == "apple_diamond"
    assert roll_drop(table, _SeqRng(0.99))[0] == "apple_obsidian"
    assert roll_drop(table, _SeqRng(0.9999))[0] == "apple_obsidian"


def test_roll_drop_empty_table_is_none():
    assert roll_drop({}, _SeqRng(0.5)) is None
    assert roll_drop({"x": 0.0}, _SeqRng(0.5)) is None  # zero total weight


@pytest.mark.asyncio
async def test_weights_for_level_follow_config(db_session):
    """Magic levels shift the weights toward rare outcomes (spec #12)."""
    l0 = await DropService.weights_for_level(db_session, 0)
    l5 = await DropService.weights_for_level(db_session, 5)
    assert l0["apple_normal"] == 70 and l0["apple_obsidian"] == 1
    assert l5["apple_normal"] == 15 and l5["apple_obsidian"] == 17
    # Beyond the cap the level is clamped to the maximum.
    l9 = await DropService.weights_for_level(db_session, 9)
    assert l9 == l5


@pytest.mark.asyncio
async def test_roll_returns_presentation_entry(db_session):
    """DropService.roll pairs the rolled key with its presentation."""
    key, entry = await DropService.roll(db_session, 0, rng=_SeqRng(0.99))
    assert key == "apple_obsidian"
    assert "باورکردنی نیست" in entry["message"]
    assert entry["asset"] == "obsidian_apple"

    key, entry = await DropService.roll(db_session, 5, rng=_SeqRng(0.0))
    assert key == "apple_normal"  # currency outcome at the table's head
    assert "معمولی" in entry["message"]


@pytest.mark.asyncio
async def test_drop_config_is_extensible(db_session):
    """Spec #12: a new outcome is config only - no code change."""
    from sib.services.config_store import ConfigService

    await ConfigService.set(
        db_session,
        "garden_drop_weights",
        {"0": {"apple_normal": 99, "apple_gold": 1}},
    )
    await ConfigService.set(
        db_session,
        "garden_drop_entries",
        {
            "apple_normal": {"message": "custom normal"},
            "apple_gold": {"message": "custom gold", "asset": "golden_apple"},
        },
    )

    key, entry = await DropService.roll(db_session, 0, rng=_SeqRng(0.995))
    assert key == "apple_gold"
    assert entry["message"] == "custom gold"
    assert entry["asset"] == "golden_apple"
