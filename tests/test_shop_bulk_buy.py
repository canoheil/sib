"""Tests for Phase S1 — Shop Bulk Buy & 'Buy Max' Feature."""

import pytest
from sqlalchemy import event, select

from sib.database.models import AppleTransaction, AppleWallet, UserItem
from sib.handlers import shop as shop_handlers
from sib.services.apple_wallet import AppleWalletService
from sib.services.items import ItemService
from sib.tests.test_parties import create_test_user
from sib.tests.test_shop_sections import _run_handler, _sell_callback


async def _fresh_balance(session, user_id: int) -> int:
    wallet = await session.scalar(
        select(AppleWallet)
        .where(AppleWallet.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return int(wallet.balance if wallet else 0)


async def _fresh_item_qty(session, user_id: int, item_key: str) -> int:
    row = await session.scalar(
        select(UserItem)
        .where(UserItem.user_id == user_id, UserItem.item_key == item_key)
        .execution_options(populate_existing=True)
    )
    return int(row.quantity if row else 0)


async def _purchase_tx_rows(session, user_id: int) -> list[AppleTransaction]:
    rows = await session.scalars(
        select(AppleTransaction)
        .where(
            AppleTransaction.user_id == user_id,
            AppleTransaction.reason == "item_purchase",
        )
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


@pytest.mark.asyncio
async def test_good_view_renders_bulk_buy_keyboard(db_session):
    user = await create_test_user(db_session, "خریدار چیدمان")
    await db_session.commit()

    _text, kb, art_photo = await shop_handlers._good_view(user.id, "magic_powder")
    assert art_photo is not None

    rows = [
        [(btn.text, btn.callback_data, btn.style) for btn in row]
        for row in kb.inline_keyboard
    ]
    assert rows == [
        [
            ("🛒 خرید ۱", "shop:buy:magic_powder:1", "success"),
            ("🛒 خرید ۵", "shop:buy:magic_powder:5", "success"),
        ],
        [
            ("🛒 خرید ۱۰", "shop:buy:magic_powder:10", "success"),
            ("🛒 خرید حداکثر", "shop:buy:magic_powder:max", "success"),
        ],
        [
            ("🔙 بازگشت", "shop_goods", "danger"),
        ],
    ]


@pytest.mark.asyncio
async def test_buy_1_item(db_session):
    user = await create_test_user(db_session, "خریدار ۱")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 100, reason="test_grant")
    await db_session.commit()

    cb = _sell_callback(uid, "shop:buy:magic_powder:1")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_good_buy, cb)

    assert await _fresh_balance(db_session, uid) == 100 - 40
    assert await _fresh_item_qty(db_session, uid, "magic_powder") == 1
    txs = await _purchase_tx_rows(db_session, uid)
    assert len(txs) == 1 and txs[0].amount == -40

    edit.assert_awaited_once()
    edited_text = edit.await_args.args[2]
    assert "✅ با موفقیت 1 عدد پودر جادویی خریدید." in edited_text


@pytest.mark.asyncio
async def test_buy_5_items(db_session):
    user = await create_test_user(db_session, "خریدار ۵")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 300, reason="test_grant")
    await db_session.commit()

    cb = _sell_callback(uid, "shop:buy:magic_powder:5")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_good_buy, cb)

    assert await _fresh_balance(db_session, uid) == 300 - (40 * 5)
    assert await _fresh_item_qty(db_session, uid, "magic_powder") == 5
    txs = await _purchase_tx_rows(db_session, uid)
    assert len(txs) == 1 and txs[0].amount == -200

    edit.assert_awaited_once()
    edited_text = edit.await_args.args[2]
    assert "✅ با موفقیت 5 عدد پودر جادویی خریدید." in edited_text


@pytest.mark.asyncio
async def test_buy_10_items(db_session):
    user = await create_test_user(db_session, "خریدار ۱۰")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 500, reason="test_grant")
    await db_session.commit()

    cb = _sell_callback(uid, "shop:buy:sib_buff:10")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_good_buy, cb)

    assert await _fresh_balance(db_session, uid) == 0
    assert await _fresh_item_qty(db_session, uid, "sib_buff") == 10
    txs = await _purchase_tx_rows(db_session, uid)
    assert len(txs) == 1 and txs[0].amount == -500

    edit.assert_awaited_once()
    edited_text = edit.await_args.args[2]
    assert "✅ با موفقیت 10 عدد سیب باف خریدید." in edited_text


@pytest.mark.asyncio
async def test_buy_max_calculates_correctly(db_session):
    user = await create_test_user(db_session, "خریدار حداکثر")
    uid = user.id
    # magic_powder price is 40 -> 230 // 40 == 5 (cost 200, 30 remaining)
    await AppleWalletService.earn(db_session, uid, 230, reason="test_grant")
    await db_session.commit()

    cb = _sell_callback(uid, "shop:buy:magic_powder:max")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_good_buy, cb)

    assert await _fresh_balance(db_session, uid) == 30
    assert await _fresh_item_qty(db_session, uid, "magic_powder") == 5
    txs = await _purchase_tx_rows(db_session, uid)
    assert len(txs) == 1 and txs[0].amount == -200

    edit.assert_awaited_once()
    edited_text = edit.await_args.args[2]
    assert "✅ با موفقیت 5 عدد پودر جادویی خریدید." in edited_text


@pytest.mark.asyncio
async def test_buy_max_fails_if_balance_too_low(db_session):
    user = await create_test_user(db_session, "موجودی ناکافی مکس")
    uid = user.id
    # magic_powder costs 40; balance is 30 -> max_qty == 0
    await AppleWalletService.earn(db_session, uid, 30, reason="test_grant")
    await db_session.commit()

    cb = _sell_callback(uid, "shop:buy:magic_powder:max")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_good_buy, cb)

    cb.answer.assert_awaited_once_with("موجودی سیب شما کافی نیست!", show_alert=True)
    edit.assert_not_awaited()
    assert await _fresh_balance(db_session, uid) == 30
    assert await _fresh_item_qty(db_session, uid, "magic_powder") == 0
    assert await _purchase_tx_rows(db_session, uid) == []


@pytest.mark.asyncio
async def test_bulk_buy_fails_if_not_enough_apples_for_full_quantity(db_session):
    user = await create_test_user(db_session, "موجودی ناکافی تعداد")
    uid = user.id
    # sib_buff costs 50; balance is 120 -> max_possible == 2, cannot buy 5 (cost 250)
    await AppleWalletService.earn(db_session, uid, 120, reason="test_grant")
    await db_session.commit()

    cb = _sell_callback(uid, "shop:buy:sib_buff:5")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_good_buy, cb)

    cb.answer.assert_awaited_once_with(
        "سیب کافی نداری! شما فقط توانایی خرید 2 عدد را دارید.",
        show_alert=True,
    )
    edit.assert_not_awaited()
    assert await _fresh_balance(db_session, uid) == 120
    assert await _fresh_item_qty(db_session, uid, "sib_buff") == 0
    assert await _purchase_tx_rows(db_session, uid) == []


@pytest.mark.asyncio
async def test_bulk_buy_is_atomic(db_session):
    """If DB write for inventory fails after wallet debit, wallet is refunded."""
    user = await create_test_user(db_session, "خریدار اتمیک")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 500, reason="test_grant")
    await db_session.commit()

    sync_engine = db_session.get_bind()

    def fail_user_item_write(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lstrip().upper()
        if (sql.startswith("INSERT") or sql.startswith("UPDATE")) and "USER_ITEMS" in sql:
            raise RuntimeError("simulated DB write failure on user_items")

    event.listen(sync_engine, "before_cursor_execute", fail_user_item_write)
    try:
        cb = _sell_callback(uid, "shop:buy:magic_powder:5")
        edit, _banner = await _run_handler(shop_handlers.handle_shop_good_buy, cb)
    finally:
        event.remove(sync_engine, "before_cursor_execute", fail_user_item_write)

    edit.assert_not_awaited()
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    assert await _fresh_balance(db_session, uid) == 500
    assert await _fresh_item_qty(db_session, uid, "magic_powder") == 0
    assert await _purchase_tx_rows(db_session, uid) == []
