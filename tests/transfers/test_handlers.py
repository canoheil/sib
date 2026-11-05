"""Handler-level tests for user-to-user transfers (fake Update/Message/CallbackQuery).

No Telegram network and no aiogram dispatcher: the router's handler functions
are called directly with the same light mocks the other handler tests use
(``MagicMock`` message shells + ``AsyncMock`` for every API call). The
database is the isolated test database.

Assertions read through a FRESH session because the handlers own their own
transactions (``get_session()``), exactly like production.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aiogram.types import User as TGUser
from sqlalchemy import delete, select

from sib.core.bidi import RLM, LRM
from sib.database.connection import get_session
from sib.database.models import (
    AppleWallet,
    PendingTransfer,
    PendingTransferState,
    TransferLog,
)
from sib.handlers import transfers as transfer_handlers
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.transfers import (
    ITEM_APPLE, ITEM_DIAMOND, ITEM_GOLD, ITEM_OBSIDIAN,
    ITEM_DISEASE_PLANT, ITEM_DISEASE_ANTIDOTE,
)
from sib.services.transfers.service import cleanup_expired_pending
from sib.tests.test_parties import create_test_user

GROUP_CHAT_ID = -780_001
CONFIRMATION_MESSAGE_ID = 9001


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session(db_session):
    """The shared test session with the two transfer tables emptied first."""
    await db_session.execute(delete(TransferLog))
    await db_session.execute(delete(PendingTransfer))
    await db_session.commit()
    return db_session


def _tg_user(uid: int, name: str, *, username=None, is_bot: bool = False) -> TGUser:
    return TGUser(id=uid, is_bot=is_bot, first_name=name, username=username)


def _group_message(sender, text, *, target=None, chat_id=GROUP_CHAT_ID, message_id=500):
    """A group reply message shell (same shape as tests/test_sib_hug.py)."""
    message = MagicMock()
    message.chat = MagicMock()
    message.chat.type = "supergroup"
    message.chat.id = chat_id
    message.message_id = message_id
    message.from_user = sender
    message.text = text
    message.reply = AsyncMock(
        return_value=MagicMock(message_id=CONFIRMATION_MESSAGE_ID)
    )
    if target is None:
        message.reply_to_message = None
    else:
        message.reply_to_message = MagicMock()
        message.reply_to_message.from_user = target
        message.reply_to_message.message_id = message_id - 1
    return message


def _callback(pending_id: int, presser_id: int, *, action: str = "confirm"):
    callback = MagicMock()
    callback.data = f"tr:{action}:{pending_id}"
    callback.from_user = MagicMock()
    callback.from_user.id = presser_id
    callback.message = MagicMock()
    callback.message.chat = MagicMock()
    callback.message.chat.id = GROUP_CHAT_ID
    callback.message.message_id = CONFIRMATION_MESSAGE_ID
    callback.message.edit_reply_markup = AsyncMock()
    callback.answer = AsyncMock()
    return callback


async def _run_command(message) -> None:
    await transfer_handlers.handle_transfer_command(message)


async def _run_confirm(callback):
    """Run the ✅ handler; returns (bot, patched edit helper)."""
    bot = AsyncMock()
    with patch.object(
        transfer_handlers, "safe_edit_callback_message", new=AsyncMock()
    ) as edit:
        await transfer_handlers.handle_transfer_confirm(callback, bot=bot)
    return bot, edit


async def _run_cancel(callback):
    bot = AsyncMock()
    with patch.object(
        transfer_handlers, "safe_edit_callback_message", new=AsyncMock()
    ) as edit:
        await transfer_handlers.handle_transfer_cancel(callback, bot=bot)
    return bot, edit


def _toast(callback) -> str:
    return callback.answer.await_args.args[0]


async def _set_config(session, key: str, value) -> None:
    await ConfigService.set(session, key, value)
    await session.commit()


async def _credit(session, user_id: int, amount: int) -> None:
    await AppleWalletService.earn(session, user_id, amount, reason="test_seed")
    await session.commit()


async def _give_items(session, user_id: int, item_key: str, quantity: int) -> None:
    await ItemService.grant(session, user_id, item_key, quantity)
    await session.commit()


async def _balance(user_id: int) -> int:
    async with get_session() as session:
        return await AppleWalletService.get_balance(session, user_id)


async def _items(user_id: int, item_key: str) -> int:
    async with get_session() as session:
        return await ItemService.get_total(session, user_id, item_key)


async def _pending_rows():
    async with get_session() as session:
        return list(
            (
                await session.scalars(
                    select(PendingTransfer).order_by(PendingTransfer.id)
                )
            ).all()
        )


async def _pending_row(pending_id: int):
    async with get_session() as session:
        return await session.get(PendingTransfer, pending_id)


async def _logs():
    async with get_session() as session:
        return list((await session.scalars(select(TransferLog))).all())


async def _drain_wallet(user_id: int) -> None:
    """Spend everything outside the handler (simulates a real concurrent spend)."""
    async with get_session() as session:
        wallet = await session.get(AppleWallet, user_id)
        if wallet is not None:
            wallet.balance = 0


async def _open_confirmation(session, *, text="انتقال سیب 100", balance=500):
    """Sender + receiver in the DB and one confirmation message sent."""
    sender = await create_test_user(session, "فرستنده")
    receiver = await create_test_user(session, "گیرنده")
    # Realistic handles: the DB rows are what the success message renders
    # from, while the confirmation uses the Telegram objects.
    sender.username = "sender"
    receiver.username = "receiver"
    await session.commit()
    await _credit(session, sender.id, balance)

    message = _group_message(
        _tg_user(sender.id, "فرستنده", username="sender"),
        text,
        target=_tg_user(receiver.id, "گیرنده", username="receiver"),
    )
    await _run_command(message)

    (row,) = await _pending_rows()
    return sender, receiver, message, row


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regular_apple_transfer_end_to_end(session):
    sender, receiver, message, row = await _open_confirmation(session)

    # --- confirmation -----------------------------------------------------
    body = message.reply.await_args.args[0]
    assert "⚠️ تأیید انتقال" in body
    assert f"از: {LRM}فرستنده{LRM} (@{LRM}sender{LRM})" in body
    assert f"به: {LRM}گیرنده{LRM} (@{LRM}receiver{LRM})" in body
    assert "مقدار: ۱۰۰ 🍎 سیب" in body
    assert "⏳ این تأیید تا ۶۰ ثانیه دیگر منقضی می‌شود." in body

    keyboard = message.reply.await_args.kwargs["reply_markup"]
    (button_row,) = keyboard.inline_keyboard
    assert [button.callback_data for button in button_row] == [
        f"tr:confirm:{row.id}",
        f"tr:cancel:{row.id}",
    ]
    assert [button.text for button in button_row] == [
        "✅ بله، انتقال بده",
        "❌ انصراف",
    ]

    assert row.state == PendingTransferState.PENDING
    assert row.chat_id == GROUP_CHAT_ID
    assert row.message_id == CONFIRMATION_MESSAGE_ID
    assert 55 <= (row.expires_at - row.created_at).total_seconds() <= 61

    # --- ✅ ----------------------------------------------------------------
    callback = _callback(row.id, sender.id)
    bot, edit = await _run_confirm(callback)

    assert await _balance(sender.id) == 400
    assert await _balance(receiver.id) == 100

    (log,) = await _logs()
    assert (log.from_user_id, log.to_user_id) == (sender.id, receiver.id)
    assert (log.item_type, log.amount, log.fee) == (ITEM_APPLE, 100, 0)

    assert (await _pending_row(row.id)).state == PendingTransferState.DONE

    edit_text = edit.await_args.args[2]
    assert edit_text == (
        RLM
        + f"✅ ۱۰۰ سیب به {LRM}گیرنده{LRM} (@{LRM}receiver{LRM}) منتقل شد."
    )
    assert edit.await_args.kwargs["reply_markup"] is None
    assert _toast(callback) == "✅ انتقال انجام شد."
    assert callback.answer.await_args.kwargs["show_alert"] is False

    # Receiver notification: same chat, replies into the transfer's thread.
    notification = bot.send_message.await_args.kwargs
    assert notification["chat_id"] == GROUP_CHAT_ID
    assert notification["reply_to_message_id"] == CONFIRMATION_MESSAGE_ID
    assert notification["text"] == (
        RLM
        + f"[{LRM}گیرنده{LRM} (@{LRM}receiver{LRM})](tg://user?id={receiver.id}) "
        f"📥 {LRM}فرستنده{LRM} (@{LRM}sender{LRM}) مبلغ ۱۰۰ سیب به شما منتقل کرد."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item_key", [ITEM_GOLD, ITEM_DIAMOND, ITEM_OBSIDIAN, ITEM_DISEASE_PLANT, ITEM_DISEASE_ANTIDOTE]
)
async def test_special_apple_transfers_end_to_end(session, item_key):
    sender = await create_test_user(session, f"فرستنده {item_key}")
    receiver = await create_test_user(session, f"گیرنده {item_key}")
    await _give_items(session, sender.id, item_key, 20)

    message = _group_message(
        _tg_user(sender.id, "فرستنده"),
        f"انتقال {transfer_name(item_key)} 10",
        target=_tg_user(receiver.id, "گیرنده"),
    )
    await _run_command(message)

    (row,) = await _pending_rows()
    assert row.item_type == item_key

    callback = _callback(row.id, sender.id)
    await _run_confirm(callback)

    assert await _items(sender.id, item_key) == 10
    assert await _items(receiver.id, item_key) == 10

    (log,) = await _logs()
    assert (log.item_type, log.amount) == (item_key, 10)

    assert (await _pending_row(row.id)).state == PendingTransferState.DONE


def transfer_name(item_key: str) -> str:
    return {
        ITEM_GOLD: "سیب طلایی",
        ITEM_DIAMOND: "سیب الماسی",
        ITEM_OBSIDIAN: "سیب ابسیدینی",
        ITEM_DISEASE_PLANT: "گیاه ویژه",
        ITEM_DISEASE_ANTIDOTE: "پادزهر آماده",
    }[item_key]


@pytest.mark.asyncio
async def test_fee_is_shown_in_the_confirmation_and_charged_on_confirm(session):
    await _set_config(session, "transfer_fee_percent", 2)
    sender, receiver, message, row = await _open_confirmation(session, balance=500)

    body = message.reply.await_args.args[0]
    assert "کارمزد: ۲ سیب" in body
    assert "مجموع کسر از شما: ۱۰۲ سیب" in body

    await _run_confirm(_callback(row.id, sender.id))

    assert await _balance(sender.id) == 398
    assert await _balance(receiver.id) == 100
    (log,) = await _logs()
    assert (log.amount, log.fee) == (100, 2)


# ---------------------------------------------------------------------------
# Pre-check refusals (no confirmation, no pending row)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_transfer_is_refused_with_a_message(session):
    user = await create_test_user(session, "خودفرست")
    await _credit(session, user.id, 100)

    message = _group_message(
        _tg_user(user.id, "خودفرست"), "انتقال سیب 10", target=_tg_user(user.id, "خودفرست")
    )
    await _run_command(message)

    assert message.reply.await_args.args[0] == "نمی‌توانید به خودتان انتقال دهید."
    assert await _pending_rows() == []


@pytest.mark.asyncio
async def test_bot_target_is_refused_with_a_message(session):
    sender = await create_test_user(session, "به ربات")
    await _credit(session, sender.id, 100)

    message = _group_message(
        _tg_user(sender.id, "به ربات"),
        "انتقال سیب 10",
        target=_tg_user(700_000_001, "ربات سیب", is_bot=True),
    )
    await _run_command(message)

    assert message.reply.await_args.args[0] == "انتقال به بات ممکن نیست."
    assert await _pending_rows() == []


@pytest.mark.asyncio
async def test_disabled_transfers_are_refused(session):
    await _set_config(session, "transfer_enabled", False)
    sender = await create_test_user(session, "غیرفعال")
    receiver = await create_test_user(session, "گیرنده غیرفعال")
    await _credit(session, sender.id, 500)

    message = _group_message(
        _tg_user(sender.id, "غیرفعال"),
        "انتقال سیب 100",
        target=_tg_user(receiver.id, "گیرنده غیرفعال"),
    )
    await _run_command(message)

    assert message.reply.await_args.args[0] == "انتقال موقتاً غیرفعال است."
    assert await _pending_rows() == []


@pytest.mark.asyncio
async def test_amount_above_the_maximum_is_refused(session):
    await _set_config(session, "transfer_max_amount", 10_000)
    sender = await create_test_user(session, "بیش از سقف")
    receiver = await create_test_user(session, "گیرنده سقف")
    await _credit(session, sender.id, 50_000)

    message = _group_message(
        _tg_user(sender.id, "بیش از سقف"),
        "انتقال سیب 10001",
        target=_tg_user(receiver.id, "گیرنده سقف"),
    )
    await _run_command(message)

    assert message.reply.await_args.args[0] == "مقدار باید بین ۱ و ۱۰۰۰۰ باشد."
    assert await _pending_rows() == []


@pytest.mark.asyncio
async def test_unknown_target_user_is_refused(session):
    sender = await create_test_user(session, "گیرنده ناموجود")
    await _credit(session, sender.id, 500)

    message = _group_message(
        _tg_user(sender.id, "گیرنده ناموجود"),
        "انتقال سیب 10",
        target=_tg_user(999_000_111, "ناشناس"),
    )
    await _run_command(message)

    assert message.reply.await_args.args[0] == "کاربر مقصد پیدا نشد."
    assert await _pending_rows() == []


@pytest.mark.asyncio
async def test_banned_participant_is_refused(session):
    sender = await create_test_user(session, "به مسدود")
    banned = await create_test_user(session, "مسدود")
    banned.is_banned = True
    await session.commit()
    await _credit(session, sender.id, 500)

    message = _group_message(
        _tg_user(sender.id, "به مسدود"),
        "انتقال سیب 10",
        target=_tg_user(banned.id, "مسدود"),
    )
    await _run_command(message)

    assert message.reply.await_args.args[0] == "یکی از طرفین مسدود است."
    assert await _pending_rows() == []


# ---------------------------------------------------------------------------
# Silent cases (no reply at all)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "انتقال سیب 100 به علی",  # trailing tokens
        "انتقال سیب",  # no number
        "انتقال سیب 0",  # zero
        "انتقال سیب طلایی",  # long type phrase, no number
        "انتقال",  # nothing
        "سلام",  # unrelated text (the filter would not even route it)
    ],
)
async def test_unparseable_commands_stay_silent(session, text):
    sender = await create_test_user(session, f"ساکت {abs(hash(text)) % 1000}")
    receiver = await create_test_user(session, f"گیرنده ساکت {abs(hash(text)) % 1000}")
    await _credit(session, sender.id, 500)

    message = _group_message(
        _tg_user(sender.id, "ساکت"), text, target=_tg_user(receiver.id, "گیرنده")
    )
    await _run_command(message)

    assert message.reply.await_count == 0
    assert await _pending_rows() == []


@pytest.mark.asyncio
async def test_non_reply_is_silent(session):
    sender = await create_test_user(session, "بدون ریپلای")
    await _credit(session, sender.id, 500)

    message = _group_message(_tg_user(sender.id, "بدون ریپلای"), "انتقال سیب 100")
    await _run_command(message)

    assert message.reply.await_count == 0
    assert await _pending_rows() == []


@pytest.mark.asyncio
async def test_private_chat_is_silent(session):
    sender = await create_test_user(session, "چت خصوصی")
    receiver = await create_test_user(session, "گیرنده خصوصی")
    await _credit(session, sender.id, 500)

    message = _group_message(
        _tg_user(sender.id, "چت خصوصی"),
        "انتقال سیب 100",
        target=_tg_user(receiver.id, "گیرنده خصوصی"),
    )
    message.chat.type = "private"
    await _run_command(message)

    assert message.reply.await_count == 0
    assert await _pending_rows() == []


# ---------------------------------------------------------------------------
# Callback safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_sender_press_is_rejected_without_touching_state(session):
    sender, receiver, _message, row = await _open_confirmation(session)
    intruder = await create_test_user(session, "فضول")

    callback = _callback(row.id, intruder.id)
    _bot, edit = await _run_confirm(callback)

    assert _toast(callback) == "این دکمه برای شما نیست."
    assert edit.await_count == 0  # the message is not edited either
    assert (await _pending_row(row.id)).state == PendingTransferState.PENDING
    assert await _balance(sender.id) == 500
    assert await _balance(receiver.id) == 0
    assert await _logs() == []


@pytest.mark.asyncio
async def test_non_sender_cannot_cancel(session):
    sender, _receiver, _message, row = await _open_confirmation(session)
    intruder = await create_test_user(session, "فضول لغو")

    callback = _callback(row.id, intruder.id, action="cancel")
    _bot, edit = await _run_cancel(callback)

    assert _toast(callback) == "این دکمه برای شما نیست."
    assert edit.await_count == 0
    assert (await _pending_row(row.id)).state == PendingTransferState.PENDING


@pytest.mark.asyncio
async def test_double_click_pays_exactly_once(session):
    sender, receiver, _message, row = await _open_confirmation(session)

    first_callback = _callback(row.id, sender.id)
    await _run_confirm(first_callback)

    second_callback = _callback(row.id, sender.id)
    _bot, second_edit = await _run_confirm(second_callback)

    assert _toast(second_callback) == "قبلاً پردازش شده است."
    assert second_edit.await_count == 0
    assert second_callback.message.edit_reply_markup.await_count == 1

    assert await _balance(sender.id) == 400
    assert await _balance(receiver.id) == 100
    assert len(await _logs()) == 1
    assert (await _pending_row(row.id)).state == PendingTransferState.DONE


@pytest.mark.asyncio
async def test_missing_pending_row_answers_invalid_request(session):
    sender = await create_test_user(session, "درخواست نامعتبر")
    callback = _callback(987_654, sender.id)

    _bot, edit = await _run_confirm(callback)

    assert _toast(callback) == "این درخواست معتبر نیست."
    assert edit.await_count == 0
    assert callback.message.edit_reply_markup.await_count == 1


@pytest.mark.asyncio
async def test_expired_confirmation_is_cancelled_without_moving_money(session):
    sender, receiver, _message, row = await _open_confirmation(session)

    future = transfer_handlers._now() + timedelta(seconds=61)
    callback = _callback(row.id, sender.id)
    with patch.object(transfer_handlers, "_now", lambda: future):
        bot, edit = await _run_confirm(callback)

    assert _toast(callback) == "⏳ این تأیید منقضی شده است."
    assert (await _pending_row(row.id)).state == PendingTransferState.CANCELLED
    assert await _balance(sender.id) == 500
    assert await _balance(receiver.id) == 0
    assert await _logs() == []

    expired_text = edit.await_args.args[2]
    assert expired_text.endswith("⏳ این تأیید منقضی شده است.")
    assert edit.await_args.kwargs["reply_markup"] is None
    assert bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_cancel_button_cancels_without_moving_money(session):
    sender, receiver, _message, row = await _open_confirmation(session)

    callback = _callback(row.id, sender.id, action="cancel")
    _bot, edit = await _run_cancel(callback)

    assert _toast(callback) == "لغو شد."
    assert (await _pending_row(row.id)).state == PendingTransferState.CANCELLED
    assert edit.await_args.args[2] == "❌ انتقال لغو شد."
    assert edit.await_args.kwargs["reply_markup"] is None
    assert await _balance(sender.id) == 500
    assert await _balance(receiver.id) == 0
    assert await _logs() == []


@pytest.mark.asyncio
async def test_second_cancel_press_says_already_processed(session):
    sender, _receiver, _message, row = await _open_confirmation(session)
    await _run_cancel(_callback(row.id, sender.id, action="cancel"))

    callback = _callback(row.id, sender.id, action="cancel")
    _bot, edit = await _run_cancel(callback)

    assert _toast(callback) == "قبلاً پردازش شده است."
    assert edit.await_count == 0


@pytest.mark.asyncio
async def test_insufficient_balance_at_click_time_fails_cleanly(session):
    sender, receiver, _message, row = await _open_confirmation(session)
    await _drain_wallet(sender.id)  # spent after the confirmation was created

    callback = _callback(row.id, sender.id)
    bot, edit = await _run_confirm(callback)

    assert _toast(callback) == "موجودی کافی نیست."
    assert (await _pending_row(row.id)).state == PendingTransferState.FAILED
    assert edit.await_args.args[2] == "❌ موجودی کافی نیست."
    assert edit.await_args.kwargs["reply_markup"] is None
    assert await _balance(sender.id) == 0
    assert await _balance(receiver.id) == 0
    assert await _logs() == []
    assert bot.send_message.await_count == 0  # the receiver is NOT notified


@pytest.mark.asyncio
async def test_stale_click_after_cleanup_says_already_processed(session):
    sender, _receiver, _message, row = await _open_confirmation(session)

    # Force the row past its TTL and let the periodic cleanup cancel it.
    async with get_session() as cleanup_session:
        live = await cleanup_session.get(PendingTransfer, row.id)
        live.expires_at = transfer_handlers._now() - timedelta(seconds=5)
    async with get_session() as cleanup_session:
        assert await cleanup_expired_pending(cleanup_session) == 1

    assert (await _pending_row(row.id)).state == PendingTransferState.CANCELLED

    callback = _callback(row.id, sender.id)
    _bot, edit = await _run_confirm(callback)

    assert _toast(callback) == "قبلاً پردازش شده است."
    assert edit.await_count == 0
    assert await _balance(sender.id) == 500
    assert await _logs() == []


# ---------------------------------------------------------------------------
# Registration / cleanup loop
# ---------------------------------------------------------------------------


def test_router_is_wired_into_the_dispatcher_registration():
    """``register_all_handlers`` includes this exact router (one message
    observer for the command, one per callback action)."""
    from sib import handlers as handlers_package

    assert handlers_package.transfers_router is transfer_handlers.router
    assert transfer_handlers.router.name == "transfers"

    assert len(transfer_handlers.router.message.handlers) == 1
    callbacks = [
        handler.callback.__name__
        for handler in transfer_handlers.router.callback_query.handlers
    ]
    assert callbacks == ["handle_transfer_confirm", "handle_transfer_cancel"]


@pytest.mark.asyncio
async def test_command_filter_only_routes_the_transfer_prefix():
    """The message filter is a cheap prefix gate; the parser decides after it."""
    filters = transfer_handlers.router.message.handlers[0].filters

    async def routed(text: str) -> bool:
        message = MagicMock()
        message.text = text
        results = [bool(await f.call(message)) for f in filters]
        return all(results)

    assert await routed("انتقال سیب 100")
    assert await routed("انتقال  سیب الماسی ۵")
    assert await routed("انتقال سیب 100 به علی")  # routed, then silent
    assert not await routed("سلام")
    assert not await routed("۱۰۰ سیب")
    assert not await routed("transfer سیب 100")


@pytest.mark.asyncio
async def test_callback_filters_match_only_well_formed_payloads():
    confirm_handler, cancel_handler = transfer_handlers.router.callback_query.handlers

    async def matches(handler, data: str) -> bool:
        callback = MagicMock()
        callback.data = data
        results = [bool(await f.call(callback)) for f in handler.filters]
        return all(results)

    assert await matches(confirm_handler, "tr:confirm:12")
    assert not await matches(confirm_handler, "tr:cancel:12")
    assert not await matches(confirm_handler, "tr:confirm:12x")
    assert await matches(cancel_handler, "tr:cancel:7")
    assert not await matches(cancel_handler, "tr:confirm:7")


@pytest.mark.asyncio
async def test_cleanup_loop_stops_on_the_stop_event():
    import asyncio

    stop_event = asyncio.Event()
    stop_event.set()  # already stopping: the loop must return immediately
    await asyncio.wait_for(
        transfer_handlers.run_pending_cleanup_forever(stop_event), timeout=5
    )
