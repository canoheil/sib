"""Focused Telegram gameplay and presentation tests for Invasions."""

import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser
from sqlalchemy import func, select

from sib.database.models import (
    Character,
    Faction,
    Group,
    Invasion,
    InvasionContribution,
    InvasionDefenseAction,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
    InvasionWave,
    PlayerCard,
    ReleaseStatus,
    User,
)
from sib.handlers.invasion import _brace_idempotency_key, handle_invasion_callback
from sib.keyboards.invasion import (
    get_invasion_keyboard,
    invasion_callback_data,
    parse_invasion_callback,
)
from sib.services.characters import CharacterService
from sib.services.player_cards import PlayerCardService
from sib.services.invasion import (
    BRACE_BASE_CONTRIBUTION,
    InvasionAlreadyResolvedError,
    InvasionDefenseActionAlreadyUsedError,
    InvasionDefenseActionCode,
    InvasionNotParticipantError,
    InvasionService,
    InvasionStaleWaveError,
)
from sib.tests.conftest import requires_row_locks
from sib.services.invasion_telegram import (
    InvasionScreenSnapshot,
    InvasionTelegramService,
    render_final_notice,
    render_invasion_status,
)
from sib.services.maintenance.service import MaintenanceService


UTC = timezone.utc
NOW = datetime.now(UTC).replace(microsecond=0)
_GROUP_QUERY_IDS = itertools.count(1)
GROUP_ID = -100700
MESSAGE_ID = 9001


async def _make_group(session, group_id: int = GROUP_ID) -> Group:
    existing = await session.get(Group, group_id)
    if existing is not None:
        return existing
    group = Group(id=group_id, title=f"Invasion UX group {group_id}", is_active=True)
    session.add(group)
    await session.flush()
    return group


async def _make_user(session, user_id: int) -> User:
    while await session.get(User, user_id) is not None:
        user_id += 1
    user = User(
        id=user_id,
        first_name=f"مدافع {user_id}",
        language_code="fa",
        is_bot=False,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def _make_event(
    session,
    *,
    group_id: int = GROUP_ID,
    status: str = InvasionStatus.ACTIVE,
    announcement_message_id: int | None = MESSAGE_ID,
    target_region_name: str | None = "دره ماه",
    wave_count: int = 2,
) -> Invasion:
    await _make_group(session, group_id)
    faction = await session.scalar(
        select(Faction).where(Faction.code == "moon_descendants")
    )
    if faction is None:
        session.add(
            Faction(
                code="moon_descendants",
                name_fa="فرزندان ماه",
                name_en="Moon Descendants",
                symbol="🌙",
                lore_fa="",
                display_order=3,
                is_active=True,
            )
        )
        await session.flush()

    scheduled_at = NOW - timedelta(minutes=1)
    invasion = await InvasionService.create(
        session,
        group_id=group_id,
        occurrence_key=f"telegram-test:{group_id}:{status}:{wave_count}",
        scheduled_at=scheduled_at,
        enemy_faction_code="moon_descendants",
        enemy_power=180,
        event_config={"schema_version": 1},
        event_spec={"event_type": "moon_descendants"},
        wave_compositions=[
            {"wave_sequence": sequence, "enemy_power": 90}
            for sequence in range(1, wave_count + 1)
        ],
        target_region_code=("moonfall_valley" if target_region_name else None),
        target_region_name_snapshot=target_region_name,
    )
    if status == InvasionStatus.ACTIVE:
        await InvasionService.start(
            session,
            invasion.id,
            now=NOW,
            deadline_at=NOW + timedelta(minutes=10),
        )
    elif status == InvasionStatus.FINISHED:
        await InvasionService.start(
            session,
            invasion.id,
            now=NOW,
            deadline_at=NOW + timedelta(seconds=1),
        )
        await InvasionService.advance_wave(
            session,
            invasion.id,
            1,
            now=NOW + timedelta(seconds=2),
        )
        await InvasionService.advance_wave(
            session,
            invasion.id,
            2,
            now=NOW + timedelta(seconds=3),
            deadline_at=NOW + timedelta(seconds=4),
        )
        await InvasionService.advance_wave(
            session,
            invasion.id,
            2,
            now=NOW + timedelta(seconds=5),
        )
        await InvasionService.resolve(
            session,
            invasion.id,
            now=NOW + timedelta(seconds=5),
        )
    await session.commit()

    if announcement_message_id is not None:
        saved = await InvasionService.set_announcement_message_id(
            session,
            invasion.id,
            announcement_message_id,
            expected_group_id=group_id,
            expected_message_id=None,
        )
        assert saved
        await session.commit()
    await session.refresh(invasion)
    return invasion


def _bot(*, send_message_id: int = 9100) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=send_message_id)
    )
    bot.send_photo = AsyncMock(
        return_value=SimpleNamespace(message_id=send_message_id)
    )
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_media = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    bot.pin_chat_message = AsyncMock(return_value=True)
    bot.unpin_chat_message = AsyncMock(return_value=True)
    return bot


def _callback(
    user_id: int,
    data: str,
    *,
    group_id: int = GROUP_ID,
    message_id: int = MESSAGE_ID,
    chat_type: str = "supergroup",
    callback_id: str | None = None,
) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.id = callback_id or f"callback-query-{next(_GROUP_QUERY_IDS)}"
    callback.data = data
    callback.from_user = TelegramUser(
        id=user_id,
        is_bot=False,
        first_name=f"مدافع {user_id}",
        language_code="fa",
    )
    callback.message = MagicMock(spec=Message)
    callback.message.chat = TelegramChat(
        id=group_id,
        type=chat_type,
        title="گروه دفاع",
    )
    callback.message.message_id = message_id
    callback.message.text = "Invasion"
    callback.answer = AsyncMock()
    return callback


def _sent_text(bot: MagicMock, index: int = -1) -> str:
    call = bot.send_message.call_args_list[index]
    return call.args[1]


def _all_buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def test_invasion_callback_contract_is_exact_and_within_bot_api_limit():
    payload = invasion_callback_data("join", 12345)
    assert payload == "invasion:join:12345"
    assert len(payload.encode("utf-8")) <= 64
    assert parse_invasion_callback(payload) == ("join", 12345)

    brace_payload = invasion_callback_data(
        "brace",
        12345,
        wave_sequence=2,
    )
    assert brace_payload == "invasion:brace:12345:2"
    assert parse_invasion_callback(brace_payload) == ("brace", 12345, 2)
    assert len(brace_payload.encode("utf-8")) <= 64

    for invalid in (
        None,
        "",
        "invasion:join",
        "invasion:join:0",
        "invasion:join:-1",
        "invasion:join:+1",
        "invasion:join:01",
        "invasion:join:not-an-id",
        "invasion:other:1",
        "invasion:join:1:extra",
        "invasion:join:9223372036854775808",
        "invasion:brace:1",
        "invasion:brace:1:0",
        "invasion:brace:1:-1",
        "invasion:brace:1:01",
        "invasion:brace:1:2147483648",
        "invasion:brace:1:2:999999",
    ):
        assert parse_invasion_callback(invalid) is None


def test_invasion_keyboard_exposes_defense_control_only_on_an_actionable_wave():
    active = get_invasion_keyboard(
        42,
        InvasionStatus.ACTIVE,
        current_wave_sequence=2,
        wave_actionable=True,
    )
    active_buttons = _all_buttons(active)
    assert [button.callback_data for button in active_buttons] == [
        "invasion:join:42",
        "invasion:brace:42:2",
        "invasion:status:42",
    ]

    participant_used = get_invasion_keyboard(
        42,
        InvasionStatus.ACTIVE,
        current_wave_sequence=2,
        wave_actionable=True,
        viewer_is_participant=True,
    )
    assert [button.callback_data for button in _all_buttons(participant_used)] == [
        "invasion:brace:42:2",
        "invasion:status:42",
    ]
    assert _all_buttons(participant_used)[0].text == "🛡 Brace"

    nonparticipant = get_invasion_keyboard(
        42,
        InvasionStatus.ACTIVE,
        current_wave_sequence=2,
        wave_actionable=True,
        viewer_is_participant=False,
    )
    assert [button.callback_data for button in _all_buttons(nonparticipant)] == [
        "invasion:join:42",
        "invasion:brace:42:2",
        "invasion:status:42",
    ]

    expired = get_invasion_keyboard(
        42,
        InvasionStatus.ACTIVE,
        current_wave_sequence=2,
        wave_actionable=False,
    )
    assert [button.callback_data for button in _all_buttons(expired)] == [
        "invasion:join:42",
        "invasion:status:42",
    ]
    finished = get_invasion_keyboard(
        42,
        InvasionStatus.FINISHED,
        current_wave_sequence=None,
    )
    assert [button.callback_data for button in _all_buttons(finished)] == [
        "invasion:status:42"
    ]


@pytest.mark.asyncio
async def test_announcement_uses_persisted_snapshot_and_saves_message_id(db_session):
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70001)
    participant = await InvasionService.join(
        db_session,
        invasion_id,
        user.id,
        expected_group_id=GROUP_ID,
        now=NOW,
    )
    wave = await InvasionService.get_current_wave(db_session, invasion_id)
    await InvasionService.record_contribution(
        db_session,
        invasion_id,
        participant.id,
        wave.wave_sequence,
        37,
        idempotency_key="telegram-announcement-contribution",
        now=NOW + timedelta(minutes=1),
    )
    await db_session.commit()

    bot = _bot(send_message_id=9317)
    published = await InvasionTelegramService.publish_maintenance_updates(bot, [])

    assert published == 1
    assert bot.send_photo.await_count == 1
    assert bot.send_message.await_count == 0
    photo = bot.send_photo.call_args.args[1]
    assert str(photo.path).endswith("assets/invasion/moon_descendant_invasion.jpg")
    announcement = bot.send_photo.call_args.kwargs["caption"]
    assert "تهاجم آغاز شده" in announcement
    assert "فرزندان ماه" in announcement
    assert "دره ماه" in announcement
    assert "۱۸۰" in announcement
    assert "موج ۱ از ۲" in announcement
    assert "۳۷" in announcement
    assert "۱" in announcement
    buttons = _all_buttons(bot.send_photo.call_args.kwargs["reply_markup"])
    callbacks = {button.callback_data for button in buttons}
    assert f"invasion:join:{invasion_id}" in callbacks
    assert f"invasion:brace:{invasion_id}:1" in callbacks

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9317
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.current_wave_sequence == 1


@pytest.mark.asyncio
async def test_telegram_send_failure_leaves_invasion_valid_and_pending_send_retries(
    db_session,
    monkeypatch,
):
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=RuntimeError("telegram unavailable"))
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram unavailable"))

    import sib.services.invasion_telegram as invasion_telegram

    clock = [100.0]
    monkeypatch.setattr(invasion_telegram.time, "monotonic", lambda: clock[0])
    invasion_telegram._ANNOUNCEMENT_RETRY_AFTER.pop(invasion_id, None)
    failed = await InvasionTelegramService.publish_maintenance_updates(bot, [])
    assert failed == 0

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.started_at is not None
    assert persisted.announcement_message_id is None

    bot.send_photo = AsyncMock(
        return_value=SimpleNamespace(message_id=9320)
    )
    clock[0] += invasion_telegram.ANNOUNCEMENT_RETRY_SECONDS + 1
    retried = await InvasionTelegramService.publish_maintenance_updates(bot, [])
    assert retried == 1
    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.announcement_message_id == 9320


@pytest.mark.asyncio
async def test_announcement_photo_failure_falls_back_to_text_without_domain_changes(
    db_session,
):
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    bot = _bot(send_message_id=9330)
    bot.send_photo = AsyncMock(side_effect=RuntimeError("photo upload unavailable"))

    published = await InvasionTelegramService.publish_maintenance_updates(bot, [])

    assert published == 1
    assert bot.send_photo.await_count == 1
    assert bot.send_message.await_count == 1
    assert "تهاجم آغاز شده" in _sent_text(bot)
    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted is not None
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.announcement_message_id == 9330


@pytest.mark.asyncio
async def test_valid_join_uses_service_main_card_snapshot_and_duplicate_is_idempotent(
    db_session,
):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70002)
    user_id = int(user.id)
    character = await CharacterService.create(
        db_session,
        code="invasion_join_card",
        name_fa="نگهبان ماه",
        power=20,
        speed=20,
        intelligence=20,
        release_status=ReleaseStatus.RELEASED,
    )
    card = PlayerCard(
        user_id=user.id,
        character_id=character.id,
        level=2,
        is_favorite=True,
    )
    db_session.add(card)
    await db_session.commit()
    card_id = int(card.id)
    character_code = character.code

    bot = _bot()
    callback = _callback(user_id, invasion_callback_data("join", invasion_id))
    await handle_invasion_callback(callback, bot)
    first_response = _sent_text(bot)
    assert callback.answer.await_count == 1
    assert "به یورش پیوستی" in first_response
    assert "نگهبان ماه" in first_response
    assert "سطح: ۲" in first_response
    assert "قدرت: ۳۰" in first_response
    assert "سرعت: ۳۰" in first_response
    assert "هوش: ۳۰" in first_response

    duplicate = _callback(user_id, invasion_callback_data("join", invasion_id))
    await handle_invasion_callback(duplicate, bot)
    second_response = _sent_text(bot)
    assert "به یورش پیوستی" in second_response
    assert bot.send_message.await_count == 2
    assert await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user_id,
        )
    ) == 1

    db_session.expire_all()
    persisted_participant = await db_session.scalar(
        select(InvasionParticipant).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user_id,
        )
    )
    assert persisted_participant.main_card_id_snapshot == card_id
    assert persisted_participant.character_code_snapshot == character_code


@pytest.mark.asyncio
async def test_join_without_main_card_is_explicit_and_does_not_choose_a_card(db_session):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70003)
    user_id = int(user.id)
    await db_session.commit()

    bot = _bot()
    callback = _callback(user_id, invasion_callback_data("join", invasion_id))
    await handle_invasion_callback(callback, bot)

    assert "بدون کارت اصلی" in _sent_text(bot)
    participant = await db_session.scalar(
        select(InvasionParticipant).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user_id,
        )
    )
    assert participant is not None
    assert participant.main_card_id_snapshot is None
    assert await db_session.scalar(
        select(func.count(PlayerCard.id)).where(PlayerCard.user_id == user_id)
    ) == 0


@pytest.mark.asyncio
async def test_wrong_group_stale_message_and_unknown_event_are_rejected(db_session):
    invasion = await _make_event(db_session)
    wrong_group_id = GROUP_ID - 1
    await _make_group(db_session, wrong_group_id)
    bot = _bot()

    wrong_group = _callback(
        70004,
        invasion_callback_data("join", invasion.id),
        group_id=wrong_group_id,
        message_id=MESSAGE_ID,
    )
    await handle_invasion_callback(wrong_group, bot)
    assert "در این گروه معتبر نیست" in _sent_text(bot)

    stale = _callback(
        70005,
        invasion_callback_data("join", invasion.id),
        message_id=MESSAGE_ID - 1,
    )
    await handle_invasion_callback(stale, bot)
    assert "اعلان قدیمی" in _sent_text(bot)

    unknown = _callback(70006, "invasion:join:999999")
    await handle_invasion_callback(unknown, bot)
    assert "دیگر در دسترس نیست" in _sent_text(bot)

    assert await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id == invasion.id
        )
    ) == 0


@pytest.mark.asyncio
async def test_malformed_and_non_group_callbacks_are_acknowledged_and_rejected(db_session):
    bot = _bot()
    malformed = _callback(70007, "invasion:join:not-an-id")
    await handle_invasion_callback(malformed, bot)
    malformed.answer.assert_awaited_once()
    assert malformed.answer.call_args.kwargs.get("show_alert") is True
    assert bot.send_message.await_count == 0

    private = _callback(
        70008,
        "invasion:join:1",
        group_id=70008,
        chat_type="private",
    )
    await handle_invasion_callback(private, bot)
    private.answer.assert_awaited_once()
    assert private.answer.call_args.kwargs.get("show_alert") is True
    assert bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_scheduled_and_finished_events_reject_join_without_mutating_participants(
    db_session,
):
    scheduled = await _make_event(
        db_session,
        group_id=GROUP_ID,
        status=InvasionStatus.SCHEDULED,
        announcement_message_id=MESSAGE_ID,
    )
    bot = _bot()
    scheduled_callback = _callback(
        70009,
        invasion_callback_data("join", scheduled.id),
    )
    await handle_invasion_callback(scheduled_callback, bot)
    assert "هنوز آغاز نشده" in _sent_text(bot)

    finished_group_id = GROUP_ID - 2
    finished = await _make_event(
        db_session,
        group_id=finished_group_id,
        status=InvasionStatus.FINISHED,
        announcement_message_id=MESSAGE_ID + 1,
        wave_count=2,
    )
    finished_callback = _callback(
        70010,
        invasion_callback_data("join", finished.id),
        group_id=finished_group_id,
        message_id=MESSAGE_ID + 1,
    )
    await handle_invasion_callback(finished_callback, bot)
    assert "بسته شده" in _sent_text(bot)
    assert await db_session.scalar(
        select(func.count(InvasionParticipant.id)).where(
            InvasionParticipant.invasion_id.in_([scheduled.id, finished.id])
        )
    ) == 0


@pytest.mark.asyncio
async def test_status_callback_edits_from_persisted_state(db_session):
    invasion = await _make_event(db_session)
    user = await _make_user(db_session, 70011)
    participant = await InvasionService.join(
        db_session,
        invasion.id,
        user.id,
        expected_group_id=GROUP_ID,
        now=NOW,
    )
    wave = await InvasionService.get_current_wave(db_session, invasion.id)
    await InvasionService.record_contribution(
        db_session,
        invasion.id,
        participant.id,
        wave.wave_sequence,
        56,
        idempotency_key="telegram-status-contribution",
        now=NOW + timedelta(minutes=1),
    )
    await db_session.commit()

    bot = _bot()
    callback = _callback(70012, invasion_callback_data("status", invasion.id))
    await handle_invasion_callback(callback, bot)

    callback.answer.assert_awaited_once()
    bot.edit_message_media.assert_awaited_once()
    text = bot.edit_message_media.call_args.kwargs["media"].caption
    assert "تهاجم آغاز شده" in text
    assert "فرزندان ماه" in text
    assert "دره ماه" in text
    assert "موج ۱ از ۲" in text
    assert "قدرت دشمن: ۱۸۰" in text
    assert "دفاع ثبت‌شده: ۵۶" in text
    assert "شرکت‌کنندگان: ۱" in text
    assert "زمان باقی‌مانده دفاع" in text


@pytest.mark.asyncio
async def test_brace_complete_flow_updates_status_and_contributions(db_session):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70100)
    user_id = int(user.id)
    await db_session.commit()
    bot = _bot()

    join_callback = _callback(
        user_id,
        invasion_callback_data("join", invasion_id),
    )
    await handle_invasion_callback(join_callback, bot)
    assert "به یورش پیوستی" in _sent_text(bot)

    status_callback = _callback(
        user_id,
        invasion_callback_data("status", invasion_id),
    )
    await handle_invasion_callback(status_callback, bot)
    status_text = bot.edit_message_media.call_args.kwargs["media"].caption
    assert "موج ۱ از ۲" in status_text
    assert "دفاع این موج: ۰" in status_text
    assert "سهم شما در یورش: ۰" in status_text
    assert "Brace در دسترس است" in status_text
    status_keyboard = bot.edit_message_media.call_args.kwargs["reply_markup"]
    assert f"invasion:brace:{invasion_id}:1" in {
        button.callback_data for button in _all_buttons(status_keyboard)
    }

    brace_callback = _callback(
        user_id,
        invasion_callback_data("brace", invasion_id, wave_sequence=1),
        callback_id="brace-complete-flow",
    )
    await handle_invasion_callback(brace_callback, bot)
    result_text = _sent_text(bot)
    assert "دفاع پذیرفته شد" in result_text
    assert "سهم این کنش: ۱" in result_text
    assert "سهم کل شما: ۱" in result_text
    assert "دفاع موج ۱: ۱" in result_text

    updated_status = bot.edit_message_media.call_args_list[-1].kwargs["media"].caption
    assert "دفاع این موج: ۱" in updated_status
    assert "سهم شما در یورش: ۱" in updated_status
    assert "Brace را در این موج انجام دادی" in updated_status
    updated_keyboard = bot.edit_message_media.call_args_list[-1].kwargs["reply_markup"]
    # Group inline keyboards are shared; keep the Wave button for other
    # participants while the screen gives this viewer their used-state feedback.
    assert f"invasion:brace:{invasion_id}:1" in {
        button.callback_data for button in _all_buttons(updated_keyboard)
    }

    db_session.expire_all()
    participant = await db_session.scalar(
        select(InvasionParticipant).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user_id,
        )
    )
    wave = await db_session.scalar(
        select(InvasionWave).where(
            InvasionWave.invasion_id == invasion_id,
            InvasionWave.wave_sequence == 1,
        )
    )
    assert participant.contribution_total == BRACE_BASE_CONTRIBUTION
    assert wave.contribution_total == BRACE_BASE_CONTRIBUTION
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 1


@pytest.mark.asyncio
async def test_brace_is_one_per_wave_and_next_wave_allows_another(db_session):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70101)
    user_id = int(user.id)
    await db_session.commit()
    bot = _bot()

    await handle_invasion_callback(
        _callback(user_id, invasion_callback_data("join", invasion_id)),
        bot,
    )
    first = _callback(
        user_id,
        invasion_callback_data("brace", invasion_id, wave_sequence=1),
        callback_id="brace-wave-one-first",
    )
    await handle_invasion_callback(first, bot)
    assert "دفاع پذیرفته شد" in _sent_text(bot)

    second = _callback(
        user_id,
        invasion_callback_data("brace", invasion_id, wave_sequence=1),
        callback_id="brace-wave-one-distinct",
    )
    await handle_invasion_callback(second, bot)
    assert "قبلاً انجام دادی" in _sent_text(bot)
    assert "Brace را در این موج انجام دادی" in bot.edit_message_media.call_args_list[-1].kwargs["media"].caption

    await db_session.commit()
    await InvasionService.finish_wave(
        db_session,
        invasion_id,
        1,
        now=NOW + timedelta(minutes=10),
    )
    await InvasionService.advance_wave(
        db_session,
        invasion_id,
        2,
        now=NOW + timedelta(minutes=10),
        deadline_at=NOW + timedelta(minutes=20),
    )
    await db_session.commit()

    third = _callback(
        user_id,
        invasion_callback_data("brace", invasion_id, wave_sequence=2),
        callback_id="brace-wave-two",
    )
    await handle_invasion_callback(third, bot)
    assert "دفاع پذیرفته شد" in _sent_text(bot)
    assert "دفاع موج ۲: ۱" in _sent_text(bot)
    assert "سهم کل شما: ۲" in _sent_text(bot)

    db_session.expire_all()
    participant = await db_session.scalar(
        select(InvasionParticipant).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user_id,
        )
    )
    assert participant.contribution_total == 2
    for sequence in (1, 2):
        wave = await db_session.scalar(
            select(InvasionWave).where(
                InvasionWave.invasion_id == invasion_id,
                InvasionWave.wave_sequence == sequence,
            )
        )
        assert wave.contribution_total == 1
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 2


@pytest.mark.asyncio
async def test_duplicate_brace_callback_uses_stable_telegram_query_key(db_session):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70102)
    user_id = int(user.id)
    await db_session.commit()
    bot = _bot()
    await handle_invasion_callback(
        _callback(user_id, invasion_callback_data("join", invasion_id)),
        bot,
    )

    callback_data = invasion_callback_data("brace", invasion_id, wave_sequence=1)
    first = _callback(
        user_id,
        callback_data,
        callback_id="telegram-retried-query-123",
    )
    duplicate_delivery = _callback(
        user_id,
        callback_data,
        callback_id="telegram-retried-query-123",
    )
    assert _brace_idempotency_key(first.id) == _brace_idempotency_key(
        duplicate_delivery.id
    )
    assert _brace_idempotency_key("fresh-press") != _brace_idempotency_key(
        "telegram-retried-query-123"
    )

    await handle_invasion_callback(first, bot)
    assert "دفاع پذیرفته شد" in _sent_text(bot)
    await handle_invasion_callback(duplicate_delivery, bot)
    assert "قبلاً ثبت شده است" in _sent_text(bot)

    db_session.expire_all()
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 1
    contribution = await db_session.scalar(
        select(InvasionContribution).where(
            InvasionContribution.invasion_id == invasion_id
        )
    )
    assert contribution.idempotency_key == _brace_idempotency_key(first.id)
    participant = await db_session.scalar(
        select(InvasionParticipant).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user_id,
        )
    )
    assert participant.contribution_total == BRACE_BASE_CONTRIBUTION


@pytest.mark.parametrize("stale_kind", ["previous", "future", "expired"])
@pytest.mark.asyncio
async def test_stale_wave_callbacks_do_not_contribute_and_refresh_current_status(
    db_session,
    stale_kind,
):
    invasion = await _make_event(db_session, wave_count=2)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70103)
    await InvasionService.join(
        db_session,
        invasion_id,
        user.id,
        expected_group_id=GROUP_ID,
        now=NOW,
    )
    await db_session.commit()

    if stale_kind == "previous":
        await InvasionService.finish_wave(
            db_session,
            invasion_id,
            1,
            now=NOW + timedelta(minutes=10),
        )
        await InvasionService.advance_wave(
            db_session,
            invasion_id,
            2,
            now=NOW + timedelta(minutes=10),
            deadline_at=NOW + timedelta(minutes=20),
        )
        callback_wave = 1
        expected_current_wave = 2
    elif stale_kind == "future":
        callback_wave = 2
        expected_current_wave = 1
    else:
        wave = await InvasionService.get_current_wave(db_session, invasion_id)
        wave.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        callback_wave = 1
        expected_current_wave = 1
    await db_session.commit()

    bot = _bot()
    callback = _callback(
        user.id,
        invasion_callback_data("brace", invasion_id, wave_sequence=callback_wave),
        callback_id=f"stale-wave-{stale_kind}",
    )
    await handle_invasion_callback(callback, bot)

    assert "موج دیگر فعال نیست" in _sent_text(bot)
    current_status_text = bot.edit_message_media.call_args_list[-1].kwargs["media"].caption
    expected_wave_fa = str(expected_current_wave).translate(
        str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    )
    assert f"موج {expected_wave_fa} از ۲" in current_status_text
    if stale_kind == "expired":
        assert "Brace در این موج در دسترس نیست" in current_status_text
        buttons = _all_buttons(
            bot.edit_message_media.call_args_list[-1].kwargs["reply_markup"]
        )
        assert not any(button.callback_data.startswith("invasion:brace:") for button in buttons)

    db_session.expire_all()
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 0


@pytest.mark.asyncio
async def test_finished_invasion_brace_callback_renders_final_result(db_session):
    invasion = await _make_event(db_session, wave_count=2)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70104)
    await InvasionService.join(
        db_session,
        invasion_id,
        user.id,
        expected_group_id=GROUP_ID,
        now=NOW,
    )
    await InvasionService.finish_wave(
        db_session,
        invasion_id,
        1,
        now=NOW + timedelta(minutes=10),
    )
    await InvasionService.advance_wave(
        db_session,
        invasion_id,
        2,
        now=NOW + timedelta(minutes=10),
        deadline_at=NOW + timedelta(minutes=20),
    )
    await InvasionService.finish_wave(
        db_session,
        invasion_id,
        2,
        now=NOW + timedelta(minutes=20),
    )
    await InvasionService.resolve(
        db_session,
        invasion_id,
        now=NOW + timedelta(minutes=20),
    )
    await db_session.commit()

    bot = _bot()
    callback = _callback(
        user.id,
        invasion_callback_data("brace", invasion_id, wave_sequence=2),
        callback_id="brace-after-finish",
    )
    await handle_invasion_callback(callback, bot)

    final_screen = bot.edit_message_media.call_args.kwargs["media"].caption
    assert "نتیجه نهایی تهاجم" in final_screen
    assert "شکست در دفاع" in final_screen
    buttons = _all_buttons(bot.edit_message_media.call_args.kwargs["reply_markup"])
    assert [button.callback_data for button in buttons] == [
        f"invasion:status:{invasion_id}"
    ]
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 0


@pytest.mark.asyncio
async def test_brace_rejects_forged_user_and_group_context_uses_callback_actor(
    db_session,
):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    participant_user = await _make_user(db_session, 70105)
    outsider = await _make_user(db_session, 70106)
    participant = await InvasionService.join(
        db_session,
        invasion_id,
        participant_user.id,
        expected_group_id=GROUP_ID,
        now=NOW,
    )
    await db_session.commit()
    bot = _bot()

    forged_user_payload = _callback(
        outsider.id,
        f"invasion:brace:{invasion_id}:1:{participant_user.id}",
    )
    await handle_invasion_callback(forged_user_payload, bot)
    forged_user_payload.answer.assert_awaited_once()
    assert forged_user_payload.answer.call_args.kwargs.get("show_alert") is True
    assert bot.send_message.await_count == 0

    wrong_group = _callback(
        participant_user.id,
        invasion_callback_data("brace", invasion_id, wave_sequence=1),
        group_id=GROUP_ID - 1,
    )
    await handle_invasion_callback(wrong_group, bot)
    assert "در این گروه معتبر نیست" in _sent_text(bot)

    nonparticipant = _callback(
        outsider.id,
        invasion_callback_data("brace", invasion_id, wave_sequence=1),
        callback_id="nonparticipant-brace",
    )
    await handle_invasion_callback(nonparticipant, bot)
    assert "اول به جمع مدافعان بپیوند" in _sent_text(bot)

    authenticated_participant = _callback(
        participant_user.id,
        invasion_callback_data("brace", invasion_id, wave_sequence=1),
        callback_id="authenticated-participant-brace",
    )
    await handle_invasion_callback(authenticated_participant, bot)
    assert "دفاع پذیرفته شد" in _sent_text(bot)
    action = await db_session.scalar(
        select(InvasionDefenseAction).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    )
    assert action.participant_id == participant.id


@pytest.mark.asyncio
async def test_brace_uses_persisted_main_card_snapshot_after_live_card_changes(
    db_session,
    monkeypatch,
):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70107)
    user_id = int(user.id)
    character = await CharacterService.create(
        db_session,
        code="telegram_brace_snapshot_card",
        name_fa="نگهبان موج",
        power=20,
        speed=20,
        intelligence=20,
        release_status=ReleaseStatus.RELEASED,
    )
    card = PlayerCard(
        user_id=user_id,
        character_id=character.id,
        level=2,
        is_favorite=True,
    )
    db_session.add(card)
    await db_session.commit()
    original_card_id = int(card.id)

    bot = _bot()
    await handle_invasion_callback(
        _callback(user_id, invasion_callback_data("join", invasion_id)),
        bot,
    )
    db_session.expire_all()
    participant = await db_session.scalar(
        select(InvasionParticipant).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user_id,
        )
    )
    assert participant.main_card_id_snapshot == original_card_id
    assert participant.power_snapshot == 30

    # Upgrade and deselect the joined card; a different live Main Card is now
    # selected. The action must still use the immutable participant snapshot.
    card.level = 9
    card.is_favorite = False
    replacement_character = await CharacterService.create(
        db_session,
        code="telegram_brace_replacement_card",
        name_fa="مدافع جایگزین",
        power=20,
        speed=20,
        intelligence=20,
        release_status=ReleaseStatus.RELEASED,
    )
    replacement_card = PlayerCard(
        user_id=user_id,
        character_id=replacement_character.id,
        level=1,
        is_favorite=True,
    )
    db_session.add(replacement_card)
    await db_session.commit()
    selected_card = await PlayerCardService.get_main_card(db_session, user_id)
    assert selected_card.id == replacement_card.id
    await db_session.commit()

    monkeypatch.setattr(
        PlayerCardService,
        "get_main_card",
        AsyncMock(side_effect=AssertionError("brace must not read the live Main Card")),
    )
    callback = _callback(
        user_id,
        invasion_callback_data("brace", invasion_id, wave_sequence=1),
        callback_id="brace-persisted-card-snapshot",
    )
    await handle_invasion_callback(callback, bot)

    assert "سهم این کنش: ۴" in _sent_text(bot)
    action = await db_session.scalar(
        select(InvasionDefenseAction).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    )
    assert action.power_snapshot_used == participant.power_snapshot == 30
    assert action.contribution_amount == 1 + 30 // 10 == 4


@pytest.mark.asyncio
async def test_brace_refresh_persists_replacement_announcement_id(db_session):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70109)
    user_id = int(user.id)
    await InvasionService.join(
        db_session,
        invasion_id,
        user_id,
        expected_group_id=GROUP_ID,
        now=NOW,
    )
    await db_session.commit()

    bot = _bot()
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=9401)
    )
    bot.send_photo = AsyncMock(
        return_value=SimpleNamespace(message_id=9555)
    )
    bot.edit_message_media = AsyncMock(
        side_effect=TelegramBadRequest(
            method=None,
            message="there is no media in the message to edit",
        )
    )
    callback = _callback(
        user_id,
        invasion_callback_data("brace", invasion_id, wave_sequence=1),
        callback_id="brace-replacement-message",
    )
    await handle_invasion_callback(callback, bot)

    assert bot.send_message.await_count == 1
    assert bot.send_photo.await_count == 1
    bot.delete_message.assert_awaited_with(GROUP_ID, MESSAGE_ID)
    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9555
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 1


@pytest.mark.asyncio
async def test_telegram_feedback_failure_does_not_rollback_brace(db_session):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70110)
    user_id = int(user.id)
    await InvasionService.join(
        db_session,
        invasion_id,
        user_id,
        expected_group_id=GROUP_ID,
        now=NOW,
    )
    await db_session.commit()

    bot = _bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
    bot.edit_message_text = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
    callback = _callback(
        user_id,
        invasion_callback_data("brace", invasion_id, wave_sequence=1),
        callback_id="brace-feedback-failure",
    )
    await handle_invasion_callback(callback, bot)

    db_session.expire_all()
    participant = await db_session.scalar(
        select(InvasionParticipant).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user_id,
        )
    )
    assert participant.contribution_total == BRACE_BASE_CONTRIBUTION
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 1


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_brace_callbacks_with_distinct_keys_accept_only_one_action(
    db_session,
):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70108)
    await InvasionService.join(
        db_session,
        invasion_id,
        user.id,
        expected_group_id=GROUP_ID,
        now=NOW,
    )
    await db_session.commit()

    bot = _bot()
    callbacks = [
        _callback(
            user.id,
            invasion_callback_data("brace", invasion_id, wave_sequence=1),
            callback_id=f"concurrent-brace-{index}",
        )
        for index in (1, 2)
    ]
    await asyncio.gather(
        *(handle_invasion_callback(callback, bot) for callback in callbacks)
    )

    texts = [call.args[1] for call in bot.send_message.call_args_list]
    assert sum("دفاع پذیرفته شد" in text for text in texts) == 1
    assert sum("قبلاً انجام دادی" in text for text in texts) == 1
    db_session.expire_all()
    assert await db_session.scalar(
        select(func.count(InvasionDefenseAction.id)).where(
            InvasionDefenseAction.invasion_id == invasion_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count(InvasionContribution.id)).where(
            InvasionContribution.invasion_id == invasion_id
        )
    ) == 1
    participant = await db_session.scalar(
        select(InvasionParticipant).where(
            InvasionParticipant.invasion_id == invasion_id,
            InvasionParticipant.user_id == user.id,
        )
    )
    assert participant.contribution_total == BRACE_BASE_CONTRIBUTION


@pytest.mark.asyncio
async def test_status_screen_replacement_persists_new_id_and_next_update_uses_it(
    db_session,
):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    bot = _bot(send_message_id=9555)
    bot.edit_message_media = AsyncMock(
        side_effect=TelegramBadRequest(
            method=None,
            message="there is no media in the message to edit",
        )
    )

    await InvasionTelegramService.publish_maintenance_updates(bot, [invasion_id])
    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9555
    bot.delete_message.assert_awaited_with(GROUP_ID, MESSAGE_ID)

    bot.edit_message_media = AsyncMock(return_value=True)
    await InvasionTelegramService.publish_maintenance_updates(bot, [invasion_id])
    assert bot.edit_message_media.await_args.kwargs["message_id"] == 9555
    assert bot.edit_message_media.await_args.kwargs["chat_id"] == GROUP_ID


@pytest.mark.asyncio
async def test_announcement_message_id_cas_rejects_stale_publisher(db_session):
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)

    # The first sender claims an empty ID. A concurrent sender's stale read of
    # NULL cannot overwrite that result.
    assert await InvasionService.set_announcement_message_id(
        db_session,
        invasion_id,
        9401,
        expected_group_id=GROUP_ID,
        expected_message_id=None,
    )
    await db_session.commit()
    assert not await InvasionService.set_announcement_message_id(
        db_session,
        invasion_id,
        9410,
        expected_group_id=GROUP_ID,
        expected_message_id=None,
    )

    # A later replacement wins only against the ID it actually observed. A
    # slow publisher and a publisher for another Group both lose their CAS.
    assert await InvasionService.set_announcement_message_id(
        db_session,
        invasion_id,
        9500,
        expected_group_id=GROUP_ID,
        expected_message_id=9401,
    )
    await db_session.commit()
    assert not await InvasionService.set_announcement_message_id(
        db_session,
        invasion_id,
        9600,
        expected_group_id=GROUP_ID,
        expected_message_id=9401,
    )
    assert not await InvasionService.set_announcement_message_id(
        db_session,
        invasion_id,
        9700,
        expected_group_id=GROUP_ID - 1,
        expected_message_id=9500,
    )
    await db_session.commit()

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted is not None
    assert persisted.announcement_message_id == 9500
    assert persisted.status == InvasionStatus.ACTIVE


@pytest.mark.asyncio
async def test_telegram_edit_and_fallback_failure_leave_domain_and_message_id_unchanged(
    db_session,
):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    bot = _bot()
    bot.edit_message_media = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="message can't be edited")
    )
    bot.send_photo = AsyncMock(side_effect=RuntimeError("telegram unavailable"))
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram unavailable"))

    result = await InvasionTelegramService.publish_maintenance_updates(
        bot,
        [invasion_id],
    )
    assert result == 0

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted is not None
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.announcement_message_id == MESSAGE_ID


@pytest.mark.asyncio
async def test_resolved_announcement_displays_persisted_final_outcome(db_session):
    invasion = await _make_event(
        db_session,
        status=InvasionStatus.FINISHED,
        wave_count=2,
    )
    invasion_id = int(invasion.id)
    bot = _bot()
    published = await InvasionTelegramService.publish_maintenance_updates(
        bot,
        [invasion_id],
    )

    assert published == 1
    final_text = bot.edit_message_media.call_args.kwargs["media"].caption
    assert "نتیجه نهایی تهاجم" in final_text
    assert "شکست در دفاع" in final_text
    assert "دفاع ثبت‌شده: ۰" in final_text
    assert "قدرت دشمن: ۱۸۰" in final_text
    assert "دره ماه" in final_text
    assert "موج‌ها: ۲ از ۲ تکمیل شد" in final_text

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.status == InvasionStatus.FINISHED
    assert persisted.outcome == InvasionOutcome.DEFEAT
    assert persisted.announcement_message_id == MESSAGE_ID


@pytest.mark.asyncio
async def test_existing_maintenance_loop_announces_after_event_commit(
    db_session, monkeypatch
):
    # Battles activate only inside a slot's window, so the loop's clock is
    # pinned to a moment inside the 08:00 UTC window instead of the wall clock.
    from functools import partial

    monkeypatch.setattr(
        MaintenanceService,
        "run_invasion_cycle",
        partial(
            MaintenanceService.run_invasion_cycle,
            now=datetime(2026, 10, 9, 8, 0, 30, tzinfo=timezone.utc),
        ),
    )
    group_id = GROUP_ID - 100
    db_session.add(
        Group(
            id=group_id,
            title="Maintenance announcement integration",
            is_active=True,
        )
    )
    await db_session.commit()

    from sib.database.connection import get_session

    observed = {"committed_before_send": False}

    async def send_photo(chat_id, photo, caption=None, **kwargs):
        async with get_session() as session:
            invasion = await session.scalar(
                select(Invasion).where(Invasion.group_id == group_id)
            )
            observed["committed_before_send"] = (
                invasion is not None and invasion.announcement_message_id is None
            )
        return SimpleNamespace(message_id=9688)

    bot = _bot(send_message_id=9688)
    bot.send_photo = AsyncMock(side_effect=send_photo)
    previous_bot = MaintenanceService._bot
    MaintenanceService._bot = bot
    try:
        await MaintenanceService.run_once()
    finally:
        MaintenanceService._bot = previous_bot

    assert observed["committed_before_send"] is True
    bot.send_photo.assert_awaited_once()
    sent_buttons = _all_buttons(bot.send_photo.call_args.kwargs["reply_markup"])
    assert any(button.callback_data.startswith("invasion:join:") for button in sent_buttons)
    persisted = await db_session.scalar(
        select(Invasion).where(Invasion.group_id == group_id)
    )
    assert persisted is not None
    assert persisted.announcement_message_id == 9688


@pytest.mark.parametrize(
    ("outcome", "label"),
    [
        (InvasionOutcome.TOTAL_VICTORY, "پیروزی کامل"),
        (InvasionOutcome.NARROW_DEFENSE, "دفاع نزدیک"),
        (InvasionOutcome.DEFEAT, "شکست در دفاع"),
    ],
)
def test_all_persisted_outcomes_have_player_facing_labels(outcome, label):
    from sib.services.invasion_telegram import InvasionScreenSnapshot

    snapshot = InvasionScreenSnapshot(
        invasion_id=2,
        group_id=GROUP_ID,
        status=InvasionStatus.FINISHED,
        scheduled_at=NOW - timedelta(minutes=30),
        ended_at=NOW,
        current_wave_sequence=None,
        wave_count=2,
        current_wave_deadline=None,
        target_region_code=None,
        target_region_name=None,
        enemy_faction_code="moon_descendants",
        enemy_faction_name="فرزندان ماه",
        enemy_faction_symbol="🌙",
        enemy_power=100,
        defense_total=90,
        participant_count=3,
        outcome=outcome,
        announcement_message_id=MESSAGE_ID,
    )
    assert f"نتیجه: {label}" in render_invasion_status(snapshot, now=NOW)


def test_plain_status_renderer_shows_scheduled_and_terminal_states():
    from sib.services.invasion_telegram import InvasionScreenSnapshot

    scheduled = InvasionScreenSnapshot(
        invasion_id=1,
        group_id=GROUP_ID,
        status=InvasionStatus.SCHEDULED,
        scheduled_at=NOW + timedelta(minutes=5),
        ended_at=None,
        current_wave_sequence=1,
        wave_count=2,
        current_wave_deadline=None,
        target_region_code=None,
        target_region_name=None,
        enemy_faction_code="moon_descendants",
        enemy_faction_name="فرزندان ماه",
        enemy_faction_symbol="🌙",
        enemy_power=100,
        defense_total=0,
        participant_count=0,
        outcome=None,
        announcement_message_id=None,
    )
    text = render_invasion_status(scheduled, now=NOW)
    assert "تهاجم در راه است" in text
    assert "زمان تا آغاز" in text
    assert "۱ از ۲ پس از آغاز" in text
    assert "مشخص نشده" in text


@pytest.mark.asyncio
async def test_announcement_is_pinned_after_send_and_persisted_id(db_session):
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    bot = _bot(send_message_id=9317)

    published = await InvasionTelegramService.publish_maintenance_updates(bot, [])

    assert published == 1
    bot.send_photo.assert_awaited_once()
    bot.pin_chat_message.assert_awaited_once()
    assert bot.pin_chat_message.await_args.kwargs["chat_id"] == GROUP_ID
    assert bot.pin_chat_message.await_args.kwargs["message_id"] == 9317
    # No previous Sib announcement exists in this Group: nothing is unpinned.
    bot.unpin_chat_message.assert_not_awaited()

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9317
    assert persisted.status == InvasionStatus.ACTIVE


@pytest.mark.asyncio
async def test_pin_failure_does_not_fail_invasion_or_announcement(db_session, caplog):
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    bot = _bot(send_message_id=9321)
    bot.pin_chat_message = AsyncMock(side_effect=RuntimeError("telegram api down"))

    published = await InvasionTelegramService.publish_maintenance_updates(bot, [])

    # The announcement itself was sent and persisted: pinning is
    # presentation-only and its failure must not fail the publication.
    assert published == 1
    bot.pin_chat_message.assert_awaited_once()
    assert "Could not pin the Invasion announcement" in caplog.text

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.started_at is not None
    assert persisted.announcement_message_id == 9321

    # The announcement remains usable: the next pass edits it in place
    # instead of re-sending or losing the persisted pointer.
    caplog.clear()
    edited = await InvasionTelegramService.publish_maintenance_updates(
        bot,
        [invasion_id],
    )
    assert edited == 1
    bot.edit_message_media.assert_awaited_once()
    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9321
    assert persisted.status == InvasionStatus.ACTIVE


@pytest.mark.asyncio
async def test_missing_pin_permission_does_not_fail_invasion(db_session, caplog):
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    bot = _bot(send_message_id=9322)
    bot.pin_chat_message = AsyncMock(
        side_effect=TelegramBadRequest(
            method=None,
            message="Bad Request: not enough rights to pin messages in this chat",
        )
    )

    published = await InvasionTelegramService.publish_maintenance_updates(bot, [])

    assert published == 1
    bot.pin_chat_message.assert_awaited_once()
    assert "Could not pin the Invasion announcement" in caplog.text

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.announcement_message_id == 9322

    # The announcement remains usable after the permission failure.
    edited = await InvasionTelegramService.publish_maintenance_updates(
        bot,
        [invasion_id],
    )
    assert edited == 1
    bot.edit_message_media.assert_awaited_once()
    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9322
    assert persisted.status == InvasionStatus.ACTIVE


@pytest.mark.asyncio
async def test_previous_sib_announcement_is_unpinned_before_the_new_pin(db_session):
    previous = await _make_event(
        db_session,
        group_id=GROUP_ID,
        status=InvasionStatus.FINISHED,
        announcement_message_id=8801,
        wave_count=2,
    )
    previous_id = int(previous.id)
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    bot = _bot(send_message_id=9317)

    published = await InvasionTelegramService.publish_maintenance_updates(bot, [])

    assert published == 1
    # The previous Sib announcement (persisted by Sib for this Group) is
    # unpinned so pinned announcements do not accumulate...
    bot.unpin_chat_message.assert_awaited_once()
    assert bot.unpin_chat_message.await_args.kwargs == {
        "chat_id": GROUP_ID,
        "message_id": 8801,
    }
    # ...and the fresh announcement is pinned instead.
    bot.pin_chat_message.assert_awaited_once()
    assert bot.pin_chat_message.await_args.kwargs["chat_id"] == GROUP_ID
    assert bot.pin_chat_message.await_args.kwargs["message_id"] == 9317
    call_names = [call[0] for call in bot.mock_calls]
    assert call_names.index("unpin_chat_message") < call_names.index("pin_chat_message")

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9317
    assert persisted.status == InvasionStatus.ACTIVE
    # The previous occurrence's persisted announcement id is not corrupted.
    previous_row = await db_session.get(Invasion, previous_id)
    assert previous_row.announcement_message_id == 8801
    assert previous_row.status == InvasionStatus.FINISHED


@pytest.mark.asyncio
async def test_unpin_failure_of_previous_announcement_still_pins_and_keeps_invasion_valid(
    db_session,
    caplog,
):
    await _make_event(
        db_session,
        group_id=GROUP_ID,
        status=InvasionStatus.FINISHED,
        announcement_message_id=8801,
        wave_count=2,
    )
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    bot = _bot(send_message_id=9323)
    bot.unpin_chat_message = AsyncMock(
        side_effect=TelegramBadRequest(
            method=None,
            message="Bad Request: message to unpin not found",
        )
    )

    published = await InvasionTelegramService.publish_maintenance_updates(bot, [])

    assert published == 1
    bot.unpin_chat_message.assert_awaited_once()
    bot.pin_chat_message.assert_awaited_once()
    assert bot.pin_chat_message.await_args.kwargs["message_id"] == 9323
    assert "Could not unpin the previous Invasion announcement" in caplog.text

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.status == InvasionStatus.ACTIVE
    assert persisted.announcement_message_id == 9323


@pytest.mark.asyncio
async def test_unrelated_pinned_messages_are_never_unpinned(db_session):
    unrelated_admin_message_id = 4242
    other_group_id = GROUP_ID - 60
    # A Sib announcement persisted for ANOTHER Group must not be unpinned
    # when this Group's announcement is pinned.
    await _make_event(
        db_session,
        group_id=other_group_id,
        status=InvasionStatus.FINISHED,
        announcement_message_id=8899,
        wave_count=2,
    )
    # The previous Sib announcement of THIS Group is the only unpin candidate.
    await _make_event(
        db_session,
        group_id=GROUP_ID,
        status=InvasionStatus.FINISHED,
        announcement_message_id=8801,
        wave_count=2,
    )
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    bot = _bot(send_message_id=9317)

    published = await InvasionTelegramService.publish_maintenance_updates(bot, [])

    assert published == 1
    unpinned_ids = [
        call.kwargs["message_id"]
        for call in bot.unpin_chat_message.await_args_list
    ]
    # Only the id Sib itself persisted for this Group is unpinned: the other
    # Group's announcement and any unrelated admin/user message stay pinned.
    assert unpinned_ids == [8801]
    assert unrelated_admin_message_id not in unpinned_ids
    assert 8899 not in unpinned_ids
    bot.pin_chat_message.assert_awaited_once()
    assert bot.pin_chat_message.await_args.kwargs["message_id"] == 9317

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9317

    # A Group without any previous Sib announcement never unpins anything,
    # even when an unrelated message is pinned there.
    lonely_group_id = GROUP_ID - 61
    lonely = await _make_event(
        db_session,
        group_id=lonely_group_id,
        announcement_message_id=None,
    )
    lonely_id = int(lonely.id)
    lonely_bot = _bot(send_message_id=9330)
    lonely_published = await InvasionTelegramService.publish_maintenance_updates(
        lonely_bot,
        [],
    )
    assert lonely_published == 1
    lonely_bot.unpin_chat_message.assert_not_awaited()
    lonely_bot.pin_chat_message.assert_awaited_once()
    assert lonely_bot.pin_chat_message.await_args.kwargs["chat_id"] == lonely_group_id
    assert lonely_bot.pin_chat_message.await_args.kwargs["message_id"] == 9330
    db_session.expire_all()
    persisted_lonely = await db_session.get(Invasion, lonely_id)
    assert persisted_lonely.announcement_message_id == 9330


@pytest.mark.asyncio
async def test_announcement_retry_does_not_duplicate_messages_or_corrupt_announcement_state(
    db_session,
):
    previous = await _make_event(
        db_session,
        group_id=GROUP_ID,
        status=InvasionStatus.FINISHED,
        announcement_message_id=8801,
        wave_count=2,
    )
    previous_id = int(previous.id)
    invasion = await _make_event(db_session, announcement_message_id=None)
    invasion_id = int(invasion.id)
    bot = _bot(send_message_id=9317)

    first = await InvasionTelegramService.publish_maintenance_updates(bot, [])
    assert first == 1
    assert bot.send_photo.await_count == 1
    assert bot.send_message.await_count == 0
    bot.pin_chat_message.assert_awaited_once()
    bot.unpin_chat_message.assert_awaited_once()

    # A retry pass must not send or pin a second announcement: the persisted
    # id routes the update through the in-place edit path.
    second = await InvasionTelegramService.publish_maintenance_updates(
        bot,
        [invasion_id],
    )
    assert second == 1
    assert bot.send_photo.await_count == 1
    assert bot.send_message.await_count == 0
    bot.edit_message_media.assert_awaited_once()
    bot.pin_chat_message.assert_awaited_once()
    bot.unpin_chat_message.assert_awaited_once()

    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9317
    assert persisted.status == InvasionStatus.ACTIVE
    previous_row = await db_session.get(Invasion, previous_id)
    assert previous_row.announcement_message_id == 8801
    assert await db_session.scalar(
        select(func.count(Invasion.id)).where(Invasion.group_id == GROUP_ID)
    ) == 2


@pytest.mark.asyncio
async def test_status_refresh_replacement_repins_the_new_announcement(db_session):
    invasion = await _make_event(db_session)
    invasion_id = int(invasion.id)
    user = await _make_user(db_session, 70201)
    await db_session.commit()

    bot = _bot()
    bot.edit_message_media = AsyncMock(
        side_effect=TelegramBadRequest(
            method=None,
            message="there is no media in the message to edit",
        )
    )
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=9611))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9611))
    callback = _callback(
        user.id,
        invasion_callback_data("status", invasion_id),
        callback_id="status-replacement-pin",
    )
    await handle_invasion_callback(callback, bot)

    # The replacement message deleted the previously pinned announcement, so
    # the persisted replacement is pinned again (best-effort, never fatal).
    bot.pin_chat_message.assert_awaited_once()
    assert bot.pin_chat_message.await_args.kwargs["chat_id"] == GROUP_ID
    assert bot.pin_chat_message.await_args.kwargs["message_id"] == 9611
    bot.delete_message.assert_awaited_with(GROUP_ID, MESSAGE_ID)
    db_session.expire_all()
    persisted = await db_session.get(Invasion, invasion_id)
    assert persisted.announcement_message_id == 9611
    assert persisted.status == InvasionStatus.ACTIVE


def _final_snapshot(**overrides) -> InvasionScreenSnapshot:
    values = dict(
        invasion_id=2,
        group_id=GROUP_ID,
        status=InvasionStatus.FINISHED,
        scheduled_at=NOW - timedelta(minutes=30),
        started_at=NOW - timedelta(minutes=30),
        ended_at=NOW,
        current_wave_sequence=None,
        wave_count=4,
        current_wave_deadline=None,
        target_region_code=None,
        target_region_name="دره ماه",
        enemy_faction_code="moon_descendants",
        enemy_faction_name="فرزندان ماه",
        enemy_faction_symbol="🌙",
        enemy_power=100,
        defense_total=100,
        participant_count=3,
        outcome=InvasionOutcome.TOTAL_VICTORY,
        announcement_message_id=MESSAGE_ID,
        reward_golden_apples=10,
        rewards_settled=True,
    )
    values.update(overrides)
    return InvasionScreenSnapshot(**values)


@pytest.mark.parametrize(
    ("outcome", "amount"),
    [
        (InvasionOutcome.TOTAL_VICTORY, 10),
        (InvasionOutcome.NARROW_DEFENSE, 5),
        (InvasionOutcome.DEFEAT, 1),
    ],
)
def test_result_notice_names_the_golden_apple_inventory_reward(outcome, amount):
    text = render_final_notice(
        _final_snapshot(outcome=outcome, reward_golden_apples=amount)
    )
    persian_digits = {10: "۱۰", 5: "۵", 1: "۱"}
    assert f"🎁 پاداش: {persian_digits[amount]} 🌕 سیب طلایی" in text
    assert "برای هر مدافع واجد شرایط" in text
    # Rewards are inventory items, never the ordinary Apple wallet.
    assert "🍎" not in text


def test_result_notice_says_when_no_defender_was_eligible():
    text = render_final_notice(_final_snapshot(reward_golden_apples=None))
    assert "مدافع واجد شرایطی وجود نداشت" in text
    assert "🌕" not in text


def test_result_notice_for_an_interrupted_battle_is_not_a_defeat_or_a_reward():
    text = render_final_notice(
        _final_snapshot(outcome=None, reward_golden_apples=None, rewards_settled=True)
    )
    assert "قطع طولانی ربات" in text
    assert "نه پیروزی ثبت شد نه شکست" in text
    assert "پاداشی تعلق نمی‌گیرد" in text
    assert "نتیجه:" not in text
    assert "🌕" not in text
    assert "🍎" not in text


def test_result_notice_for_a_voided_legacy_battle_pays_nothing():
    text = render_final_notice(
        _final_snapshot(
            outcome=None,
            started_at=None,
            reward_golden_apples=None,
            rewards_settled=True,
        )
    )
    assert "بدون دفاع لغو شد" in text
    assert "پاداشی تعلق نمی‌گیرد" in text
    assert "🌕" not in text
