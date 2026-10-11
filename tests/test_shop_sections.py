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
# Root: exactly four sections, never a direct purchase
# ---------------------------------------------------------------------------


SHOP_SECTION_CALLBACKS = {"shop_cards", "shop_goods", "shop_sell", "shop_chests"}


@pytest.mark.asyncio
async def test_shop_root_offers_four_sections(db_session):
    from sib.handlers.shop import _shop_view

    user = await create_test_user(db_session, "خریدار بخش‌ها")
    text, kb = await _shop_view(user.id)

    callbacks = _callbacks(kb)
    sections = [c for c in callbacks if c in SHOP_SECTION_CALLBACKS]
    assert sorted(sections) == sorted(SHOP_SECTION_CALLBACKS)  # exactly four
    assert len(sections) == 4
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
async def test_sell_section_lists_only_owned_sellable_items(db_session):
    """The list shows ONE button per owned special Apple (name + quantity)."""
    from sib.handlers.shop import _sell_view

    user = await create_test_user(db_session, "فروشنده سیب")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_gold", 2)
    await ItemService.grant(db_session, uid, "magic_powder", 3)  # NOT sellable
    await db_session.commit()

    text, kb = await _sell_view(uid)
    callbacks = _callbacks(kb)
    labels = _labels(kb)

    assert "shop_sell_item:apple_gold" in callbacks
    assert any("سیب طلایی" in label and "2" in label for label in labels)
    # Not owned -> hidden. Magic Powder has no sell price -> never listed.
    assert "shop_sell_item:apple_diamond" not in callbacks
    assert "shop_sell_item:apple_obsidian" not in callbacks
    assert not any(c.endswith(":magic_powder") for c in callbacks)
    # The currency itself is never sellable through this flow.
    assert not any("apple_normal" in c for c in callbacks)
    assert "سیب معمولی" not in text
    # Selling happens on the item page, never straight from the list.
    assert not any(c.startswith("shop_sell_one:") for c in callbacks)
    assert not any(c.startswith("shop_sell_all:") for c in callbacks)
    # Back to the shop.
    assert "shop_refresh" in callbacks


@pytest.mark.asyncio
async def test_sell_section_empty_state(db_session):
    from sib.handlers.shop import _sell_view

    user = await create_test_user(db_session, "انبار خالی")
    text, kb = await _sell_view(user.id)

    assert "آیتم قابل‌فروشی نداری" in text
    assert not any(c.startswith("shop_sell_item:") for c in _callbacks(kb))
    assert "shop_refresh" in _callbacks(kb)


@pytest.mark.asyncio
async def test_sell_section_hides_zero_quantity_items(db_session):
    from sib.handlers.shop import _sell_view

    user = await create_test_user(db_session, "صفر شده")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_diamond", 1)
    await db_session.commit()
    await ItemService.sell(db_session, uid, "apple_diamond", 1)
    await db_session.commit()

    _text, kb = await _sell_view(uid)
    assert not any(c.startswith("shop_sell_item:") for c in _callbacks(kb))


@pytest.mark.asyncio
async def test_sell_item_page_shows_art_price_and_actions(db_session):
    """Item page: the item's EXISTING art + price + Sell 1 / Sell All / Back."""
    from sib.handlers.shop import _sell_item_view

    user = await create_test_user(db_session, "صفحه آیتم")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_diamond", 7)
    await db_session.commit()

    text, kb, photo, ok = await _sell_item_view(uid, "apple_diamond")
    callbacks = _callbacks(kb)

    assert ok is True
    assert "سیب الماسی" in text
    assert "`7`" in text and "`100`" in text and "`700`" in text
    assert photo is not None  # bundled assets/items/diamond_apple.jpg
    assert callbacks == [
        "shop_sell_one:apple_diamond",
        "shop_sell_all:apple_diamond",
        "shop_sell",
    ]


@pytest.mark.asyncio
async def test_sell_item_page_uses_the_matching_existing_image(db_session):
    """Each apple shows ITS own registered artwork (one mapping, no duplicates)."""
    from sib.services.assets import AssetService
    from sib.handlers.shop import _sell_item_view

    user = await create_test_user(db_session, "تصویر درست")
    uid = user.id
    for key in ("apple_gold", "apple_diamond", "apple_obsidian"):
        await ItemService.grant(db_session, uid, key, 1)
    await AssetService.register(db_session, "golden_apple", "file-gold")
    await AssetService.register(db_session, "diamond_apple", "file-diamond")
    await AssetService.register(db_session, "obsidian_apple", "file-obsidian")
    await db_session.commit()

    for key, asset, expected_file_id in (
        ("apple_gold", "golden_apple", "file-gold"),
        ("apple_diamond", "diamond_apple", "file-diamond"),
        ("apple_obsidian", "obsidian_apple", "file-obsidian"),
    ):
        # The ONE existing item -> image mapping is reused (no second system).
        assert await ItemService.asset_key(db_session, key) == asset
        _text, _kb, photo, ok = await _sell_item_view(uid, key)
        assert ok is True and photo == expected_file_id


@pytest.mark.asyncio
async def test_sell_item_page_of_an_unowned_or_invalid_item_falls_back_to_the_list(db_session):
    from sib.handlers.shop import _sell_item_view

    user = await create_test_user(db_session, "آیتم نامعتبر")

    for key in ("apple_gold", "apple_normal", "no_such_item"):
        text, kb, photo, ok = await _sell_item_view(user.id, key)
        assert ok is False and photo is None
        assert "فروش آیتم‌ها" in text
        assert not any(c.startswith("shop_sell_one:") for c in _callbacks(kb))


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

    await ShopService.purchase(db_session, uid, entry_id, nonce="ownedcheck01")
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
# Selling handlers: Sell 1, Sell All confirmation, Confirm, Cancel
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
    """Await a shop handler with the screen renderer patched out.

    Returns the (edit, banner) mocks so the rendered screen can be asserted.
    """
    from unittest.mock import AsyncMock, patch

    from sib.handlers import shop as shop_handlers

    with patch.object(shop_handlers, "safe_edit_callback_message", new=AsyncMock()) as edit, patch(
        "sib.services.banner.render_banner", new=AsyncMock()
    ) as banner:
        await handler(cb, bot=AsyncMock())
    return edit, banner


@pytest.mark.asyncio
async def test_sell_one_sells_exactly_one_and_refreshes_the_item_page(db_session):
    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "فروشنده تکی")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_gold", 3)
    await db_session.commit()

    cb = _sell_callback(uid, "shop_sell_one:apple_gold")
    edit, banner = await _run_handler(shop_handlers.handle_shop_sell_one, cb)

    assert await ItemService.get_total(db_session, uid, "apple_gold") == 2
    assert await AppleWalletService.get_balance(db_session, uid) == 50

    cb.answer.assert_awaited_once()
    assert "فروش انجام شد" in cb.answer.await_args.args[0]

    # Refreshed ITEM page (fresh quantity), with its art.
    edit.assert_awaited_once()
    text = edit.await_args.args[2]
    assert "سیب طلایی" in text and "`2`" in text
    assert edit.await_args.kwargs["photo"] is not None
    banner.assert_not_awaited()


@pytest.mark.asyncio
async def test_sell_one_of_the_last_unit_returns_to_the_sell_list(db_session):
    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "آخرین دانه")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_gold", 1)
    await db_session.commit()

    cb = _sell_callback(uid, "shop_sell_one:apple_gold")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_sell_one, cb)

    assert await ItemService.list_items(db_session, uid) == {}
    text = edit.await_args.args[2]
    kb = edit.await_args.kwargs["reply_markup"]
    assert "آیتم قابل‌فروشی نداری" in text  # empty state
    assert not any(c.startswith("shop_sell_item:") for c in _callbacks(kb))
    assert edit.await_args.kwargs["photo"] is None


@pytest.mark.asyncio
async def test_sell_one_on_a_stale_callback_sells_nothing(db_session):
    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "کلیک کهنه")
    uid = user.id

    cb = _sell_callback(uid, "shop_sell_one:apple_gold")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_sell_one, cb)

    assert await AppleWalletService.get_balance(db_session, uid) == 0
    assert await ItemService.get_total(db_session, uid, "apple_gold") == 0
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert "فروش آیتم‌ها" in edit.await_args.args[2]  # back on the list


@pytest.mark.asyncio
async def test_sell_all_asks_for_confirmation_and_sells_nothing_yet(db_session):
    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "تأیید لازم")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_diamond", 7)
    await db_session.commit()

    cb = _sell_callback(uid, "shop_sell_all:apple_diamond")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_sell_all, cb)

    # NOTHING sold on the confirmation screen.
    assert await ItemService.get_total(db_session, uid, "apple_diamond") == 7
    assert await AppleWalletService.get_balance(db_session, uid) == 0

    text = edit.await_args.args[2]
    callbacks = _callbacks(edit.await_args.kwargs["reply_markup"])
    assert "`7`" in text and "سیب الماسی" in text
    assert callbacks == ["shop_sell_all_ok:apple_diamond", "shop_sell_item:apple_diamond"]


@pytest.mark.asyncio
async def test_cancel_returns_to_the_item_page_without_selling(db_session):
    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "منصرف")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_diamond", 7)
    await db_session.commit()

    cb = _sell_callback(uid, "shop_sell_item:apple_diamond")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_sell_item, cb)

    assert await ItemService.get_total(db_session, uid, "apple_diamond") == 7
    assert await AppleWalletService.get_balance(db_session, uid) == 0
    text = edit.await_args.args[2]
    assert "سیب الماسی" in text and "`7`" in text
    assert "shop_sell_one:apple_diamond" in _callbacks(edit.await_args.kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_confirmed_sell_all_uses_the_current_inventory_and_returns_to_the_list(db_session):
    """The confirmation's quantity is informational: the CURRENT stock is sold."""
    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "فروشنده عمده دکمه")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_gold", 3)
    await db_session.commit()

    # Inventory grows AFTER the confirmation screen was rendered.
    await ItemService.grant(db_session, uid, "apple_gold", 2)
    await db_session.commit()

    cb = _sell_callback(uid, "shop_sell_all_ok:apple_gold")
    edit, banner = await _run_handler(shop_handlers.handle_shop_sell_all_confirm, cb)

    assert await ItemService.list_items(db_session, uid) == {}
    assert await AppleWalletService.get_balance(db_session, uid) == 5 * 50  # not 3 * 50

    cb.answer.assert_awaited_once()
    toast = cb.answer.await_args.args[0]
    assert "فروش انجام شد" in toast and "250" in toast
    assert "کارت" not in toast

    edit.assert_awaited_once()
    text = edit.await_args.args[2]
    assert "فروش آیتم‌ها" in text and "`250`" in text
    assert not any(
        c.startswith("shop_sell_item:")
        for c in _callbacks(edit.await_args.kwargs["reply_markup"])
    )
    banner.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_sell_all_confirmations_cannot_pay_twice(db_session):
    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "کلیک پشت کلیک")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_obsidian", 2)
    await db_session.commit()

    for _ in range(3):
        cb = _sell_callback(uid, "shop_sell_all_ok:apple_obsidian")
        await _run_handler(shop_handlers.handle_shop_sell_all_confirm, cb)

    assert await AppleWalletService.get_balance(db_session, uid) == 2 * 200
    assert await ItemService.get_total(db_session, uid, "apple_obsidian") == 0


@pytest.mark.asyncio
async def test_sell_all_handler_with_nothing_to_sell_alerts_and_changes_nothing(db_session):
    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "دست خالی")
    uid = user.id

    cb = _sell_callback(uid, "shop_sell_all_ok:apple_gold")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_sell_all_confirm, cb)

    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert await AppleWalletService.get_balance(db_session, uid) == 0
    assert "فروش آیتم‌ها" in edit.await_args.args[2]


@pytest.mark.asyncio
async def test_sell_screens_use_native_button_styles(db_session):
    """Semantic native styles: primary = navigation, success = sell/confirm,
    danger = back/cancel (never simulated)."""
    from sib.handlers.shop import _sell_confirm_view, _sell_item_view, _sell_view

    user = await create_test_user(db_session, "رنگ دکمه")
    uid = user.id
    await ItemService.grant(db_session, uid, "apple_gold", 4)
    await db_session.commit()

    _t, list_kb = await _sell_view(uid)
    styles = {b.callback_data: b.style for row in list_kb.inline_keyboard for b in row}
    assert styles["shop_sell_item:apple_gold"] == "primary"
    assert styles["shop_refresh"] == "danger"

    _t, item_kb, _photo, _ok = await _sell_item_view(uid, "apple_gold")
    styles = {b.callback_data: b.style for row in item_kb.inline_keyboard for b in row}
    assert styles["shop_sell_one:apple_gold"] == "success"
    assert styles["shop_sell_all:apple_gold"] == "success"
    assert styles["shop_sell"] == "danger"

    _t, confirm_kb, _photo, _ok = await _sell_confirm_view(uid, "apple_gold")
    styles = {b.callback_data: b.style for row in confirm_kb.inline_keyboard for b in row}
    assert styles["shop_sell_all_ok:apple_gold"] == "success"
    assert styles["shop_sell_item:apple_gold"] == "danger"
