"""Self-driving Apple Duel scene-player behavior."""

import asyncio
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TelegramUser
from sib.database.models import AppleBattle, AppleDuel
from sib.services.apple_duel import AppleDuelService as Service
from sib.services.apple_duel import now_utc
from sib.services.apple_duel_presentation import AppleDuelPresentation as Presentation
from sib.services.apple_duel_presentation import scene_text
from sib.services.player_cards import PlayerCardService
from sib.tests.test_apple_battle import _card
from sqlalchemy import update


async def create_test_user(session, user_id):
    from sib.database.models import User

    user = await session.get(User, user_id)
    if user:
        return user
    user = User(
        id=user_id,
        first_name="مریم" if user_id == 701 else "علی",
        is_bot=False,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


def telegram_message():
    target_user = TelegramUser(id=702, first_name="علی", is_bot=False)
    target = Message(
        message_id=1,
        date=now_utc(),
        chat=Chat(id=-701, type="supergroup"),
        from_user=target_user,
    )
    return Message(
        message_id=2,
        date=now_utc(),
        chat=target.chat,
        from_user=TelegramUser(id=701, first_name="مریم", is_bot=False),
        text="دوئل سیبی",
        reply_to_message=target,
    )


async def setup_duel(session):
    a = await create_test_user(session, 701)
    b = await create_test_user(session, 702)
    ca = await _card(session, a, "duel_a")
    cb = await _card(session, b, "duel_b")
    cb.level = 3
    await PlayerCardService.set_favorite(session, a.id, ca.id)
    await PlayerCardService.set_favorite(session, b.id, cb.id)
    duel = await Service.create(session, -701, a.id, b.id)
    duel.message_id = 90
    await session.commit()
    return duel


async def accept(session, duel):
    return await Service.act(
        session, duel.id, duel.recipient_id, duel.group_id, "accept", message_id=90
    )


async def _play_bot():
    return SimpleNamespace(
        send_photo=AsyncMock(),
        send_message=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


async def _capture_play_sleeps(monkeypatch):
    import asyncio
    from sib.services import apple_duel_presentation as presentation

    delays = []
    original_sleep = asyncio.sleep

    async def capture(delay):
        delays.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(presentation.asyncio, "sleep", capture)
    return delays


async def test_play_sends_all_scenes_in_order(db_session, monkeypatch):
    from sib.services import apple_duel_presentation as presentation

    duel = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    delays = await _capture_play_sleeps(monkeypatch)
    bot = await _play_bot()
    monkeypatch.setattr(Presentation, "_photo", AsyncMock(return_value="photo"))

    await Presentation.play(bot, duel.id)

    battle = await db_session.get(AppleBattle, duel.battle_id)
    assert [call.kwargs["caption"] for call in bot.send_photo.await_args_list] == [
        scene_text(duel, battle, index) for index in range(6)
    ]
    assert delays == [presentation.DUEL_SCENE_DELAY_SECONDS] * 5
    assert all(delay <= 1.6 for delay in delays)
    await db_session.refresh(duel)
    assert duel.cursor == 6 and duel.status == "COMPLETED"
    assert duel.terminal_presented


async def test_play_stops_at_terminal_state(db_session, monkeypatch):
    duel = await setup_duel(db_session)
    await accept(db_session, duel)
    duel.status = "COMPLETED"
    duel.cursor = 6
    duel.terminal_presented = True
    await db_session.commit()
    tick = AsyncMock()
    monkeypatch.setattr(Presentation, "tick", tick)

    await Presentation.play(await _play_bot(), duel.id)

    tick.assert_not_awaited()


async def test_play_is_serialized_per_duel(db_session, monkeypatch):
    from sib.services import apple_duel_presentation as presentation

    duel = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    await _capture_play_sleeps(monkeypatch)
    bot = await _play_bot()
    monkeypatch.setattr(Presentation, "_photo", AsyncMock(return_value="photo"))
    original_tick = Presentation.tick
    calls = []

    async def counted_tick(cls, bot_arg, duel_id):
        calls.append(duel_id)
        await original_tick(bot_arg, duel_id)

    monkeypatch.setattr(Presentation, "tick", classmethod(counted_tick))
    import asyncio

    await asyncio.gather(
        Presentation.play(bot, duel.id), Presentation.play(bot, duel.id)
    )

    assert calls == [duel.id] * 6
    assert bot.send_photo.await_count == 6


async def test_play_exits_on_scene_send_failure(db_session, monkeypatch):
    from sib.services import apple_duel_presentation as presentation

    duel = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    bot = await _play_bot()
    bot.send_photo.side_effect = RuntimeError("photo transport down")
    bot.send_message.side_effect = RuntimeError("message transport down")
    monkeypatch.setattr(Presentation, "_photo", AsyncMock(return_value="photo"))
    delays = await _capture_play_sleeps(monkeypatch)

    await Presentation.play(bot, duel.id)

    await db_session.rollback()
    await db_session.refresh(duel)
    assert duel.cursor == 0
    assert duel.status == "ACCEPTED"
    retry_at = duel.next_scene_at
    if retry_at.tzinfo is None:
        from datetime import timezone

        retry_at = retry_at.replace(tzinfo=timezone.utc)
    assert retry_at > now_utc()
    assert delays == []


async def test_handler_uses_play_not_direct_tick(monkeypatch):
    from sib.handlers import apple_duel as handler_module
    from sib.services import apple_duel_presentation as presentation

    duel_id = "a" * 32
    callback = CallbackQuery(
        id="callback-1",
        from_user=TelegramUser(id=702, first_name="علی", is_bot=False),
        chat_instance="chat-instance",
        message=telegram_message().model_copy(update={"message_id": 90}),
        data=f"ad:accept:{duel_id}",
    )
    bot = SimpleNamespace()
    monkeypatch.setattr(
        handler_module.AppleDuelService,
        "act",
        AsyncMock(return_value=SimpleNamespace(status="ACCEPTED")),
    )
    monkeypatch.setattr(handler_module, "safe_answer_callback", AsyncMock())
    scheduled = []

    def capture_schedule(bot_arg, id_arg):
        scheduled.append((bot_arg, id_arg))

    monkeypatch.setattr(presentation, "schedule_play", capture_schedule)
    tick = AsyncMock()
    monkeypatch.setattr(Presentation, "tick", tick)

    await handler_module.callback_duel(callback, bot)

    assert scheduled == [(bot, duel_id)]
    tick.assert_not_awaited()


async def test_recovery_uses_play_not_direct_tick(db_session, monkeypatch):
    from sib.services import apple_duel_presentation as presentation

    duel = await setup_duel(db_session)
    await accept(db_session, duel)
    duel.next_scene_at = now_utc() - timedelta(seconds=1)
    await db_session.commit()
    scheduled = []
    monkeypatch.setattr(
        presentation, "schedule_play", lambda bot, duel_id: scheduled.append(duel_id)
    )
    tick = AsyncMock()
    monkeypatch.setattr(Presentation, "tick", tick)

    await Presentation.recover_and_publish(SimpleNamespace())

    assert scheduled == [duel.id]
    tick.assert_not_awaited()


async def test_total_duel_time_under_15s_for_five_scenes(db_session, monkeypatch):
    from sib.services import apple_duel_presentation as presentation

    duel = await setup_duel(db_session)
    await accept(db_session, duel)
    # Five pending indexed scenes (1 through 5).
    duel.cursor = 1
    duel.next_scene_at = now_utc()
    await db_session.commit()
    delays = await _capture_play_sleeps(monkeypatch)
    bot = await _play_bot()
    monkeypatch.setattr(Presentation, "_photo", AsyncMock(return_value="photo"))

    await Presentation.play(bot, duel.id)

    assert bot.send_photo.await_count == 5
    assert sum(delays) <= 5 * presentation.DUEL_SCENE_DELAY_SECONDS
    assert sum(delays) == 4 * presentation.DUEL_SCENE_DELAY_SECONDS


async def test_schedule_play_keeps_task_reference_and_deduplicates(monkeypatch):
    import asyncio

    from sib.services import apple_duel_presentation as presentation

    duel_id = "f" * 32
    presentation._play_tasks.pop(duel_id, None)
    play = AsyncMock()
    monkeypatch.setattr(Presentation, "play", play)

    task = presentation.schedule_play(SimpleNamespace(), duel_id)
    assert presentation._play_tasks[duel_id] is task
    assert presentation.schedule_play(SimpleNamespace(), duel_id) is task
    await task
    assert play.await_count == 1
    await asyncio.sleep(0)
    assert duel_id not in presentation._play_tasks


# --- F1.6: the terminal result never leaks an internal identifier ----------

# A raw duel id (32 hex chars) or its dashed UUID spelling. Nothing like this
# belongs in a caption a player reads in the group.
_INTERNAL_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32}"
)


def _delivered_texts(bot):
    """Every string this (mocked) bot pushed to the group, in send order."""
    texts = [
        call.kwargs["caption"]
        for call in bot.send_photo.await_args_list
        if "caption" in call.kwargs
    ]
    texts += [
        call.args[1]
        for call in bot.send_message.await_args_list
        if len(call.args) > 1
    ]
    texts += [
        call.args[0]
        for call in bot.edit_message_text.await_args_list
        if call.args
    ]
    texts += [
        call.kwargs["text"]
        for call in bot.edit_message_text.await_args_list
        if "text" in call.kwargs
    ]
    return texts


async def test_duel_result_does_not_contain_duel_id(db_session, monkeypatch):
    """The «🏆 پایان دوئل سیبی» caption carries no raw internal duel id."""
    duel = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    battle = await db_session.get(AppleBattle, duel.battle_id)

    result_text = scene_text(duel, battle, 5)

    # The removed line, and the identifier it used to print.
    assert "شناسهٔ دوئل" not in result_text
    assert "شناسه" not in result_text
    assert duel.id not in result_text
    assert str(duel.id) not in result_text
    assert not _INTERNAL_ID.search(result_text)

    # No scene of the six-scene presentation exposes an internal identifier.
    for index in range(6):
        caption = scene_text(duel, battle, index)
        assert duel.id not in caption, index
        assert not _INTERNAL_ID.search(caption), index

    # The result still tells the group who won and what the reward was.
    assert result_text.startswith("🏆 پایان دوئل سیبی")
    assert "برنده:" in result_text
    assert "یک نهال به برنده داده شد" in result_text
    assert result_text.rstrip().endswith("بازنده نهالی از دست نمی‌دهد.")


async def test_delivered_duel_scenes_contain_no_duel_id(db_session, monkeypatch):
    """End to end: what actually reaches the group has no internal id."""
    duel = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    await _capture_play_sleeps(monkeypatch)
    bot = await _play_bot()
    monkeypatch.setattr(Presentation, "_photo", AsyncMock(return_value="photo"))

    await Presentation.play(bot, duel.id)

    # Six scene photos, plus the terminal edit of the invitation message.
    captions = [
        call.kwargs["caption"]
        for call in bot.send_photo.await_args_list
        if "caption" in call.kwargs
    ]
    assert len(captions) == 6

    delivered = _delivered_texts(bot)
    assert len(delivered) >= 6
    for text in delivered:
        assert duel.id not in text
        assert "شناسهٔ دوئل" not in text
        assert not _INTERNAL_ID.search(text)


async def test_terminal_edit_carries_no_duel_id(db_session, monkeypatch):
    """``publish_terminal`` edits the invitation into the result — also clean."""
    duel = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    battle = await db_session.get(AppleBattle, duel.battle_id)
    bot = await _play_bot()

    await Presentation.publish_terminal(bot, duel, battle)

    texts = _delivered_texts(bot)
    assert texts, "terminal result was not delivered"
    for text in texts:
        assert duel.id not in text
        assert "شناسهٔ دوئل" not in text
        assert not _INTERNAL_ID.search(text)
