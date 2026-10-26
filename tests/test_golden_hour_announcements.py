"""Per-group Golden Hour notice timing, coalescing, and idempotency."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from sib.database.models import (
    GoldenHourGroupAnnouncement,
    Group,
    Inv2Invasion,
    Inv2Region,
)
from sib.services import golden_hour
from sib.services.golden_hour_announcement import (
    GOLDEN_HOUR_NOTICE_TEXT,
    GoldenHourGroupAnnouncementService,
)
from sib.services.invasion_v2 import presentation
from sib.services.maintenance.service import MaintenanceService


TEHRAN = golden_hour.TEHRAN


def _local_time(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 26, hour, minute, tzinfo=TEHRAN).astimezone(
        timezone.utc
    )


@pytest.mark.asyncio
async def test_standalone_notice_waits_for_spawn_window_and_is_once_per_group_day(
    db_session,
):
    db_session.add_all(
        [
            Group(id=-1001001, title="گروه فعال", is_active=True),
            Group(id=-1001002, title="گروه غیرفعال", is_active=False),
        ]
    )
    await db_session.commit()
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=501)),
        edit_message_caption=AsyncMock(),
    )

    # The default 20:00 spawn slot stays open through 20:30 Tehran.
    assert await GoldenHourGroupAnnouncementService.run_due(
        bot, now=_local_time(20, 29)
    ) == 0
    bot.send_message.assert_not_awaited()

    assert await GoldenHourGroupAnnouncementService.run_due(
        bot, now=_local_time(20, 30)
    ) == 1
    bot.send_message.assert_awaited_once_with(
        chat_id=-1001001,
        text=GOLDEN_HOUR_NOTICE_TEXT,
        disable_web_page_preview=True,
    )

    # A later maintenance tick the same Tehran day cannot send a duplicate.
    assert await GoldenHourGroupAnnouncementService.run_due(
        bot, now=_local_time(20, 45)
    ) == 0
    bot.send_message.assert_awaited_once()

    rows = list(
        (
            await db_session.scalars(
                select(GoldenHourGroupAnnouncement).order_by(
                    GoldenHourGroupAnnouncement.group_id
                )
            )
        ).all()
    )
    assert len(rows) == 1
    assert rows[0].group_id == -1001001
    assert rows[0].announcement_day == _local_time(20, 30).astimezone(TEHRAN).date()
    assert rows[0].source == "standalone"
    assert rows[0].status == "sent"
    assert rows[0].message_id == 501


@pytest.mark.asyncio
async def test_live_invasion_caption_is_edited_instead_of_sending_an_extra_message(
    db_session,
):
    group_id = -1002001
    now = _local_time(20, 30)
    db_session.add_all(
        [
            Group(id=group_id, title="یورش زنده", is_active=True),
            Inv2Region(
                code="golden_hour_test_region",
                name_fa="دشت آزمایش",
                controlling_faction="empire",
            ),
        ]
    )
    await db_session.flush()
    invasion = Inv2Invasion(
        group_id=group_id,
        region_code="golden_hour_test_region",
        enemy_type="soldier",
        enemy_name_fa="شیاسیب",
        enemy_hp=500,
        enemy_hp_remaining=350,
        difficulty_multiplier=2.0,
        status="ACTIVE",
        message_chat_id=group_id,
        message_id=601,
        announcement_narrative="نگهبانان برای دفاع از دشت آماده شدند.",
        scheduled_at=now - timedelta(minutes=5),
        started_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(minutes=20),
        occurrence_key="golden-hour-active-invasion",
    )
    db_session.add(invasion)
    await db_session.commit()

    bot = SimpleNamespace(
        edit_message_caption=AsyncMock(return_value=SimpleNamespace()),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=602)),
    )

    assert await GoldenHourGroupAnnouncementService.run_due(bot, now=now) == 1
    bot.edit_message_caption.assert_awaited_once()
    edit = bot.edit_message_caption.await_args.kwargs
    assert edit["chat_id"] == group_id
    assert edit["message_id"] == 601
    assert GOLDEN_HOUR_NOTICE_TEXT in edit["caption"]
    assert bot.send_message.await_count == 0

    await db_session.refresh(invasion)
    saved_invasion = await db_session.get(Inv2Invasion, invasion.id)
    assert GOLDEN_HOUR_NOTICE_TEXT in saved_invasion.announcement_narrative
    record = await db_session.get(
        GoldenHourGroupAnnouncement,
        (group_id, now.astimezone(TEHRAN).date()),
    )
    assert record.status == "sent"
    assert record.source == "invasion_edit"
    assert record.invasion_id == invasion.id
    assert record.message_id == 601


@pytest.mark.asyncio
@pytest.mark.golden_hour
async def test_new_invasion_announcement_includes_and_records_golden_hour_notice(
    db_session, monkeypatch, tmp_path
):
    group_id = -1003001
    db_session.add_all(
        [
            Group(id=group_id, title="یورش تازه", is_active=True),
            Inv2Region(
                code="golden_hour_new_region",
                name_fa="دشت تازه",
                controlling_faction="empire",
            ),
        ]
    )
    invasion = Inv2Invasion(
        group_id=group_id,
        region_code="golden_hour_new_region",
        enemy_type="soldier",
        enemy_name_fa="شیاسیب تازه",
        enemy_hp=300,
        enemy_hp_remaining=300,
        difficulty_multiplier=2.0,
        status="PENDING",
        scheduled_at=datetime.now(timezone.utc),
        occurrence_key="golden-hour-new-invasion",
    )
    db_session.add(invasion)
    await db_session.commit()

    image = tmp_path / "unused.jpg"
    image.write_bytes(b"not opened by this mocked Telegram call")
    monkeypatch.setattr(presentation, "cached_image", lambda *args, **kwargs: image)
    monkeypatch.setattr(
        presentation, "is_group_available", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        MaintenanceService, "_invasion_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(golden_hour, "is_golden_hour", lambda now=None: True)

    bot = SimpleNamespace(
        send_photo=AsyncMock(
            return_value=SimpleNamespace(
                chat=SimpleNamespace(id=group_id), message_id=701
            )
        )
    )

    await presentation.InvasionPresentation().announce(bot, invasion.id)

    bot.send_photo.assert_awaited_once()
    caption = bot.send_photo.await_args.kwargs["caption"]
    assert GOLDEN_HOUR_NOTICE_TEXT in caption
    assert bot.send_photo.await_args.kwargs["reply_markup"] is not None

    await db_session.refresh(invasion)
    saved_invasion = await db_session.get(Inv2Invasion, invasion.id)
    assert saved_invasion.status == "ACTIVE"
    assert GOLDEN_HOUR_NOTICE_TEXT in saved_invasion.announcement_narrative
    record = await db_session.scalar(
        select(GoldenHourGroupAnnouncement).where(
            GoldenHourGroupAnnouncement.group_id == group_id
        )
    )
    assert record is not None
    assert record.status == "sent"
    assert record.source == "invasion"
    assert record.message_id == 701


@pytest.mark.asyncio
@pytest.mark.golden_hour
async def test_terminal_invasion_caption_keeps_the_golden_hour_notice(
    db_session, monkeypatch, tmp_path
):
    group_id = -1004001
    db_session.add_all(
        [
            Group(id=group_id, title="یورش تمام‌شده", is_active=True),
            Inv2Region(
                code="golden_hour_final_region",
                name_fa="دشت نهایی",
                controlling_faction="empire",
            ),
        ]
    )
    invasion = Inv2Invasion(
        group_id=group_id,
        region_code="golden_hour_final_region",
        enemy_type="soldier",
        enemy_name_fa="شیاسیب نهایی",
        enemy_hp=300,
        enemy_hp_remaining=0,
        difficulty_multiplier=2.0,
        status="VICTORY",
        message_chat_id=group_id,
        message_id=801,
        announcement_narrative=f"روایت شروع یورش\n\n{GOLDEN_HOUR_NOTICE_TEXT}",
        scheduled_at=datetime.now(timezone.utc),
        occurrence_key="golden-hour-terminal-invasion",
    )
    db_session.add(invasion)
    await db_session.commit()

    image = tmp_path / "result.jpg"
    image.write_bytes(b"not opened by this mocked Telegram call")
    monkeypatch.setattr(presentation, "cached_image", lambda *args, **kwargs: image)
    monkeypatch.setattr(
        MaintenanceService, "_invasion_enabled", AsyncMock(return_value=True)
    )
    bot = SimpleNamespace(
        edit_message_caption=AsyncMock(return_value=SimpleNamespace()),
        send_photo=AsyncMock(
            return_value=SimpleNamespace(
                chat=SimpleNamespace(id=group_id), message_id=802
            )
        ),
    )

    assert await presentation.InvasionPresentation().finalize(bot, invasion.id)

    assert GOLDEN_HOUR_NOTICE_TEXT in bot.edit_message_caption.await_args.kwargs[
        "caption"
    ]
    assert GOLDEN_HOUR_NOTICE_TEXT in bot.send_photo.await_args.kwargs["caption"]
