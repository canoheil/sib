"""Shop structure: root sections, Items, Sell Items + rotation validation."""

import pytest

from sib.services.apple_wallet import AppleWalletService
from sib.services.items import ItemService
from sib.services.shop import ShopService
from sib.services.characters import CharacterService
from sib.database.models import ReleaseStatus
from sib.tests.test_parties import create_test_user
from sib.tests.test_shop import _make_characters


def _callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Root: exactly three sections, never a direct purchase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shop_root_offers_three_sections(db_session):
    from sib.handlers.shop import _shop_view

    user = await create_test_user(db_session, "خریدار بخش‌ها")
    text, kb = await _shop_view(user.id)

    callbacks = _callbacks(kb)
    assert callbacks.count("shop_cards") == 1
    assert callbacks.count("shop_goods") == 1
    assert callbacks.count("shop_sell") == 1
    assert not any(c.startswith("shop_buy:") for c in callbacks)
    assert "فروشگاه" in text


# ---------------------------------------------------------------------------
# Section: Items (Magic Powder buyable with Apples)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goods_section_lists_config_priced_items(db_session):
    from sib.handlers.shop import _goods_view

    user = await create_test_user(db_session, "خریدار پودر")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 100, reason="test_grant")
    await db_session.commit()

    text, kb = await _goods_view(uid)
    callbacks = _callbacks(kb)

    assert "پودر جادویی" in text
    assert "40" in text  # config buy price
    assert "shop_good:magic_powder" in callbacks
    # Items without a buy price (sell-only specials) are NOT offered.
    assert "shop_good:apple_obsidian" not in callbacks


@pytest.mark.asyncio
async def test_good_detail_uses_bundled_art(db_session):
    from sib.handlers.shop import _good_view

    user = await create_test_user(db_session, "خریدار جزئیات")
    text, kb, art_photo = await _good_view(user.id, "magic_powder")

    assert "پودر جادویی" in text
    assert "shop_good_buy:magic_powder" in _callbacks(kb)
    assert art_photo is not None  # bundled assets/items/magic_powder.jpg


# ---------------------------------------------------------------------------
# Section: Sell Items (specials back into Apples)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_section_lists_only_sellable_inventory(db_session):
    from sib.handlers.shop import _sell_view

    user = await create_test_user(db_session, "فروشنده سیب")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_gold", 2)
    await ItemService.grant(db_session, uid, "magic_powder", 3)  # NOT sellable
    await db_session.commit()

    text, kb = await _sell_view(uid)
    callbacks = _callbacks(kb)

    assert "سیب طلایی" in text
    assert "shop_sell_one:apple_gold" in callbacks
    assert "shop_sell_all:apple_gold" in callbacks
    # Magic Powder has no sell price -> never listed.
    assert not any(c.endswith(":magic_powder") for c in callbacks)


@pytest.mark.asyncio
async def test_sell_all_creates_one_transaction_per_item(db_session):
    user = await create_test_user(db_session, "فروشنده عمده")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_gold", 2)
    await ItemService.grant(db_session, uid, "apple_diamond", 1)
    await db_session.commit()

    credited = 0
    credited += await ItemService.sell(db_session, uid, "apple_gold", 2)
    credited += await ItemService.sell(db_session, uid, "apple_diamond", 1)
    await db_session.commit()

    assert credited == 50 * 2 + 100  # config sell prices
    assert await AppleWalletService.get_balance(db_session, uid) == credited
    assert await ItemService.list_items(db_session, uid) == {}


# ---------------------------------------------------------------------------
# get_entry: detail screens validate the CURRENT rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entry_returns_rotation_entry_and_ownership(db_session):
    chars = await _make_characters(db_session, 2)
    user = await create_test_user(db_session, "مشتری چرخش")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 10_000, reason="test_grant")
    await db_session.commit()

    entries = await ShopService.list_entries(db_session, user_id=uid)
    entry = entries[0][0]
    character = entries[0][1]
    entry_id = entry.id

    got, got_char, owned = await ShopService.get_entry(db_session, entry_id, user_id=uid)
    assert got is not None and got.id == entry_id
    assert got_char.id == character.id
    assert owned is False

    await ShopService.purchase(db_session, uid, entry_id)
    await db_session.commit()

    _e, _c, owned_now = await ShopService.get_entry(db_session, entry_id, user_id=uid)
    assert owned_now is True


@pytest.mark.asyncio
async def test_get_entry_rejects_stale_rotation(db_session):
    """A card from LAST week's rotation must not be reachable by id."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from sib.database.models import ShopEntry, ShopRotation

    chars = await _make_characters(db_session, 2)
    await ShopService.current_rotation(db_session)
    await db_session.commit()

    entries = await ShopService.list_entries(db_session)
    stale_entry_id = entries[0][0].id

    # Expire the current rotation so a new one is created on demand.
    await db_session.execute(
        update(ShopRotation)
        .where(ShopRotation.is_active.is_(True))
        .values(is_active=False, ends_at=datetime.now(timezone.utc) - timedelta(days=1))
    )
    await db_session.commit()
    await ShopService.current_rotation(db_session)  # brand-new rotation
    await db_session.commit()

    got, character, owned = await ShopService.get_entry(db_session, stale_entry_id)
    assert got is None and character is None and owned is False


@pytest.mark.asyncio
async def test_get_entry_rejects_inactive_entry(db_session):
    from sqlalchemy import update

    from sib.database.models import ShopEntry

    await _make_characters(db_session, 2)
    await ShopService.current_rotation(db_session)
    await db_session.commit()

    entry_id = (await ShopService.list_entries(db_session))[0][0].id
    await db_session.execute(
        update(ShopEntry).where(ShopEntry.id == entry_id).values(is_active=False)
    )
    await db_session.commit()

    got, _character, _owned = await ShopService.get_entry(db_session, entry_id)
    assert got is None


# ---------------------------------------------------------------------------
# Sell-all handler: one sale, one toast, refreshed SELL view, no crash
# (regression: a copy-pasted purchase tail used undefined render_banner/text/kb)
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


@pytest.mark.asyncio
async def test_sell_all_handler_sells_everything_and_refreshes_the_sell_view(db_session):
    from unittest.mock import AsyncMock, patch

    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "فروشنده دکمه")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_gold", 3)
    await db_session.commit()

    cb = _sell_callback(uid, "shop_sell_all:apple_gold")
    with patch.object(shop_handlers, "safe_edit_callback_message", new=AsyncMock()) as edit, patch(
        "sib.services.banner.render_banner", new=AsyncMock()
    ) as banner:
        await shop_handlers.handle_shop_sell_all(cb, bot=AsyncMock())  # must not raise

    # Sale happened exactly once.
    assert await AppleWalletService.get_balance(db_session, uid) == 3 * 50
    assert await ItemService.list_items(db_session, uid) == {}

    # ONE acknowledgement: the sale toast (not the old card-purchase text).
    cb.answer.assert_awaited_once()
    toast = cb.answer.await_args.args[0]
    assert "فروش انجام شد" in toast and "150" in toast
    assert "کارت" not in toast

    # The sell screen is refreshed in place (same as sell-one); the shop root
    # banner is not re-rendered.
    edit.assert_awaited_once()
    text = edit.await_args.args[2]
    assert "فروش آیتم‌ها" in text and "`150`" in text
    banner.assert_not_awaited()


@pytest.mark.asyncio
async def test_sell_all_handler_with_nothing_to_sell_alerts_and_changes_nothing(db_session):
    from unittest.mock import AsyncMock, patch

    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "دست خالی")
    uid = user.id

    cb = _sell_callback(uid, "shop_sell_all:apple_gold")
    with patch.object(shop_handlers, "safe_edit_callback_message", new=AsyncMock()) as edit:
        await shop_handlers.handle_shop_sell_all(cb, bot=AsyncMock())

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    edit.assert_not_awaited()
    assert await AppleWalletService.get_balance(db_session, uid) == 0
