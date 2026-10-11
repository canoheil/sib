"""Callback-level remediation tests (audit items 6 and 7).

Handlers are driven through their real code paths; only the Telegram screen
renderers are patched out (see ``test_shop_sections._run_handler``).
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from sib.database.models import AppleTransaction, PlayerCard
from sib.services.apple_wallet import AppleWalletService
from sib.services.shop import STALE_PURCHASE_TEXT, ShopService
from sib.tests.test_parties import create_test_user
from sib.tests.test_shop import _make_characters
from sib.tests.test_shop_sections import _run_handler, _sell_callback


async def _buyer(db_session, apples: int = 1000):
    await _make_characters(db_session, 1)
    user = await create_test_user(db_session, "خریدار دکمه")
    await AppleWalletService.earn(db_session, user.id, apples, reason="seed")
    await db_session.commit()
    entries = await ShopService.list_entries(db_session, user_id=user.id)
    await db_session.commit()
    return user.id, entries[0][0].id


async def _purchase_rows(db_session, user_id: int) -> int:
    rows = await db_session.scalars(
        select(AppleTransaction.id).where(
            AppleTransaction.user_id == user_id,
            AppleTransaction.reason == "shop_purchase",
        )
    )
    return len(rows.all())


# ---------------------------------------------------------------------------
# Item 6: nonce-less or malformed purchase buttons are refused safely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "shop_buy:{entry}",          # old button: no nonce at all
        "shop_buy:{entry}:",         # empty nonce
        "shop_buy:{entry}:x",        # too short
        "shop_buy:{entry}:bad nonce!!",  # illegal characters
    ],
)
async def test_nonce_less_or_malformed_buy_button_is_refused_before_any_work(
    db_session, data, monkeypatch
):
    from sib.handlers import shop as shop_handlers

    user_id, entry_id = await _buyer(db_session)
    balance_before = await AppleWalletService.get_balance(db_session, user_id)

    purchase = AsyncMock()
    monkeypatch.setattr(ShopService, "purchase", purchase)

    cb = _sell_callback(user_id, data.format(entry=entry_id))
    await _run_handler(shop_handlers.handle_shop_buy, cb)

    purchase.assert_not_awaited()  # no database work, no purchase attempted
    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.await_args
    assert args[0] == STALE_PURCHASE_TEXT
    assert kwargs.get("show_alert") is True
    # The refusal tells the user to re-open the shop.
    assert "فروشگاه" in STALE_PURCHASE_TEXT
    assert await AppleWalletService.get_balance(db_session, user_id) == balance_before


@pytest.mark.asyncio
async def test_nonce_less_button_never_charges_or_grants_end_to_end(db_session):
    """Real path, no mocks: an old button must not buy, charge, or grant."""
    from sib.handlers import shop as shop_handlers

    user_id, entry_id = await _buyer(db_session)
    balance_before = await AppleWalletService.get_balance(db_session, user_id)

    cb = _sell_callback(user_id, f"shop_buy:{entry_id}")
    await _run_handler(shop_handlers.handle_shop_buy, cb)

    assert await AppleWalletService.get_balance(db_session, user_id) == balance_before
    assert await _purchase_rows(db_session, user_id) == 0
    cards = await db_session.scalar(
        select(PlayerCard.id).where(PlayerCard.user_id == user_id).limit(1)
    )
    assert cards is None
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_service_refuses_empty_nonce_even_when_called_directly(db_session):
    """Defence in depth: the service itself never accepts an empty nonce."""
    from sib.services.shop import ShopError

    user_id, entry_id = await _buyer(db_session)
    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user_id, entry_id, nonce="")
    await db_session.rollback()
    assert await AppleWalletService.get_balance(db_session, user_id) == 1000
