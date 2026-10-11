"""Keyed ledger semantics for ``AppleWalletService.earn`` (remediation of the
post-implementation audit).

``earn`` must behave like ``debit_once``: an identical keyed operation replays
without writing, while a reused key with any different user, signed amount,
reason, or reference raises :class:`LedgerKeyConflictError`. A key owned by
another user is a conflict, never a silent no-op.

These run on SQLite. SQLite has no row locks, so they check the logic only;
the savepoint race path is exercised by the MariaDB-only tests, which are not
run here.
"""

import pytest
from sqlalchemy import func, select

from sib.database.models import AppleTransaction, AppleWallet
from sib.services.apple_wallet import (
    AppleWalletService,
    LedgerKeyConflictError,
)
from sib.tests.test_parties import create_test_user


async def _ledger_rows(session, key: str):
    return (
        await session.scalars(
            select(AppleTransaction).where(AppleTransaction.idempotency_key == key)
        )
    ).all()


@pytest.mark.asyncio
async def test_identical_keyed_earn_replays_once(db_session):
    user = await create_test_user(db_session, "ایرن ۱")
    uid = user.id

    await AppleWalletService.earn(
        db_session, uid, 25, reason="reward", ref_type="match", ref_id=7,
        idempotency_key="earn-same:1",
    )
    balance = await AppleWalletService.earn(
        db_session, uid, 25, reason="reward", ref_type="match", ref_id=7,
        idempotency_key="earn-same:1",
    )
    await db_session.commit()

    assert balance == 25
    assert await AppleWalletService.get_balance(db_session, uid) == 25
    assert len(await _ledger_rows(db_session, "earn-same:1")) == 1


@pytest.mark.asyncio
async def test_conflicting_amount_is_refused_and_writes_nothing(db_session):
    user = await create_test_user(db_session, "ایرن ۲")
    uid = user.id
    await AppleWalletService.earn(
        db_session, uid, 25, reason="reward", idempotency_key="earn-amt:1"
    )
    await db_session.commit()

    with pytest.raises(LedgerKeyConflictError):
        await AppleWalletService.earn(
            db_session, uid, 999, reason="reward", idempotency_key="earn-amt:1"
        )
    await db_session.rollback()

    assert await AppleWalletService.get_balance(db_session, uid) == 25
    assert len(await _ledger_rows(db_session, "earn-amt:1")) == 1


@pytest.mark.asyncio
async def test_conflicting_reason_is_refused(db_session):
    user = await create_test_user(db_session, "ایرن ۳")
    uid = user.id
    await AppleWalletService.earn(
        db_session, uid, 10, reason="reward", idempotency_key="earn-reason:1"
    )
    await db_session.commit()

    with pytest.raises(LedgerKeyConflictError):
        await AppleWalletService.earn(
            db_session, uid, 10, reason="other_reason", idempotency_key="earn-reason:1"
        )
    await db_session.rollback()
    assert await AppleWalletService.get_balance(db_session, uid) == 10


@pytest.mark.asyncio
async def test_conflicting_reference_is_refused(db_session):
    user = await create_test_user(db_session, "ایرن ۴")
    uid = user.id
    await AppleWalletService.earn(
        db_session, uid, 10, reason="reward", ref_type="match", ref_id=1,
        idempotency_key="earn-ref:1",
    )
    await db_session.commit()

    with pytest.raises(LedgerKeyConflictError):
        await AppleWalletService.earn(
            db_session, uid, 10, reason="reward", ref_type="match", ref_id=2,
            idempotency_key="earn-ref:1",
        )
    await db_session.rollback()
    assert await AppleWalletService.get_balance(db_session, uid) == 10


@pytest.mark.asyncio
async def test_cross_user_key_reuse_is_a_conflict_not_a_silent_noop(db_session):
    owner = await create_test_user(db_session, "ایرن مالک")
    other = await create_test_user(db_session, "ایرن دیگر")
    # Capture ids before any rollback: rollback expires ORM instances, and
    # reading an expired attribute outside the greenlet raises MissingGreenlet.
    owner_id, other_id = owner.id, other.id
    await AppleWalletService.earn(
        db_session, owner_id, 40, reason="reward", idempotency_key="earn-xuser:1"
    )
    await db_session.commit()

    # The same key used for another user must NOT look like a successful
    # replay: that user would silently receive nothing.
    with pytest.raises(LedgerKeyConflictError):
        await AppleWalletService.earn(
            db_session, other_id, 40, reason="reward", idempotency_key="earn-xuser:1"
        )
    await db_session.rollback()

    assert await AppleWalletService.get_balance(db_session, owner_id) == 40
    assert await AppleWalletService.get_balance(db_session, other_id) == 0


@pytest.mark.asyncio
async def test_credit_key_reused_as_debit_is_a_conflict(db_session):
    user = await create_test_user(db_session, "ایرن ۵")
    uid = user.id
    await AppleWalletService.earn(
        db_session, uid, 30, reason="reward", idempotency_key="earn-dir:1"
    )
    await db_session.commit()

    # debit_once with the same key and amount must not treat the credit as its
    # own debit: the sign differs.
    from sib.services.apple_wallet import LedgerKeyConflictError as Conflict

    with pytest.raises(Conflict):
        await AppleWalletService.debit_once(
            db_session, uid, 30, reason="reward", idempotency_key="earn-dir:1"
        )
    await db_session.rollback()
    assert await AppleWalletService.get_balance(db_session, uid) == 30


@pytest.mark.asyncio
async def test_debit_key_reused_as_credit_is_a_conflict(db_session):
    user = await create_test_user(db_session, "ایرن ۶")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 50, reason="seed")
    await AppleWalletService.debit_once(
        db_session, uid, 20, reason="buy", idempotency_key="debit-dir:1"
    )
    await db_session.commit()

    with pytest.raises(LedgerKeyConflictError):
        await AppleWalletService.earn(
            db_session, uid, 20, reason="buy", idempotency_key="debit-dir:1"
        )
    await db_session.rollback()
    assert await AppleWalletService.get_balance(db_session, uid) == 30


@pytest.mark.asyncio
async def test_debit_once_conflicting_amount_still_refused(db_session):
    """Regression guard: the signed-amount refactor must keep debit conflicts."""
    user = await create_test_user(db_session, "ایرن ۷")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 100, reason="seed")
    await AppleWalletService.debit_once(
        db_session, uid, 10, reason="buy", idempotency_key="debit-amt:1"
    )
    await db_session.commit()

    with pytest.raises(LedgerKeyConflictError):
        await AppleWalletService.debit_once(
            db_session, uid, 11, reason="buy", idempotency_key="debit-amt:1"
        )
    await db_session.rollback()
    assert await AppleWalletService.get_balance(db_session, uid) == 90


@pytest.mark.asyncio
async def test_unkeyed_earn_is_unchanged(db_session):
    user = await create_test_user(db_session, "ایرن ۸")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 5, reason="grant")
    await AppleWalletService.earn(db_session, uid, 5, reason="grant")
    await db_session.commit()
    assert await AppleWalletService.get_balance(db_session, uid) == 10
    count = await db_session.scalar(
        select(func.count()).select_from(AppleTransaction).where(
            AppleTransaction.user_id == uid
        )
    )
    assert count == 2
    # no wallet row was created by a zero-key path beyond the one used
    wallets = await db_session.scalar(
        select(func.count()).select_from(AppleWallet).where(AppleWallet.user_id == uid)
    )
    assert wallets == 1
