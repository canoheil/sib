"""HW1-IMPL-1 foundation tests: schema/migration 057, pumpkin_seed item, exclusive card Amber, and assets."""

import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from PIL import Image
import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from sib.database.base import Base
from sib.database.connection import seed_characters
from sib.database.models import (
    Character,
    Group,
    HalloweenAttack,
    HalloweenEventState,
    HalloweenLootDrop,
    HalloweenLootDropClaim,
    HalloweenRewardClaim,
    HalloweenSeedCooldown,
    PlayerCard,
    ReleaseStatus,
    SystemConfig,
    User,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.assets import AssetService, bundled_item_path
from sib.services.card_progression import MAX_CARD_LEVEL, attributes_at_level, base_attributes
from sib.services.card_upgrade import CardUpgradeService
from sib.services.character_art import artwork_path, resolve_character_photo
from sib.services.config_store import CONFIG_DEFAULTS, ConfigService
from sib.services.daily_chest import DailyChestService
from sib.services.items import ITEM_CATALOG, ItemService, item_emoji, item_label
from sib.services.player_cards import PlayerCardService
from sib.services.shop import ShopService
from sib.services.wiki import WikiService
from sib.tests.test_parties import create_test_user

REPO_ROOT = Path(__file__).resolve().parents[1]

AMBER_LORE = (
    "«امبر» جادوگر سرزمین‌های فراموش‌شده است. او که با رعد و برق و ماه کامل "
    "پیوند خورده، تنها کسی است که جرأت کرده طلسم‌های ممنوعه را برای مقابله با "
    "سیبومورتیس به کار ببرد. جغد بنفش او، دیده‌بان ارتش اواریاست و هیچ سایه‌ای "
    "از نگاهش پنهان نمی‌ماند."
)


def _load_migration_057():
    path = REPO_ROOT / "database/migrations/versions/057_halloween_event.py"
    spec = importlib.util.spec_from_file_location("migration_057", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. Migration 057: 6 tables, constraints, indexes, config seed, downgrade
# ---------------------------------------------------------------------------


def test_migration_057_creates_tables_constraints_indexes_and_downgrades():
    migration = _load_migration_057()
    assert migration.revision == "057_halloween_event"
    assert migration.down_revision == "056_garden_wither_grace_hours"

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        Base.metadata.create_all(
            conn,
            tables=[
                User.__table__,
                Group.__table__,
                Character.__table__,
                PlayerCard.__table__,
                SystemConfig.__table__,
            ],
        )
        # Pre-populate custom sell price on an existing item + pre-existing flag test.
        conn.execute(
            SystemConfig.__table__.insert(),
            [
                {
                    "key": "item_sell_prices",
                    "value": json.dumps(
                        {"apple_gold": 55, "apple_diamond": 100}, ensure_ascii=False
                    ),
                }
            ],
        )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()

        inspector = sa.inspect(conn)
        expected_tables = {
            "halloween_event_state",
            "halloween_attacks",
            "halloween_reward_claims",
            "halloween_seed_cooldowns",
            "halloween_loot_drops",
            "halloween_loot_drop_claims",
        }
        assert expected_tables <= set(inspector.get_table_names())

        # Indexes
        assert {
            idx["name"] for idx in inspector.get_indexes("halloween_event_state")
        } >= {"ix_hw_state_active"}
        assert {
            idx["name"] for idx in inspector.get_indexes("halloween_attacks")
        } >= {"ix_hw_attacks_event_user", "ix_hw_attacks_created_at"}
        assert {
            idx["name"] for idx in inspector.get_indexes("halloween_reward_claims")
        } >= {"ix_hw_reward_claims_user"}
        assert {
            idx["name"] for idx in inspector.get_indexes("halloween_loot_drops")
        } >= {"ix_hw_drops_status_expires", "ix_hw_drops_group_created"}
        assert {
            idx["name"]
            for idx in inspector.get_indexes("halloween_loot_drop_claims")
        } >= {"ix_hw_drop_claims_user"}

        # Unique constraints
        assert {
            uq["name"]
            for uq in inspector.get_unique_constraints("halloween_reward_claims")
        } >= {"uq_hw_reward_claims_event_user_pct"}
        assert {
            uq["name"]
            for uq in inspector.get_unique_constraints("halloween_loot_drop_claims")
        } >= {"uq_hw_drop_claims_drop_user", "uq_hw_drop_claims_drop_slot"}

        # Config seeds & non-destructive merge into item_sell_prices
        cfg_rows = dict(
            conn.execute(select(SystemConfig.key, SystemConfig.value)).all()
        )
        assert cfg_rows["halloween_enabled"] == "false"
        merged_prices = json.loads(cfg_rows["item_sell_prices"])
        assert merged_prices == {
            "apple_gold": 55,
            "apple_diamond": 100,
            "pumpkin_seed": 15,
        }

        # Clean downgrade removes only default pumpkin_seed entry & all 6 tables
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()

        remaining_tables = set(sa.inspect(conn).get_table_names())
        assert not (expected_tables & remaining_tables)
        restored_prices = json.loads(
            conn.execute(
                select(SystemConfig.value).where(
                    SystemConfig.key == "item_sell_prices"
                )
            ).scalar_one()
        )
        assert restored_prices == {"apple_gold": 55, "apple_diamond": 100}
    engine.dispose()


@pytest.mark.asyncio
async def test_halloween_models_enforce_unique_and_check_constraints(db_session):
    from datetime import datetime, timedelta, timezone

    user = await create_test_user(db_session, "شکارچی تست")
    now = datetime.now(timezone.utc)
    state = HalloweenEventState(
        boss_hp=100_000,
        max_hp=100_000,
        current_phase=1,
        is_active=True,
        started_at=now,
        ends_at=now + timedelta(days=7),
    )
    db_session.add(state)
    await db_session.flush()
    event_id = state.id
    uid = user.id

    attack = HalloweenAttack(
        event_id=event_id,
        user_id=uid,
        damage=42,
        is_crit=True,
        boss_hp_after=99_958,
    )
    claim = HalloweenRewardClaim(
        event_id=event_id,
        user_id=uid,
        milestone_pct=25,
        reward_summary_json='{"card": "amber"}',
    )
    cooldown = HalloweenSeedCooldown(
        event_id=event_id,
        user_id=uid,
        last_claimed_at=now,
        total_claims=1,
        total_seeds=2,
    )
    db_session.add_all([attack, claim, cooldown])
    await db_session.commit()

    # Duplicate milestone claim for the same (event_id, user_id, milestone_pct) must fail.
    db_session.add(
        HalloweenRewardClaim(
            event_id=event_id,
            user_id=uid,
            milestone_pct=25,
            reward_summary_json='{"card": "amber"}',
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    # Invalid milestone percentage outside (75, 50, 25, 0) must fail.
    db_session.add(
        HalloweenRewardClaim(
            event_id=event_id,
            user_id=uid,
            milestone_pct=33,
            reward_summary_json="{}",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# 2. Seed items & config: pumpkin_seed permanent inventory + 15-Apple sell
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pumpkin_seed_catalog_config_permanent_inventory_and_shop_sale(db_session):
    assert CONFIG_DEFAULTS["halloween_enabled"] is False
    assert await ConfigService.get(db_session, "halloween_enabled") is False

    assert "pumpkin_seed" in ITEM_CATALOG
    assert item_emoji("pumpkin_seed") == "🎃"
    assert item_label("pumpkin_seed") == "دونه کدو"
    assert "سیبومورتیس" in ITEM_CATALOG["pumpkin_seed"]["description"]

    user = await create_test_user(db_session, "نگهدارنده دونه کدو")
    await db_session.commit()
    uid = user.id

    # Event is OFF: seeds remain in inventory permanently and remain sellable for 15 Apples each.
    assert await ItemService.grant(db_session, uid, "pumpkin_seed", 4) == 4
    await db_session.commit()

    sellable = await ItemService.sellable_inventory(db_session, uid)
    assert ("pumpkin_seed", 4, 15) in sellable

    credited = await ItemService.sell(db_session, uid, "pumpkin_seed", 3)
    await db_session.commit()
    assert credited == 45
    assert await ItemService.get_total(db_session, uid, "pumpkin_seed") == 1
    assert await AppleWalletService.get_balance(db_session, uid) == 45

    # Raid ammo cannot be purchased directly with normal Apples.
    with pytest.raises(ValueError):
        await ItemService.buy(db_session, uid, "pumpkin_seed", 1)
    await db_session.rollback()


# ---------------------------------------------------------------------------
# 3. Exclusive Card «امبر» (amber): stats, lore, shop exclusion, full gameplay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amber_exclusive_card_seeded_excluded_from_shop_and_usable_everywhere(
    db_session,
):
    await seed_characters(db_session)
    await db_session.commit()

    amber = await db_session.scalar(
        select(Character).where(Character.code == "amber")
    )
    assert amber is not None
    assert amber.name_fa == "امبر"
    assert amber.name_en == "Amber"
    assert (amber.power, amber.speed, amber.intelligence) == (15, 20, 25)
    assert base_attributes(amber).total == 60
    assert attributes_at_level(base_attributes(amber), MAX_CARD_LEVEL).total == 330
    assert amber.lore_fa == AMBER_LORE
    assert amber.release_status == ReleaseStatus.RELEASED
    assert amber.is_active is True
    assert amber.shop_enabled is False

    # Bundled artwork resolves to assets/characters/amber.jpg
    file_id, fs_path = await resolve_character_photo(db_session, amber)
    assert file_id is None
    assert fs_path == REPO_ROOT / "assets" / "characters" / "amber.jpg"
    assert artwork_path(amber) == fs_path

    # Excluded from both weekly Shop rotation and free Daily Chest jackpot
    offered_codes = {
        ch.code for _entry, ch, _owned in await ShopService.list_entries(db_session)
    }
    assert "amber" not in offered_codes

    user = await create_test_user(db_session, "برنده امبر")
    eligible_chest_codes = {
        ch.code
        for ch in await DailyChestService._eligible_card_characters(
            db_session, user.id
        )
    }
    assert "amber" not in eligible_chest_codes

    # Visible in Wiki, acquirable, selectable as Main Card, and upgradable
    wiki_codes = {ch.code for ch in await WikiService.list_wiki(db_session)}
    assert "amber" in wiki_codes
    wiki_page = await WikiService.wiki_page(db_session, amber.id)
    assert wiki_page is not None and "امبر" in wiki_page and "سیبومورتیس" in wiki_page

    card = await PlayerCardService.acquire(db_session, user.id, amber.id)
    await PlayerCardService.set_favorite(
        db_session, user.id, card.id, expected_main_card_id=0
    )
    await AppleWalletService.earn(db_session, user.id, 500, reason="test_grant")
    await ItemService.grant(db_session, user.id, "apple_gold", 3)
    await db_session.commit()

    upgraded = await CardUpgradeService.upgrade(
        db_session, user.id, card.id, expected_level=1
    )
    await db_session.commit()
    assert (upgraded.from_level, upgraded.to_level) == (1, 2)


# ---------------------------------------------------------------------------
# 4. Halloween visual assets: existence, clean decode, exact target dimensions
# ---------------------------------------------------------------------------


def test_all_halloween_assets_exist_and_decode_cleanly():
    expected_square_1024 = (
        REPO_ROOT / "assets/halloween/sibomortis.jpg",
        REPO_ROOT / "assets/halloween/loot_pumpkin_chest.jpg",
        REPO_ROOT / "assets/halloween/loot_lantern_crate.jpg",
        REPO_ROOT / "assets/halloween/loot_autumn_hollow.jpg",
        REPO_ROOT / "assets/items/pumpkin_seed.jpg",
    )
    for path in expected_square_1024:
        assert path.is_file() and path.stat().st_size > 0, f"missing {path}"
        with Image.open(path) as im:
            im.load()
            assert im.mode == "RGB"
            assert im.size == (1024, 1024), f"unexpected size {im.size} for {path}"

    assert bundled_item_path("pumpkin_seed") == REPO_ROOT / "assets/items/pumpkin_seed.jpg"

    amber_art = REPO_ROOT / "assets/characters/amber.jpg"
    assert amber_art.is_file() and amber_art.stat().st_size > 0
    with Image.open(amber_art) as im:
        im.load()
        assert im.width > 0 and im.height > 0
