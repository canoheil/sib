"""Tests for the apple wallet + transaction ledger (Phase 8).

Covers: auditable ledger entries, negative-balance prevention, atomic
spend/earn, idempotency keys, and history lookups.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from sib.database.models import AppleTransaction
from sib.services.apple_wallet import (
    AppleWalletService,
    InsufficientFundsException,
)
from sib.tests.test_parties import create_test_user


@pytest.mark.asyncio
async def test_earn_and_spend_write_ledger_entries(db_session):
    user = await create_test_user(db_session, "کیفدار ۱")

    balance = await AppleWalletService.earn(db_session, user.id, 100, reason="test_grant")
    await db_session.commit()
    assert balance == 100

    balance = await AppleWalletService.spend(db_session, user.id, 30, reason="test_buy")
    await db_session.commit()
    assert balance == 70

    history = await AppleWalletService.get_history(db_session, user.id)
    assert len(history) == 2
    # Newest first.
    assert history[0].amount == -30
    assert history[0].balance_after == 70
    assert history[1].amount == 100
    assert history[1].balance_after == 100
    assert history[0].reason == "test_buy"


@pytest.mark.asyncio
async def test_negative_balance_is_impossible(db_session):
    user = await create_test_user(db_session, "کیفدار ۲")
    await AppleWalletService.earn(db_session, user.id, 50, reason="test_grant")
    await db_session.commit()

    with pytest.raises(InsufficientFundsException):
        await AppleWalletService.spend(db_session, user.id, 51, reason="too_much")

    assert await AppleWalletService.get_balance(db_session, user.id) == 50

    # Exact spend of the whole balance is allowed (balance goes to zero).
    balance = await AppleWalletService.spend(db_session, user.id, 50, reason="all_in")
    await db_session.commit()
    assert balance == 0


@pytest.mark.asyncio
async def test_idempotency_key_blocks_duplicates(db_session):
    user = await create_test_user(db_session, "کیفدار ۳")

    await AppleWalletService.earn(
        db_session, user.id, 25, reason="reward", idempotency_key="reward:1"
    )
    await AppleWalletService.earn(
        db_session, user.id, 25, reason="reward", idempotency_key="reward:1"
    )
    await db_session.commit()

    assert await AppleWalletService.get_balance(db_session, user.id) == 25

    # Spend keys work the same way.
    await AppleWalletService.spend(
        db_session, user.id, 10, reason="buy", idempotency_key="buy:1"
    )
    await AppleWalletService.spend(
        db_session, user.id, 10, reason="buy", idempotency_key="buy:1"
    )
    await db_session.commit()

    assert await AppleWalletService.get_balance(db_session, user.id) == 15

    rows = (
        await db_session.scalars(
            select(AppleTransaction).where(AppleTransaction.user_id == user.id)
        )
    ).all()
    assert len(rows) == 2  # one earn + one spend despite repeated calls


@pytest.mark.asyncio
async def test_unkeyed_operations_are_not_deduplicated(db_session):
    """Plain operations without a key always execute (by design)."""
    user = await create_test_user(db_session, "کیفدار ۴")

    await AppleWalletService.earn(db_session, user.id, 10, reason="a")
    await AppleWalletService.earn(db_session, user.id, 10, reason="a")
    await db_session.commit()

    assert await AppleWalletService.get_balance(db_session, user.id) == 20


@pytest.mark.asyncio
async def test_balance_after_is_consistent_in_ledger(db_session):
    user = await create_test_user(db_session, "کیفدار ۵")

    await AppleWalletService.earn(db_session, user.id, 10, reason="r1")
    await AppleWalletService.earn(db_session, user.id, 5, reason="r2")
    await AppleWalletService.spend(db_session, user.id, 3, reason="s1")
    await db_session.commit()

    rows = (
        await db_session.scalars(
            select(AppleTransaction)
            .where(AppleTransaction.user_id == user.id)
            .order_by(AppleTransaction.id.asc())
        )
    ).all()
    assert [r.balance_after for r in rows] == [10, 15, 12]
    assert rows[-1].balance_after == await AppleWalletService.get_balance(db_session, user.id)


@pytest.mark.asyncio
async def test_admin_adjustment_is_audited(db_session):
    user = await create_test_user(db_session, "کیفدار ۶")
    await AppleWalletService.earn(db_session, user.id, 10, reason="r")
    await db_session.commit()

    # Controlled administrative adjustment (negative), fully audited.
    balance = await AppleWalletService.spend(
        db_session, user.id, 4, reason="admin_correction", ref_type="admin", ref_id=1
    )
    await db_session.commit()

    assert balance == 6
    rows = (
        await db_session.scalars(
            select(AppleTransaction).where(AppleTransaction.user_id == user.id)
        )
    ).all()
    assert any(r.reason == "admin_correction" and r.ref_type == "admin" for r in rows)


@pytest.mark.asyncio
async def test_keyed_earn_integrity_error_keeps_callers_transaction(db_session, monkeypatch):
    """A keyed earn error never rolls back caller-owned work."""
    user = await create_test_user(db_session, "کیفدار savepoint")

    async def fail_wallet_create(_session, _user_id):
        raise IntegrityError("injected", {}, RuntimeError("injected constraint failure"))

    monkeypatch.setattr(
        AppleWalletService,
        "_get_or_create",
        staticmethod(fail_wallet_create),
    )
    with pytest.raises(IntegrityError, match="injected constraint failure"):
        await AppleWalletService.earn(
            db_session,
            user.id,
            2,
            reason="savepoint_test",
            idempotency_key="savepoint-test:1",
        )

    # A caller-owned write remains usable and can commit after the isolated
    # Apple error; no Apple wallet/ledger row was written by the failed call.
    user.first_name = "Caller transaction still active"
    await db_session.commit()
    await db_session.refresh(user)
    assert user.first_name == "Caller transaction still active"
    assert await AppleWalletService.get_balance(db_session, user.id) == 0
    assert await db_session.scalar(
        select(AppleTransaction.id).where(
            AppleTransaction.idempotency_key == "savepoint-test:1"
        )
    ) is None
