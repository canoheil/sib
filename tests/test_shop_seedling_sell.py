"""Selling EXTRA seedlings (نهال) in the shop's «فروش آیتم‌ها» section.

Rule under test (option (a) of the feature spec): only seedlings ABOVE the
garden's planting capacity are sellable, because a planted tree is never
removed, so ``garden.capacity_plots`` is the hard ceiling on the seedlings a
player can ever use. Everything above it is dead inventory.

Covered here:
- a sale decrements the seedling counter and credits normal Apples,
- quantity 0/negative is refused with the same Persian message as items,
- the "above capacity" rule is enforced (never sell below the reserve),
- the shop Sell list offers the seedling entry (name, count, callback) and
  the item page resolves the bundled 1:1 icon ``assets/items/seedling.jpg``,
- the config-driven price is authoritative (overriding it changes the
  credited amount; a missing/zero price hides the entry),
- migration 061 seeds exactly one more head and is idempotent / reversible.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from sib.database.models import Garden, SeedlingInventory
from sib.services.apple_wallet import AppleWalletService
from sib.services.assets import AssetService, bundled_item_path
from sib.services.config_store import ConfigService
from sib.services.garden import GardenService
from sib.services.items import ItemService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_user


async def _seedlings(session, user_id: int, total: int) -> None:
    """Give a player ``total`` seedlings (inventory row, no award ledger)."""
    row = await session.get(SeedlingInventory, user_id)
    if row is None:
        session.add(SeedlingInventory(user_id=user_id, total=total))
    else:
        row.total = total
    await session.flush()
    await session.commit()


async def _gardener(session, name: str, *, seedlings: int):
    """A player with a garden (default capacity 5) and a seedling stock."""
    user = await create_test_user(session, name)
    garden = await GardenService.get_or_create(session, user.id)
    await _seedlings(session, user.id, seedlings)
    return user, garden


def _callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Service: sell credits Apples and decrements the counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_seedlings_decrements_count_and_credits_apples(db_session):
    user, garden = await _gardener(db_session, "نهال‌فروش", seedlings=8)
    assert garden.capacity_plots == 5  # default garden_initial_plots
    price = await SeedlingService.sell_price(db_session)
    assert price == 20

    credited = await SeedlingService.sell(db_session, user.id, 3)
    await db_session.commit()

    assert credited == price * 3 == 60
    # 5 reserved for the garden stay, 3 extra sold.
    assert await SeedlingService.get_total(db_session, user.id) == 5
    assert await AppleWalletService.get_balance(db_session, user.id) == 60
    # Same currency/flow as every other sale: one wallet ledger entry.
    from sib.database.models import AppleTransaction

    reasons = (
        await db_session.execute(
            sa.select(AppleTransaction.reason).where(
                AppleTransaction.user_id == user.id
            )
        )
    ).scalars().all()
    assert reasons == ["seedling_sold"]


@pytest.mark.asyncio
async def test_sell_all_extras_leaves_exactly_the_capacity(db_session):
    user, garden = await _gardener(db_session, "فروشنده همه نهال", seedlings=12)

    extra = await SeedlingService.sellable_total(db_session, user.id)
    assert extra == 12 - garden.capacity_plots == 7

    credited = await SeedlingService.sell(db_session, user.id, extra)
    await db_session.commit()

    assert credited == 7 * 20
    assert await SeedlingService.get_total(db_session, user.id) == garden.capacity_plots
    assert await SeedlingService.sellable_total(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_sell_zero_or_negative_is_rejected_with_the_item_message(db_session):
    user, _garden = await _gardener(db_session, "تعداد نامعتبر", seedlings=9)
    uid = user.id

    for bad_quantity in (0, -3):
        with pytest.raises(ValueError) as excinfo:
            await SeedlingService.sell(db_session, uid, bad_quantity)
        # Same wording ItemService.sell / consume use for bad quantities.
        assert str(excinfo.value) == "تعداد باید حداقل ۱ باشد."

    await db_session.commit()
    assert await SeedlingService.get_total(db_session, uid) == 9
    assert await AppleWalletService.get_balance(db_session, uid) == 0


# ---------------------------------------------------------------------------
# Service: the "above the garden capacity" eligibility rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_sell_below_the_planting_capacity(db_session):
    """Option (a): the seedlings the garden can still plant are NOT sellable."""
    user, garden = await _gardener(db_session, "ظرفیت حفظ‌شده", seedlings=7)
    uid = user.id
    assert garden.capacity_plots == 5

    with pytest.raises(ValueError) as excinfo:
        await SeedlingService.sell(db_session, uid, 3)  # only 2 are extra
    message = str(excinfo.value)
    assert "قابل فروش: 2" in message
    assert "قیمت" not in message

    await db_session.commit()
    assert await SeedlingService.get_total(db_session, uid) == 7
    assert await AppleWalletService.get_balance(db_session, uid) == 0


@pytest.mark.asyncio
async def test_player_at_or_below_capacity_has_nothing_to_sell(db_session):
    user, garden = await _gardener(db_session, "بدون نهال اضافه", seedlings=5)
    uid = user.id
    assert garden.capacity_plots == 5
    assert await SeedlingService.sellable_total(db_session, uid) == 0

    with pytest.raises(ValueError):
        await SeedlingService.sell(db_session, uid, 1)

    # Zero seedlings is the same story, never a negative credit.
    empty = await create_test_user(db_session, "انبار نهال خالی")
    await GardenService.get_or_create(db_session, empty.id)
    assert await SeedlingService.sellable_total(db_session, empty.id) == 0
    await db_session.commit()


@pytest.mark.asyncio
async def test_capacity_comes_from_the_garden_not_a_fixed_threshold(db_session):
    """Unlocking a plot raises the reserve, so it is not option (c)."""
    user, garden = await _gardener(db_session, "باغ بزرگ‌تر", seedlings=8)
    uid = user.id
    assert await SeedlingService.sellable_total(db_session, uid) == 3

    garden.capacity_plots = 8  # what GardenService.unlock_plot does
    await db_session.commit()

    assert await SeedlingService.planting_capacity(db_session, uid) == 8
    assert await SeedlingService.sellable_total(db_session, uid) == 0
    with pytest.raises(ValueError):
        await SeedlingService.sell(db_session, uid, 1)


@pytest.mark.asyncio
async def test_planting_still_works_after_selling_the_extras(db_session):
    """Only the SELL path is new: planting consumes a reserved seedling."""
    user, garden = await _gardener(db_session, "کاشت بعد از فروش", seedlings=6)
    uid = user.id

    await SeedlingService.sell(db_session, uid, 1)
    await db_session.commit()
    assert await SeedlingService.get_total(db_session, uid) == 5

    plot = await GardenService.plant(db_session, uid, 0)
    await db_session.commit()
    assert plot.planted_at is not None
    assert await SeedlingService.get_total(db_session, uid) == 4


# ---------------------------------------------------------------------------
# Service: config-driven price
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overriding_the_config_price_changes_the_credited_amount(db_session):
    user, _garden = await _gardener(db_session, "قیمت تنظیم‌شده", seedlings=9)
    uid = user.id
    await ConfigService.set(db_session, "seedling_sell_price", 7)
    await db_session.commit()

    assert await SeedlingService.sell_price(db_session) == 7
    credited = await SeedlingService.sell(db_session, uid, 2)
    await db_session.commit()

    assert credited == 14
    assert await AppleWalletService.get_balance(db_session, uid) == 14


@pytest.mark.asyncio
async def test_disabled_price_refuses_the_sale_and_hides_the_entry(db_session):
    user, _garden = await _gardener(db_session, "غیرفعال", seedlings=9)
    uid = user.id
    await ConfigService.set(db_session, "seedling_sell_price", 0)
    await db_session.commit()

    assert await SeedlingService.sell_price(db_session) is None
    with pytest.raises(ValueError) as excinfo:
        await SeedlingService.sell(db_session, uid, 1)
    assert str(excinfo.value) == "این آیتم قابل فروش نیست."
    await db_session.commit()
    assert await SeedlingService.get_total(db_session, uid) == 9
    assert await AppleWalletService.get_balance(db_session, uid) == 0

    from sib.handlers.shop import _sell_view

    _text, kb = await _sell_view(uid)
    assert "shop_sell_seedling" not in _callbacks(kb)


# ---------------------------------------------------------------------------
# Shop UI: listing entry + icon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_list_includes_the_seedling_entry(db_session):
    """Name, sellable count, price and the seedling callback are all shown."""
    from sib.handlers.shop import _sell_view

    user, _garden = await _gardener(db_session, "فهرست فروش", seedlings=8)
    uid = user.id
    # One ordinary item too, so the list keeps both entries.
    await ItemService.grant(db_session, uid, "apple_gold", 1)
    await db_session.commit()

    text, kb = await _sell_view(uid)
    callbacks = _callbacks(kb)
    labels = _labels(kb)

    assert "shop_sell_item:apple_gold" in callbacks
    assert "shop_sell_seedling" in callbacks
    assert any("نهال" in label and "3" in label for label in labels)
    assert "نهال" in text and "`3`" in text and "ظرفیت باغ `5`" in text
    # Navigation stays intact.
    assert "shop_sell" in callbacks and "shop_refresh" in callbacks


@pytest.mark.asyncio
async def test_sell_list_hides_seedlings_at_or_below_capacity(db_session):
    from sib.handlers.shop import _sell_view

    user, _garden = await _gardener(db_session, "بدون فروش نهال", seedlings=4)
    _text, kb = await _sell_view(user.id)

    assert "shop_sell_seedling" not in _callbacks(kb)
    assert not any(c.startswith("shop_sell_seedling") for c in _callbacks(kb))


@pytest.mark.asyncio
async def test_seedling_sell_page_shows_price_actions_and_the_icon(db_session):
    from sib.handlers.shop import _seedling_sell_view

    user, _garden = await _gardener(db_session, "صفحه نهال", seedlings=8)
    text, kb, photo, ok = await _seedling_sell_view(user.id)

    assert ok is True
    assert "نهال" in text
    assert "`8`" in text and "`5`" in text and "`3`" in text  # total/capacity/extra
    assert "`20`" in text and "`60`" in text  # unit price / total value
    assert _callbacks(kb) == [
        "shop_sell_seedling_one",
        "shop_sell_seedling_all",
        "shop_sell",
    ]
    # The bundled 1:1 icon is used exactly like every other item's art.
    assert photo is not None
    path = bundled_item_path("seedling")
    assert path is not None and path.name == "seedling.jpg"


@pytest.mark.asyncio
async def test_seedling_icon_matches_the_existing_item_icon_format():
    """Same directory, filename convention and 1:1 pixel size as other items."""
    from PIL import Image

    path = Path(bundled_item_path("seedling"))
    assert path.parent.name == "items"
    assert path.suffix == ".jpg"

    with Image.open(path) as icon:
        assert icon.size == (1024, 1024)  # every assets/items icon is 1024x1024
        assert icon.mode == "RGB"  # opaque watercolor card, no alpha channel
        assert icon.format == "JPEG"

    with Image.open(path.parent / "magic_powder.jpg") as reference:
        assert icon.size == reference.size


@pytest.mark.asyncio
async def test_seedling_page_falls_back_to_the_list_without_extras(db_session):
    from sib.handlers.shop import _seedling_sell_view

    user, _garden = await _gardener(db_session, "لیست به‌جای صفحه", seedlings=5)
    text, kb, photo, ok = await _seedling_sell_view(user.id)

    assert ok is False and photo is None
    assert "فروش آیتم‌ها" in text
    assert not any(c.startswith("shop_sell_seedling") for c in _callbacks(kb))


# ---------------------------------------------------------------------------
# Handlers: Sell 1 / Sell All
# ---------------------------------------------------------------------------


def _sell_callback(user_id: int, data: str):
    from unittest.mock import AsyncMock, MagicMock

    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock()
    cb.from_user.id = user_id
    cb.message = MagicMock()
    cb.answer = AsyncMock()
    return cb


async def _run_handler(handler, cb):
    from unittest.mock import AsyncMock, patch

    from sib.handlers import shop as shop_handlers

    with patch.object(
        shop_handlers, "safe_edit_callback_message", new=AsyncMock()
    ) as edit:
        await handler(cb, bot=AsyncMock())
    return edit


@pytest.mark.asyncio
async def test_sell_one_handler_sells_one_and_refreshes_the_page(db_session):
    from sib.handlers import shop as shop_handlers

    user, _garden = await _gardener(db_session, "کلیک فروش یک", seedlings=8)
    uid = user.id

    cb = _sell_callback(uid, "shop_sell_seedling_one")
    edit = await _run_handler(shop_handlers.handle_shop_sell_seedling_one, cb)

    assert await SeedlingService.get_total(db_session, uid) == 7
    assert await AppleWalletService.get_balance(db_session, uid) == 20
    toast = cb.answer.await_args.args[0]
    assert "فروش انجام شد" in toast and "20" in toast
    text = edit.await_args.args[2]
    assert "نهال" in text and "`2`" in text  # two extras left
    assert edit.await_args.kwargs["photo"] is not None


@pytest.mark.asyncio
async def test_sell_all_confirmation_sells_every_extra_and_returns_to_the_list(
    db_session,
):
    from sib.handlers import shop as shop_handlers

    user, _garden = await _gardener(db_session, "کلیک فروش همه", seedlings=9)
    uid = user.id

    cb = _sell_callback(uid, "shop_sell_seedling_all_ok")
    edit = await _run_handler(shop_handlers.handle_shop_sell_seedling_all_confirm, cb)

    assert await SeedlingService.get_total(db_session, uid) == 5  # capacity kept
    assert await AppleWalletService.get_balance(db_session, uid) == 4 * 20

    toast = cb.answer.await_args.args[0]
    assert "فروش انجام شد" in toast and "80" in toast
    text = edit.await_args.args[2]
    kb = edit.await_args.kwargs["reply_markup"]
    assert "فروش آیتم‌ها" in text
    assert not any(c.startswith("shop_sell_seedling") for c in _callbacks(kb))


@pytest.mark.asyncio
async def test_sell_all_confirmation_screen_sells_nothing_yet(db_session):
    from sib.handlers import shop as shop_handlers

    user, _garden = await _gardener(db_session, "تأیید فروش نهال", seedlings=7)
    uid = user.id

    cb = _sell_callback(uid, "shop_sell_seedling_all")
    edit = await _run_handler(shop_handlers.handle_shop_sell_seedling_all, cb)

    assert await SeedlingService.get_total(db_session, uid) == 7
    assert await AppleWalletService.get_balance(db_session, uid) == 0
    text = edit.await_args.args[2]
    callbacks = _callbacks(edit.await_args.kwargs["reply_markup"])
    assert "تأیید فروش نهال‌ها" in text and "`2`" in text
    assert callbacks == ["shop_sell_seedling_all_ok", "shop_sell_seedling"]


@pytest.mark.asyncio
async def test_repeated_sell_all_cannot_pay_twice(db_session):
    from sib.handlers import shop as shop_handlers

    user, _garden = await _gardener(db_session, "کلیک پشت کلیک نهال", seedlings=7)
    uid = user.id

    for _ in range(3):
        cb = _sell_callback(uid, "shop_sell_seedling_all_ok")
        await _run_handler(shop_handlers.handle_shop_sell_seedling_all_confirm, cb)

    assert await SeedlingService.get_total(db_session, uid) == 5
    assert await AppleWalletService.get_balance(db_session, uid) == 2 * 20


@pytest.mark.asyncio
async def test_sell_one_handler_without_extras_alerts_and_changes_nothing(db_session):
    from sib.handlers import shop as shop_handlers

    user, _garden = await _gardener(db_session, "کلیک کهنه نهال", seedlings=5)
    uid = user.id

    cb = _sell_callback(uid, "shop_sell_seedling_one")
    edit = await _run_handler(shop_handlers.handle_shop_sell_seedling_one, cb)

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert await SeedlingService.get_total(db_session, uid) == 5
    assert await AppleWalletService.get_balance(db_session, uid) == 0
    assert "فروش آیتم‌ها" in edit.await_args.args[2]


# ---------------------------------------------------------------------------
# Migration 061: one idempotent config seed, single head
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database/migrations/versions/061_seedling_sell_price.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_061", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, direction: str) -> None:
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            getattr(migration, direction)()


def _engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm061.db'}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE system_config ("
                "`key` VARCHAR(64) PRIMARY KEY, `value` TEXT NOT NULL)"
            )
        )
    return engine


def test_migration_061_revises_060_and_keeps_one_head():
    migration = _load_migration()
    assert migration.revision == "061_seedling_sell_price"
    assert migration.down_revision == "060_invasion_fixed_schedule"

    # Every other revision still has at most one child: 061 is revised by
    # 062_transfers -> 063_reminders -> 064_group_league -> 065_league_points
    # -> 066_daily_channel_digest -> 067_invasion_daily_20_schedule
    # -> 068_golden_hour_apple_rewards -> 069_golden_hour_notices
    # -> 070_disease_system -> 071_disease_items -> 072_disease_ui, the single head.
    versions = REPO_ROOT / "database/migrations/versions"
    revisions = {}
    for path in versions.glob("*.py"):
        spec = importlib.util.spec_from_file_location(f"rev_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        revisions[module.revision] = module.down_revision
    parents = [parent for parent in revisions.values() if parent]
    assert len(parents) == len(set(parents)), "forked migration history"
    heads = set(revisions) - set(parents)
    assert heads == {"072_disease_ui"}


def test_migration_061_seeds_the_default_price_and_is_idempotent(tmp_path):
    engine = _engine(tmp_path)

    _run(engine, "upgrade")
    _run(engine, "upgrade")  # idempotent: ON CONFLICT DO NOTHING

    with engine.begin() as connection:
        rows = connection.execute(
            sa.text("SELECT `key`, `value` FROM system_config")
        ).all()
    assert rows == [("seedling_sell_price", "20")]

    # An operator-customized price survives a re-run.
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE system_config SET `value` = '35' WHERE `key` = :key"),
            {"key": "seedling_sell_price"},
        )
    _run(engine, "upgrade")
    with engine.begin() as connection:
        value = connection.execute(
            sa.text("SELECT `value` FROM system_config WHERE `key` = :key"),
            {"key": "seedling_sell_price"},
        ).scalar_one()
    assert value == "35"


def test_migration_061_downgrade_only_removes_the_untouched_default(tmp_path):
    engine = _engine(tmp_path)

    _run(engine, "upgrade")
    _run(engine, "downgrade")
    with engine.begin() as connection:
        assert connection.execute(
            sa.text("SELECT COUNT(*) FROM system_config")
        ).scalar_one() == 0

    # A customized value is not deleted by the downgrade.
    _run(engine, "upgrade")
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE system_config SET `value` = '35' WHERE `key` = :key"),
            {"key": "seedling_sell_price"},
        )
    _run(engine, "downgrade")
    with engine.begin() as connection:
        value = connection.execute(
            sa.text("SELECT `value` FROM system_config WHERE `key` = :key"),
            {"key": "seedling_sell_price"},
        ).scalar_one()
    assert value == "35"


def test_config_default_matches_the_migration_seed():
    from sib.services.config_store import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["seedling_sell_price"] == int(
        _load_migration()._VALUE
    )
