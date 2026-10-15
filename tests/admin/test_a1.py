"""A1 integration tests use isolated SQLite; Telegram is always mocked."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import json
import pytest
from sqlalchemy import select, delete
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import CopyMessage
from sib.database.models import User, Group, AdminLog
from sib.services import broadcast as svc
from sib.services import admin_user_actions as actions
from sib.handlers import admin_broadcast as flow, admin_users as users
from sib.core.config import settings

@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_IDS", [10, 11])
    flow._pending_broadcast.clear()
    users._pending_amounts.clear()
    users._lookup.clear()

@pytest.fixture
async def seeded(db_session):
    await db_session.execute(delete(AdminLog))
    await db_session.merge(User(id=10, first_name="مدیر"))
    await db_session.merge(User(id=20, first_name="آزمایش", is_banned=False))
    db_session.add_all([Group(id=-100, title="یک"), Group(id=-200, title="دو"),
                       Group(id=-300, title="سه", is_active=False)])
    await db_session.commit()
    return db_session

@pytest.mark.asyncio
async def test_all_targets_and_audit(seeded, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(svc.asyncio, "sleep", sleep)
    bot = NS(copy_message=AsyncMock())
    result = await svc.broadcast(bot, seeded, 10, 10, 1)
    assert result.success == 2
    assert {c.kwargs['chat_id'] for c in bot.copy_message.call_args_list} == {-100, -200}
    assert sleep.await_count == 2
    log = await seeded.scalar(select(AdminLog).where(AdminLog.action == "broadcast"))
    assert json.loads(log.details)['success_count'] == 2

@pytest.mark.asyncio
async def test_one_target(seeded, monkeypatch):
    monkeypatch.setattr(svc.asyncio, "sleep", AsyncMock())
    bot = NS(copy_message=AsyncMock())
    result = await svc.broadcast(bot, seeded, 10, 10, 1, -100)
    assert result.success == 1
    bot.copy_message.assert_awaited_once_with(chat_id=-100, from_chat_id=10, message_id=1)

@pytest.mark.asyncio
async def test_kicked_skipped_next_time(seeded, monkeypatch):
    monkeypatch.setattr(svc.asyncio, "sleep", AsyncMock())
    bot = NS(copy_message=AsyncMock(side_effect=TelegramForbiddenError(
        method=CopyMessage(chat_id=-100, from_chat_id=10, message_id=1), message="bot was kicked")))
    result = await svc.broadcast(bot, seeded, 10, 10, 1, -100)
    assert len(result.failures) == 1
    assert not (await seeded.get(Group, -100)).is_active
    bot.copy_message.reset_mock()
    await svc.broadcast(bot, seeded, 10, 10, 1, -100)
    bot.copy_message.assert_not_awaited()

@pytest.mark.asyncio
async def test_retry_once(seeded, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(svc.asyncio, "sleep", sleep)
    exc = TelegramRetryAfter(method=CopyMessage(chat_id=-100, from_chat_id=10, message_id=1), message="wait", retry_after=2)
    bot = NS(copy_message=AsyncMock(side_effect=[exc, None]))
    result = await svc.broadcast(bot, seeded, 10, 10, 1, -100)
    assert result.success == 1
    assert bot.copy_message.await_count == 2
    sleep.assert_any_await(2)

@pytest.mark.asyncio
async def test_rate_limit_large_batch(seeded, monkeypatch):
    seeded.add_all([Group(id=-i, title="گروه") for i in range(1000, 1041)])
    await seeded.commit()
    sleep = AsyncMock()
    monkeypatch.setattr(svc.asyncio, "sleep", sleep)
    bot = NS(copy_message=AsyncMock())
    result = await svc.broadcast(bot, seeded, 10, 10, 1)
    assert result.success == 43
    assert sum(c.args[0] for c in sleep.call_args_list) == pytest.approx(2.15)

def test_summary_ten_reasons_persian():
    text = svc.BroadcastResult(1, [(i, "خطا") for i in range(20)]).summary()
    assert len(text.splitlines()) == 13
    assert "۱" in text
    assert not any(c in text for c in '0123456789')

@pytest.mark.asyncio
async def test_broadcast_rejects_nonadmin():
    message = NS(from_user=NS(id=99), answer=AsyncMock())
    await flow.start(message)
    message.answer.assert_awaited_once_with("⛔ فقط مدیران اجازه دسترسی دارند.")
    assert not flow._pending_broadcast

@pytest.mark.asyncio
async def test_preview_required():
    flow._pending_broadcast[10] = flow.PendingBroadcast(10, stage="content")
    callback = NS(from_user=NS(id=10), message=NS(chat=NS(id=10)), data="bcast:send", answer=AsyncMock())
    bot = NS(copy_message=AsyncMock())
    await flow.control(callback, bot)
    bot.copy_message.assert_not_awaited()
    assert flow.pending(10).stage == "content"

@pytest.mark.asyncio
async def test_cancel_clears():
    flow._pending_broadcast[10] = flow.PendingBroadcast(10)
    callback = NS(from_user=NS(id=10), message=NS(chat=NS(id=10), answer=AsyncMock()), data="bcast:cancel", answer=AsyncMock())
    await flow.control(callback, NS())
    assert not flow._pending_broadcast

def test_expiry():
    flow._pending_broadcast[10] = flow.PendingBroadcast(10, touched=-1000)
    assert flow.pending(10) is None

@pytest.mark.asyncio
async def test_apples_logs_and_nonnegative(seeded):
    await actions.mutate(seeded, 10, 20, 'apples', 'add', 15)
    await seeded.commit()
    log = await seeded.scalar(select(AdminLog).where(AdminLog.action == 'user_edit'))
    assert json.loads(log.details)['old_value'] == 0
    assert json.loads(log.details)['new_value'] == 15
    with pytest.raises(ValueError):
        await actions.mutate(seeded, 10, 20, 'apples', 'sub', 16, confirmed=True)

@pytest.mark.asyncio
async def test_ban_unban(seeded):
    with pytest.raises(ValueError):
        await actions.mutate(seeded, 10, 20, 'ban', 'set', True)
    await actions.mutate(seeded, 10, 20, 'ban', 'set', True, confirmed=True)
    user = await seeded.get(User, 20)
    assert user.is_banned and user.banned_by == 10 and user.banned_at
    await actions.mutate(seeded, 10, 20, 'ban', 'set', False, confirmed=True)
    assert not user.is_banned and user.banned_by is None and user.banned_at is None and user.ban_reason is None

@pytest.mark.asyncio
async def test_user_rejects_nonadmin(seeded):
    with pytest.raises(ValueError, match='مدیران'):
        await actions.mutate(seeded, 99, 20, 'apples', 'add', 1)

@pytest.mark.asyncio
async def test_other_admin_needs_second_confirm(seeded):
    with pytest.raises(ValueError, match='تأیید دوم'):
        await actions.mutate(seeded, 10, 11, 'apples', 'add', 1)

@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['mine', 'claim'])
async def test_cooldown_reset_fails_closed(seeded, field):
    with pytest.raises(ValueError, match='تاریخچه'):
        await actions.mutate(seeded, 10, 20, field, 'set', True, confirmed=True)

@pytest.mark.asyncio
async def test_dashboard_digits_and_refresh(seeded):
    before, _ = await users.dashboard(seeded, 20)
    await actions.mutate(seeded, 10, 20, 'apples', 'add', 123)
    await seeded.flush()
    after, keyboard = await users.dashboard(seeded, 20)
    assert before != after
    assert '۱۲۳' in after
    assert not any(c in after for c in '0123456789')
    assert keyboard.inline_keyboard[-1][0].callback_data == 'uadm:refresh:20'

@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['apple_gold', 'apple_diamond', 'apple_obsidian', 'magic_powder', 'seedling', 'xp', 'level'])
async def test_supported_fields(seeded, field):
    await actions.mutate(seeded, 10, 20, field, 'add', 2)
    _, values, _ = await actions.snapshot(seeded, 20)
    assert values[field] == (3 if field == 'level' else 2)

@pytest.mark.asyncio
async def test_card_add_main_remove(seeded):
    from sib.database.models import Character
    character = Character(code="A1", name_fa="آزمایش", is_active=True)
    seeded.add(character)
    await seeded.flush()
    await actions.mutate(seeded, 10, 20, 'card', 'add', 'A1')
    await actions.mutate(seeded, 10, 20, 'card', 'set', 'A1')
    _, _, cards = await actions.snapshot(seeded, 20)
    assert len(cards) == 1 and cards[0].is_favorite
    await actions.mutate(seeded, 10, 20, 'card', 'sub', 'A1', confirmed=True)
    _, _, cards = await actions.snapshot(seeded, 20)
    assert cards == []

@pytest.mark.asyncio
async def test_user_handler_rejects_nonadmin():
    callback = NS(from_user=NS(id=99), answer=AsyncMock())
    await users.control(callback, NS())
    callback.answer.assert_awaited_once_with("⛔ فقط مدیران اجازه دسترسی دارند.")

@pytest.mark.asyncio
async def test_preview_copies_before_state_ready():
    flow._pending_broadcast[10] = flow.PendingBroadcast(10, stage="content")
    bot = NS(copy_message=AsyncMock())
    message = NS(from_user=NS(id=10), chat=NS(id=10), message_id=55,
                 content_type="photo", answer=AsyncMock())
    await flow.receive(message, bot)
    assert flow.pending(10).stage == 'preview'
    assert flow.pending(10).source == 55
    assert bot.copy_message.await_count == 1

@pytest.mark.asyncio
async def test_failed_preview_cannot_send():
    flow._pending_broadcast[10] = flow.PendingBroadcast(10, stage="content")
    bot = NS(copy_message=AsyncMock(side_effect=RuntimeError("unsupported")))
    message = NS(from_user=NS(id=10), chat=NS(id=10), message_id=55,
                 content_type="text", answer=AsyncMock())
    await flow.receive(message, bot)
    assert flow.pending(10).stage == 'content'
    assert flow.pending(10).source is None

@pytest.mark.asyncio
async def test_confirm_refreshes_original_panel(seeded):
    await seeded.commit()
    edit = users.Edit(chat_id=10, panel_id=100, value=5)
    users._pending_amounts[(10, 20, 'apples', 'add')] = edit
    bot = NS(edit_message_text=AsyncMock())
    callback = NS(from_user=NS(id=10), data=f'uadm:confirm:{edit.token}',
                  answer=AsyncMock(), message=NS(chat=NS(id=10), edit_text=AsyncMock()))
    await users.control(callback, bot)
    assert not users._pending_amounts
    assert bot.edit_message_text.call_args.kwargs['message_id'] == 100
    assert '۵' in bot.edit_message_text.call_args.kwargs['text']
    # Confirmation is consumed and cannot apply twice.
    bot.edit_message_text.reset_mock()
    await users.control(callback, bot)
    bot.edit_message_text.assert_not_awaited()

@pytest.mark.asyncio
async def test_second_flood_wait_is_not_retried(seeded, monkeypatch):
    monkeypatch.setattr(svc.asyncio, 'sleep', AsyncMock())
    exc = TelegramRetryAfter(method=CopyMessage(chat_id=-100, from_chat_id=10, message_id=1), message='wait', retry_after=1)
    bot = NS(copy_message=AsyncMock(side_effect=exc))
    result = await svc.broadcast(bot, seeded, 10, 10, 1, -100)
    assert bot.copy_message.await_count == 2
    assert len(result.failures) == 1

@pytest.mark.asyncio
async def test_banned_user_cannot_dispatch(seeded):
    from sib.middleware.user_ban import UserBanMiddleware
    await actions.mutate(seeded, 10, 20, 'ban', 'set', True, confirmed=True)
    await seeded.commit()
    handler = AsyncMock()
    event = NS(from_user=NS(id=20), answer=AsyncMock())
    await UserBanMiddleware()(handler, event, {})
    handler.assert_not_awaited()
    await actions.mutate(seeded, 10, 20, 'ban', 'set', False, confirmed=True)
    await seeded.commit()
    await UserBanMiddleware()(handler, event, {})
    handler.assert_awaited_once()
