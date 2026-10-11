"""Atomicity of a shop card purchase after the card write (remediation).

The card is acquired before the wallet is debited (lock order: cards rank 4,
wallet rank 6). If the debit then refuses, a caller that catches the
ShopError and commits its outer transaction must still persist nothing: no
card and no charge.

Two branches are covered:
* SQLite (no savepoints): the explicit compensation deletes the card again.
  This runs for real on SQLite.
* Savepoint branch (MariaDB behaviour): forced on here with a monkeypatch so
  the wiring is exercised. SQLite only emulates the savepoint, so this is a
  logic check, not evidence of MariaDB row-lock or savepoint behaviour.
"""

import pytest
from sqlalchemy import func, select

from sib.database.models import PlayerCard
from sib.services import shop as shop_module
from sib.services.apple_wallet import (
    AppleWalletService,
    InsufficientFundsException,
    LedgerDebit,
)
from sib.services.shop import ShopError, ShopService
from sib.tests.test_parties import create_test_user
from sib.tests.test_shop import _make_characters


async def _setup(db_session):
    await _make_characters(db_session, 1)
    user = await create_test_user(db_session, "اتمیک خرید")
    await AppleWalletService.earn(db_session, user.id, 5000, reason="test_grant")
    await db_session.commit()
    entries = await ShopService.list_entries(db_session, user_id=user.id)
    await db_session.commit()
    entry = entries[0][0]
    character = entries[0][1]
    return user.id, entry.id, character.id


async def _cards(db_session, user_id: int, character_id: int) -> int:
    return await db_session.scalar(
        select(func.count())
        .select_from(PlayerCard)
        .where(PlayerCard.user_id == user_id, PlayerCard.character_id == character_id)
    )


@pytest.mark.asyncio
async def test_debit_refusal_after_card_write_persists_nothing_on_commit(
    db_session, monkeypatch
):
    user_id, entry_id, character_id = await _setup(db_session)
    balance = await AppleWalletService.get_balance(db_session, user_id)

    async def refuse(*args, **kwargs):
        raise InsufficientFundsException()

    monkeypatch.setattr(AppleWalletService, "debit_once", refuse)
    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user_id, entry_id, nonce="atomic00001")

    # The caller catches the refusal and commits anyway.
    await db_session.commit()

    assert await _cards(db_session, user_id, character_id) == 0
    assert await AppleWalletService.get_balance(db_session, user_id) == balance


@pytest.mark.asyncio
async def test_not_applied_debit_after_card_write_persists_nothing_on_commit(
    db_session, monkeypatch
):
    user_id, entry_id, character_id = await _setup(db_session)
    balance = await AppleWalletService.get_balance(db_session, user_id)

    async def not_applied(*args, **kwargs):
        return LedgerDebit(applied=False, balance_after=balance)

    monkeypatch.setattr(AppleWalletService, "debit_once", not_applied)
    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user_id, entry_id, nonce="atomic00002")
    await db_session.commit()

    assert await _cards(db_session, user_id, character_id) == 0
    assert await AppleWalletService.get_balance(db_session, user_id) == balance


@pytest.mark.asyncio
async def test_savepoint_branch_rolls_back_the_card_on_refusal(db_session, monkeypatch):
    """Logic check of the savepoint wiring (SQLite emulation, not MariaDB)."""
    user_id, entry_id, character_id = await _setup(db_session)
    balance = await AppleWalletService.get_balance(db_session, user_id)

    monkeypatch.setattr(shop_module, "savepoints_supported", lambda _session: True)

    async def refuse(*args, **kwargs):
        raise InsufficientFundsException()

    monkeypatch.setattr(AppleWalletService, "debit_once", refuse)
    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user_id, entry_id, nonce="atomic00003")
    await db_session.commit()

    assert await _cards(db_session, user_id, character_id) == 0
    assert await AppleWalletService.get_balance(db_session, user_id) == balance


@pytest.mark.asyncio
async def test_successful_purchase_still_writes_card_and_one_charge(db_session):
    """Regression guard: the atomic block must not change the happy path."""
    user_id, entry_id, character_id = await _setup(db_session)
    balance = await AppleWalletService.get_balance(db_session, user_id)

    outcome = await ShopService.purchase(db_session, user_id, entry_id, nonce="atomic00004")
    await db_session.commit()

    assert outcome.replayed is False
    assert await _cards(db_session, user_id, character_id) == 1
    assert await AppleWalletService.get_balance(db_session, user_id) == balance - outcome.price
