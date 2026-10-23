"""Service-level tests for user-to-user transfers.

No Telegram imports: the service is exercised directly against the same
database fixtures as the other service tests. ``FOR UPDATE`` is a no-op on
SQLite, so the real two-session race test is marked ``requires_row_locks``
(it runs on MySQL/MariaDB).
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from sib.database.models import (
    AppleWallet,
    PendingTransfer,
    PendingTransferState,
    TransferLog,
    User,
    UserItem,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.transfers import (
    ITEM_APPLE,
    ITEM_DIAMOND,
    ITEM_GOLD,
    ITEM_OBSIDIAN,
    TransferService,
    cleanup_expired_pending,
    execute_transfer,
    tehran_day_bounds,
)
from sib.tests.conftest import requires_row_locks
from sib.tests.test_parties import create_test_user

SPECIAL_ITEMS = (ITEM_GOLD, ITEM_DIAMOND, ITEM_OBSIDIAN)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session(db_session):
    """The shared db_session with the transfer tables emptied first.

    ``db_session`` cleans most tables between tests but knows nothing about
    the two new ones, and leftover log rows would leak into the cooldown and
    daily-limit checks.
    """
    await db_session.execute(delete(TransferLog))
    await db_session.execute(delete(PendingTransfer))
    await db_session.commit()
    return db_session


async def _user(session, name: str) -> User:
    user = await create_test_user(session, name)
    return user


async def _set_config(session, key: str, value) -> None:
    await ConfigService.set(session, key, value)
    await session.commit()


async def _credit(session, user_id: int, amount: int) -> None:
    await AppleWalletService.earn(session, user_id, amount, reason="test_seed")
    await session.commit()


async def _give_items(session, user_id: int, item_key: str, quantity: int) -> None:
    await ItemService.grant(session, user_id, item_key, quantity)
    await session.commit()


async def _logs(session) -> list[TransferLog]:
    return list((await session.scalars(select(TransferLog))).all())


# ---------------------------------------------------------------------------
# Successful transfers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regular_apple_transfer_moves_balance_and_writes_one_log(session):
    sender = await _user(session, "فرستنده")
    receiver = await _user(session, "گیرنده")
    await _credit(session, sender.id, 500)

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 120)
    await session.commit()

    assert result.ok is True
    assert result.reason is None
    assert result.amount_sent == 120
    assert result.amount_received == 120
    assert result.fee == 0
    assert result.transfer_id is not None

    assert await AppleWalletService.get_balance(session, sender.id) == 380
    assert await AppleWalletService.get_balance(session, receiver.id) == 120

    rows = await _logs(session)
    assert len(rows) == 1
    log = rows[0]
    assert log.id == result.transfer_id
    assert (log.from_user_id, log.to_user_id) == (sender.id, receiver.id)
    assert log.item_type == ITEM_APPLE
    assert log.amount == 120
    assert log.fee == 0


@pytest.mark.asyncio
async def test_transfer_creates_the_receiver_wallet_when_missing(session):
    sender = await _user(session, "کیف‌دار")
    receiver = await _user(session, "بی‌کیف")
    await _credit(session, sender.id, 50)
    # No wallet row for the receiver at all.
    assert (
        await session.scalar(
            select(AppleWallet).where(AppleWallet.user_id == receiver.id)
        )
        is None
    )

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 20)
    await session.commit()

    assert result.ok is True
    assert await AppleWalletService.get_balance(session, receiver.id) == 20
    assert await AppleWalletService.get_balance(session, sender.id) == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("item_key", SPECIAL_ITEMS)
async def test_special_apple_transfer_moves_inventory_rows(session, item_key):
    sender = await _user(session, f"فرستنده {item_key}")
    receiver = await _user(session, f"گیرنده {item_key}")
    await _give_items(session, sender.id, item_key, 10)

    result = await execute_transfer(session, sender.id, receiver.id, item_key, 4)
    await session.commit()

    assert result.ok is True
    assert result.amount_sent == 4
    assert result.amount_received == 4
    assert await ItemService.get_total(session, sender.id, item_key) == 6
    assert await ItemService.get_total(session, receiver.id, item_key) == 4

    row = await session.scalar(
        select(UserItem).where(
            UserItem.user_id == receiver.id, UserItem.item_key == item_key
        )
    )
    assert row is not None and row.quantity == 4

    (log,) = await _logs(session)
    assert log.item_type == item_key
    assert log.amount == 4


@pytest.mark.asyncio
async def test_special_apple_transfer_out_of_stock_is_refused(session):
    sender = await _user(session, "کم‌موجود")
    receiver = await _user(session, "گیرنده کم‌موجود")
    await _give_items(session, sender.id, ITEM_OBSIDIAN, 1)

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_OBSIDIAN, 2)
    await session.commit()

    assert (result.ok, result.reason) == (False, "insufficient")
    assert await ItemService.get_total(session, sender.id, ITEM_OBSIDIAN) == 1
    assert await ItemService.get_total(session, receiver.id, ITEM_OBSIDIAN) == 0
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_execute_transfer_never_commits(session):
    """The service flushes but leaves the transaction to the caller."""
    sender = await _user(session, "بدون کامیت")
    receiver = await _user(session, "گیرنده بدون کامیت")
    await _credit(session, sender.id, 100)
    sender_id, receiver_id = sender.id, receiver.id

    result = await execute_transfer(session, sender_id, receiver_id, ITEM_APPLE, 40)
    assert result.ok is True

    await session.rollback()  # the caller's transaction is not committed

    assert await AppleWalletService.get_balance(session, sender_id) == 100
    assert await AppleWalletService.get_balance(session, receiver_id) == 0
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_transfer_service_facade_delegates_to_execute_transfer(session):
    sender = await _user(session, "نمای سرویس")
    receiver = await _user(session, "گیرنده نما")
    await _give_items(session, sender.id, ITEM_GOLD, 3)

    result = await TransferService.transfer(
        session, sender.id, receiver.id, ITEM_GOLD, 2
    )
    await session.commit()

    assert result.ok is True
    assert await ItemService.get_total(session, receiver.id, ITEM_GOLD) == 2


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fee_is_paid_by_sender_and_recorded(session):
    sender = await _user(session, "پرداخت‌کننده کارمزد")
    receiver = await _user(session, "دریافت‌کننده")
    await _credit(session, sender.id, 500)
    await _set_config(session, "transfer_fee_percent", 5)

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 100)
    await session.commit()

    assert result.ok is True
    assert result.fee == 5
    assert result.amount_sent == 105
    assert result.amount_received == 100
    assert await AppleWalletService.get_balance(session, sender.id) == 395
    assert await AppleWalletService.get_balance(session, receiver.id) == 100
    (log,) = await _logs(session)
    assert (log.amount, log.fee) == (100, 5)


@pytest.mark.asyncio
async def test_fee_rounds_up(session):
    sender = await _user(session, "کارمزد کسری")
    receiver = await _user(session, "دریافت کسری")
    await _credit(session, sender.id, 500)
    await _set_config(session, "transfer_fee_percent", 5)

    # 69 * 5% = 3.45 -> ceil -> 4
    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 69)
    await session.commit()

    assert result.ok is True
    assert result.fee == 4
    assert result.amount_sent == 73
    assert await AppleWalletService.get_balance(session, sender.id) == 427
    assert await AppleWalletService.get_balance(session, receiver.id) == 69


@pytest.mark.asyncio
async def test_fee_applies_to_inventory_items_too(session):
    sender = await _user(session, "کارمزد طلایی")
    receiver = await _user(session, "گیرنده طلایی")
    await _give_items(session, sender.id, ITEM_GOLD, 20)
    await _set_config(session, "transfer_fee_percent", 10)

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_GOLD, 10)
    await session.commit()

    assert result.ok is True
    assert result.fee == 1
    assert await ItemService.get_total(session, sender.id, ITEM_GOLD) == 9
    assert await ItemService.get_total(session, receiver.id, ITEM_GOLD) == 10


@pytest.mark.asyncio
async def test_fee_is_charged_on_top_of_the_amount_for_insufficient_check(session):
    sender = await _user(session, "لبه موجودی")
    receiver = await _user(session, "گیرنده لبه")
    await _credit(session, sender.id, 100)
    await _set_config(session, "transfer_fee_percent", 5)

    # 100 + 5% = 105 > 100: the exact amount is not enough.
    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 100)
    await session.commit()

    assert (result.ok, result.reason) == (False, "insufficient")
    assert await AppleWalletService.get_balance(session, sender.id) == 100
    assert await AppleWalletService.get_balance(session, receiver.id) == 0
    assert await _logs(session) == []


# ---------------------------------------------------------------------------
# Refusals (each with NO side effects)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_transfer_is_refused(session):
    user = await _user(session, "خودفرست")
    await _credit(session, user.id, 100)

    result = await execute_transfer(session, user.id, user.id, ITEM_APPLE, 10)
    await session.commit()

    assert (result.ok, result.reason) == (False, "self")
    assert result.transfer_id is None
    assert await AppleWalletService.get_balance(session, user.id) == 100
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_bot_transfer_is_refused(session):
    sender = await _user(session, "به ربات")
    bot = await create_test_user(session, "ربات سیب")
    bot.is_bot = True
    await session.commit()
    await _credit(session, sender.id, 100)

    result = await execute_transfer(session, sender.id, bot.id, ITEM_APPLE, 10)
    await session.commit()

    assert (result.ok, result.reason) == (False, "bot")
    assert await AppleWalletService.get_balance(session, sender.id) == 100
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_bot_sender_is_refused_too(session):
    bot = await create_test_user(session, "ربات فرستنده")
    bot.is_bot = True
    receiver = await _user(session, "گیرنده از ربات")
    await session.commit()
    await _credit(session, bot.id, 100)

    result = await execute_transfer(session, bot.id, receiver.id, ITEM_APPLE, 10)
    await session.commit()

    assert (result.ok, result.reason) == (False, "bot")
    assert await AppleWalletService.get_balance(session, bot.id) == 100


@pytest.mark.asyncio
async def test_missing_receiver_is_refused(session):
    sender = await _user(session, "گیرنده ناموجود")
    await _credit(session, sender.id, 100)

    result = await execute_transfer(session, sender.id, 999_999_999, ITEM_APPLE, 10)
    await session.commit()

    assert (result.ok, result.reason) == (False, "user_not_found")
    assert await AppleWalletService.get_balance(session, sender.id) == 100
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_missing_sender_is_refused(session):
    receiver = await _user(session, "گیرنده بی‌فرستنده")

    result = await execute_transfer(session, 999_999_999, receiver.id, ITEM_APPLE, 10)
    await session.commit()

    assert (result.ok, result.reason) == (False, "user_not_found")
    assert await AppleWalletService.get_balance(session, receiver.id) == 0


@pytest.mark.asyncio
async def test_banned_receiver_is_refused(session):
    sender = await _user(session, "به بن‌شده")
    banned = await _user(session, "بن‌شده")
    banned.is_banned = True
    await session.commit()
    await _credit(session, sender.id, 100)

    result = await execute_transfer(session, sender.id, banned.id, ITEM_APPLE, 10)
    await session.commit()

    assert (result.ok, result.reason) == (False, "banned")
    assert await AppleWalletService.get_balance(session, sender.id) == 100
    assert await AppleWalletService.get_balance(session, banned.id) == 0
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_banned_sender_is_refused(session):
    banned = await _user(session, "فرستنده بن‌شده")
    banned.is_banned = True
    receiver = await _user(session, "گیرنده سالم")
    await session.commit()
    await _credit(session, banned.id, 100)

    result = await execute_transfer(session, banned.id, receiver.id, ITEM_APPLE, 10)
    await session.commit()

    assert (result.ok, result.reason) == (False, "banned")
    assert await AppleWalletService.get_balance(session, banned.id) == 100
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_insufficient_balance_is_refused(session):
    sender = await _user(session, "کم‌موجود کیف")
    receiver = await _user(session, "گیرنده کیف")
    await _credit(session, sender.id, 50)

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 100)
    await session.commit()

    assert (result.ok, result.reason) == (False, "insufficient")
    assert result.amount_sent == 0 and result.amount_received == 0
    assert await AppleWalletService.get_balance(session, sender.id) == 50
    assert await AppleWalletService.get_balance(session, receiver.id) == 0
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_sender_without_a_wallet_is_insufficient(session):
    sender = await _user(session, "بی‌کیف فرستنده")
    receiver = await _user(session, "گیرنده بی‌کیف")

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 1)
    await session.commit()

    assert (result.ok, result.reason) == (False, "insufficient")
    assert await _logs(session) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0, -5, 10_001])
async def test_amount_out_of_bounds_is_refused(session, amount):
    sender = await _user(session, f"مبلغ {amount}")
    receiver = await _user(session, f"گیرنده مبلغ {amount}")
    await _credit(session, sender.id, 20_000)

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, amount)
    await session.commit()

    assert (result.ok, result.reason) == (False, "amount_out_of_bounds")
    assert await AppleWalletService.get_balance(session, sender.id) == 20_000
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_config_bounds_are_honoured(session):
    sender = await _user(session, "مرزها")
    receiver = await _user(session, "گیرنده مرزها")
    await _credit(session, sender.id, 1000)
    await _set_config(session, "transfer_min_amount", 10)
    await _set_config(session, "transfer_max_amount", 50)

    too_small = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 9)
    too_big = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 51)
    assert (too_small.reason, too_big.reason) == (
        "amount_out_of_bounds",
        "amount_out_of_bounds",
    )

    ok = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 10)
    await session.commit()
    assert ok.ok is True
    assert len(await _logs(session)) == 1


@pytest.mark.asyncio
async def test_unknown_item_type_is_refused(session):
    sender = await _user(session, "نوع ناشناس")
    receiver = await _user(session, "گیرنده نوع")
    await _credit(session, sender.id, 100)

    result = await execute_transfer(session, sender.id, receiver.id, "apple_red", 10)
    await session.commit()

    assert (result.ok, result.reason) == (False, "invalid_item_type")
    assert await AppleWalletService.get_balance(session, sender.id) == 100
    assert await _logs(session) == []


@pytest.mark.asyncio
async def test_daily_limit_blocks_the_next_transfer(session):
    sender = await _user(session, "سقف روزانه")
    receiver = await _user(session, "گیرنده سقف")
    await _credit(session, sender.id, 1000)
    await _set_config(session, "transfer_daily_limit", 1)

    first = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 10)
    await session.commit()
    assert first.ok is True

    second = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 10)
    await session.commit()

    assert (second.ok, second.reason) == (False, "daily_limit")
    assert await AppleWalletService.get_balance(session, sender.id) == 990
    assert await AppleWalletService.get_balance(session, receiver.id) == 10
    assert len(await _logs(session)) == 1


@pytest.mark.asyncio
async def test_daily_limit_counts_only_today(session):
    sender = await _user(session, "دیروزی")
    receiver = await _user(session, "گیرنده دیروزی")
    await _credit(session, sender.id, 1000)
    await _set_config(session, "transfer_daily_limit", 1)

    # A transfer from a previous Tehran day must not consume today's slot.
    yesterday = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    session.add(
        TransferLog(
            from_user_id=sender.id,
            to_user_id=receiver.id,
            item_type=ITEM_APPLE,
            amount=10,
            fee=0,
            created_at=yesterday,
        )
    )
    await session.commit()

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 10)
    await session.commit()

    assert result.ok is True


@pytest.mark.asyncio
async def test_cooldown_blocks_an_immediate_second_transfer(session):
    sender = await _user(session, "کول‌داون")
    receiver = await _user(session, "گیرنده کول‌داون")
    await _credit(session, sender.id, 1000)
    await _set_config(session, "transfer_cooldown_seconds", 3600)

    first = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 10)
    await session.commit()
    assert first.ok is True

    second = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 10)
    await session.commit()

    assert (second.ok, second.reason) == (False, "cooldown")
    assert await AppleWalletService.get_balance(session, sender.id) == 990
    assert await AppleWalletService.get_balance(session, receiver.id) == 10
    assert len(await _logs(session)) == 1


@pytest.mark.asyncio
async def test_cooldown_zero_disables_the_check(session):
    sender = await _user(session, "بدون کول‌داون")
    receiver = await _user(session, "گیرنده بدون کول‌داون")
    await _credit(session, sender.id, 1000)

    assert (await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 10)).ok
    assert (await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 10)).ok
    await session.commit()

    assert await AppleWalletService.get_balance(session, sender.id) == 980
    assert len(await _logs(session)) == 2


@pytest.mark.asyncio
async def test_disabled_flag_refuses_every_transfer(session):
    sender = await _user(session, "غیرفعال")
    receiver = await _user(session, "گیرنده غیرفعال")
    await _credit(session, sender.id, 1000)
    await _set_config(session, "transfer_enabled", False)

    result = await execute_transfer(session, sender.id, receiver.id, ITEM_APPLE, 10)
    await session.commit()

    assert (result.ok, result.reason) == (False, "disabled")
    assert result.amount_sent == 0 and result.transfer_id is None
    assert await AppleWalletService.get_balance(session, sender.id) == 1000
    assert await AppleWalletService.get_balance(session, receiver.id) == 0
    assert await _logs(session) == []


# ---------------------------------------------------------------------------
# Tehran day boundary helper
# ---------------------------------------------------------------------------


def test_tehran_day_bounds_are_utc_and_tehran_midnight():
    # 2026-01-01 12:00 UTC is 15:30 in Tehran -> the day started at
    # 2026-01-01 00:00 Tehran == 2025-12-31 20:30 UTC.
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    start, end = tehran_day_bounds(now.replace(tzinfo=None))

    assert start == datetime(2025, 12, 31, 20, 30)
    assert end == datetime(2026, 1, 1, 20, 30)
    assert start.tzinfo is None and end.tzinfo is None


def test_tehran_day_bounds_handle_the_late_utc_evening():
    # 21:00 UTC is 00:30 Tehran on the NEXT calendar day.
    now = datetime(2026, 1, 1, 21, 0)
    start, end = tehran_day_bounds(now)

    assert start == datetime(2026, 1, 1, 20, 30)
    assert end == datetime(2026, 1, 2, 20, 30)


# ---------------------------------------------------------------------------
# Pending transfer cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_expired_pending_cancels_only_expired_pending_rows(session):
    sender = await _user(session, "در انتظار")
    receiver = await _user(session, "گیرنده انتظار")
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    session.add_all(
        [
            PendingTransfer(
                from_user_id=sender.id,
                to_user_id=receiver.id,
                item_type=ITEM_APPLE,
                amount=10,
                chat_id=1,
                state=PendingTransferState.PENDING,
                expires_at=now - timedelta(minutes=5),
            ),
            PendingTransfer(
                from_user_id=sender.id,
                to_user_id=receiver.id,
                item_type=ITEM_APPLE,
                amount=20,
                chat_id=1,
                state=PendingTransferState.PENDING,
                expires_at=now + timedelta(minutes=5),
            ),
            PendingTransfer(
                from_user_id=sender.id,
                to_user_id=receiver.id,
                item_type=ITEM_APPLE,
                amount=30,
                chat_id=1,
                state=PendingTransferState.DONE,
                expires_at=now - timedelta(minutes=5),
            ),
        ]
    )
    await session.commit()

    assert await cleanup_expired_pending(session) == 1
    await session.commit()

    states = sorted(
        (row.amount, row.state)
        for row in (await session.scalars(select(PendingTransfer))).all()
    )
    assert states == [
        (10, PendingTransferState.CANCELLED),
        (20, PendingTransferState.PENDING),
        (30, PendingTransferState.DONE),
    ]


@pytest.mark.asyncio
async def test_cleanup_expired_pending_returns_zero_when_nothing_expired(session):
    assert await cleanup_expired_pending(session) == 0


# ---------------------------------------------------------------------------
# True concurrency: FOR UPDATE serializes two transfers of the same wallet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_double_execute_allows_exactly_one_transfer(session):
    """Two simultaneous transfers of 100 from a wallet holding exactly 100.

    Row locking must serialize the pair: the winner moves the balance and the
    loser re-reads the locked wallet (balance 0) and returns ``insufficient``.
    """
    from sib.database.connection import get_session

    sender = await _user(session, "همزمان فرستنده")
    receiver = await _user(session, "همزمان گیرنده")
    await _credit(session, sender.id, 100)
    sender_id, receiver_id = sender.id, receiver.id

    async def attempt():
        async with get_session() as race_session:
            return await execute_transfer(
                race_session, sender_id, receiver_id, ITEM_APPLE, 100
            )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert all(not isinstance(r, Exception) for r in results), results

    assert {r.reason for r in results} == {None, "insufficient"}
    assert sum(1 for r in results if r.ok) == 1

    assert await AppleWalletService.get_balance(session, sender_id) == 0
    assert await AppleWalletService.get_balance(session, receiver_id) == 100
    assert len(await _logs(session)) == 1
