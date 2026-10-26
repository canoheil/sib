"""Curated daily official-channel digest: scheduled once, factual, and quiet-safe."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

from sib.database.models import (
    DailyChannelPost,
    Game,
    Group,
    GroupCastle,
    GroupStats,
    GroupWar,
    Match,
    MatchState,
    MatchType,
    User,
    AppleMineShift,
    WarAttack,
)
from sib.services.config_store import ConfigService
from sib.services.daily_channel_digest import (
    DailyChannelDigestService,
    preview_reason_fa,
)
from sib.services.maintenance.service import MaintenanceService
from sib.services.sib_claim import fa_digits
from sib.handlers.admin_daily_digest import _panel

NOW = datetime(2026, 10, 26, 16, 30, tzinfo=timezone.utc)  # 20:00 Asia/Tehran


class FakeBot:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        if self.fail:
            raise RuntimeError("temporary Telegram failure")
        return SimpleNamespace(message_id=987)


async def _settings(session, **overrides):
    values = {
        "league_enabled": True,
        "league_rewards": {"1": 300, "2": 150, "3": 100},
        "league_min_points": 15,
        "league_first_week": "2026-10-01",
        "daily_digest_enabled": True,
        "daily_digest_hour": 20,
        "daily_digest_channel_id": "@sib_test_channel",
        **overrides,
    }
    for key, value in values.items():
        await ConfigService.set(session, key, value)
    await session.commit()


async def _group(session, title: str, group_id: int) -> Group:
    group = Group(id=group_id, title=title)
    session.add(group)
    session.add(GroupStats(group_id=group_id))
    await session.flush()
    return group


async def _match(session, game, group1, group2, *, at=NOW - timedelta(minutes=20), winner=None, friendly=False, bot=False):
    winner = winner if winner is not None else group1.id
    match = Match(
        game_id=game.id,
        mode="solo",
        state=MatchState.FINISHED,
        match_type=MatchType.FRIENDLY if friendly else MatchType.NORMAL,
        is_bot_match=bot,
        result_applied=True,
        group1_id=group1.id,
        group2_id=group2.id,
        winner_group_id=winner,
        winner_team=1 if winner == group1.id else 2,
        is_draw=winner is None,
        finished_at=at,
    )
    session.add(match)
    await session.flush()
    return match


async def test_preview_is_a_concise_real_league_snapshot(db_session):
    await _settings(db_session)
    game = Game(code="digest_test", name_fa="بازی تست")
    db_session.add(game)
    await db_session.flush()
    first = await _group(db_session, "گروه سیب", -70001)
    second = await _group(db_session, "گروه گردو", -70002)
    await _match(db_session, game, first, second)
    await db_session.commit()

    preview = await DailyChannelDigestService.build_preview(db_session, now=NOW)

    assert preview.ready
    assert "🍏 نبض روزانهٔ لیگ سیب" in preview.text
    assert "گروه سیب" in preview.text
    assert "گروه گردو" in preview.text
    assert "🏆 جدول زندهٔ این هفته" in preview.text
    assert "پر‌بردترین گروه" in preview.text
    assert len(preview.text) < 1000


async def test_preview_ignores_friendly_and_bot_matches(db_session):
    await _settings(db_session)
    game = Game(code="digest_filtered_test", name_fa="بازی تست")
    db_session.add(game)
    await db_session.flush()
    first = await _group(db_session, "گروه معمولی", -70011)
    second = await _group(db_session, "گروه دوم", -70012)
    await _match(db_session, game, first, second, friendly=True)
    await _match(db_session, game, first, second, bot=True)
    await db_session.commit()

    preview = await DailyChannelDigestService.build_preview(db_session, now=NOW)
    assert preview.reason == "quiet_day"
    assert not preview.ready


async def test_scheduled_post_waits_until_20_tehran_and_posts_once(db_session):
    await _settings(db_session)
    game = Game(code="digest_schedule_test", name_fa="بازی تست")
    db_session.add(game)
    await db_session.flush()
    first = await _group(db_session, "گروه شب", -70021)
    second = await _group(db_session, "گروه روز", -70022)
    await _match(db_session, game, first, second)
    await db_session.commit()

    bot = FakeBot()
    before = await DailyChannelDigestService.run_scheduled(
        bot, now=NOW - timedelta(minutes=1)
    )
    assert before.status == "not_due"
    assert bot.messages == []

    result = await DailyChannelDigestService.run_scheduled(bot, now=NOW)
    assert result.status == "sent"
    assert result.message_id == 987
    assert bot.messages[0]["chat_id"] == "@sib_test_channel"
    assert bot.messages[0]["parse_mode"] is None
    assert bot.messages[0]["reply_markup"].inline_keyboard[0][0].url.endswith(
        "?startgroup=true"
    )

    repeated = await DailyChannelDigestService.run_scheduled(
        bot, now=NOW + timedelta(minutes=10)
    )
    assert repeated.status == "sent"
    assert len(bot.messages) == 1
    record = await db_session.get(DailyChannelPost, NOW.date())
    assert record.status == "sent"
    assert record.attempt_count == 1
    assert record.message_id == 987


async def test_quiet_day_is_skipped_once_without_filler(db_session):
    await _settings(db_session)
    bot = FakeBot()

    first = await DailyChannelDigestService.run_scheduled(bot, now=NOW)
    second = await DailyChannelDigestService.run_scheduled(
        bot, now=NOW + timedelta(minutes=30)
    )

    assert first.status == second.status == "skipped"
    assert first.reason == "quiet_day"
    assert bot.messages == []
    record = await db_session.get(DailyChannelPost, NOW.date())
    assert record.status == "skipped"
    assert preview_reason_fa(record.skip_reason) == (
        "امروز فعالیت گروهیِ قابل گزارش ثبت نشده؛ پست خالی نمی‌گذاریم."
    )


async def test_failed_send_does_not_retry_automatically_but_admin_can_retry(db_session):
    await _settings(db_session)
    game = Game(code="digest_retry_test", name_fa="بازی تست")
    db_session.add(game)
    await db_session.flush()
    first = await _group(db_session, "گروه برنده", -70031)
    second = await _group(db_session, "گروه رقیب", -70032)
    await _match(db_session, game, first, second)
    await db_session.commit()

    failing_bot = FakeBot(fail=True)
    failed = await DailyChannelDigestService.run_scheduled(failing_bot, now=NOW)
    assert failed.status == "failed"
    assert len(failing_bot.messages) == 1

    # A scheduled tick never retries an uncertain/failed channel send.
    no_retry = await DailyChannelDigestService.run_scheduled(
        failing_bot, now=NOW + timedelta(minutes=5)
    )
    assert no_retry.status == "failed"
    assert len(failing_bot.messages) == 1

    retry_bot = FakeBot()
    retried = await DailyChannelDigestService.retry_failed_today(retry_bot, now=NOW)
    assert retried.status == "sent"
    assert len(retry_bot.messages) == 1
    record = await db_session.get(DailyChannelPost, NOW.date())
    assert record.status == "sent"
    assert record.attempt_count == 2


async def test_disabled_digest_never_creates_a_post_record(db_session):
    await _settings(db_session, daily_digest_enabled=False)
    bot = FakeBot()

    result = await DailyChannelDigestService.run_scheduled(bot, now=NOW)

    assert result.status == "disabled"
    assert bot.messages == []
    assert await db_session.get(DailyChannelPost, NOW.date()) is None


async def test_daily_activity_counts_group_mining_and_excludes_bot_users(db_session):
    await _settings(db_session)
    human_id = uuid.uuid4().int % 900_000_000 + 1
    bot_id = human_id + 1
    human = User(id=human_id, first_name="معدن‌کار", is_bot=False)
    bot_user = User(id=bot_id, first_name="ربات", is_bot=True)
    group = await _group(db_session, "گروه معدن", -70041)
    db_session.add_all([human, bot_user])
    await db_session.flush()
    db_session.add_all(
        [
            AppleMineShift(
                user_id=human.id,
                group_id=group.id,
                outcome="normal",
                item_code="apple_normal",
                amount=75,
                worked_at=NOW - timedelta(minutes=10),
            ),
            AppleMineShift(
                user_id=bot_user.id,
                group_id=group.id,
                outcome="normal",
                item_code="apple_normal",
                amount=900,
                worked_at=NOW - timedelta(minutes=5),
            ),
        ]
    )
    await db_session.commit()

    preview = await DailyChannelDigestService.build_preview(db_session, now=NOW)

    assert preview.ready
    assert f"⛏ {fa_digits(75)} سیب معمولی از معدن" in preview.text
    assert "900" not in preview.text


async def test_schedule_hour_is_clamped_and_channel_override_is_used(db_session):
    await _settings(
        db_session,
        daily_digest_hour=99,
        daily_digest_channel_id="@digest_override",
    )

    config = await DailyChannelDigestService.load_settings(db_session)

    assert config.hour == 23
    assert config.channel_id == "@digest_override"


async def test_admin_panel_exposes_toggle_preview_and_schedule(db_session):
    await _settings(db_session, daily_digest_enabled=False)

    text, keyboard = await _panel()
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    }

    assert "پست روزانهٔ کانال" in text
    assert "خاموش" in text
    assert "@sib_test_channel" in text
    assert "۲۰:۰۰" in text
    assert "admin_daily_digest:toggle" in callbacks
    assert "admin_daily_digest:preview" in callbacks


async def test_maintenance_stops_checking_after_a_daily_decision(db_session, monkeypatch):
    from sib.services.daily_channel_digest import TEHRAN

    today = datetime.now(timezone.utc).astimezone(TEHRAN).date()
    scheduled = AsyncMock(
        return_value=SimpleNamespace(status="sent", day=today)
    )
    monkeypatch.setattr(DailyChannelDigestService, "run_scheduled", scheduled)
    monkeypatch.setattr(MaintenanceService, "_bot", FakeBot())
    monkeypatch.setattr(MaintenanceService, "_last_daily_digest_sweep_at", None)
    monkeypatch.setattr(MaintenanceService, "_last_daily_digest_decision_day", None)

    await MaintenanceService.run_once()
    # Even once the normal 60-second check interval has elapsed, a durable
    # decision means this process need not read config/status again today.
    MaintenanceService._last_daily_digest_sweep_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    await MaintenanceService.run_once()

    scheduled.assert_awaited_once()
    assert MaintenanceService._last_daily_digest_decision_day == today


async def test_daily_war_attacks_alone_qualify_for_the_digest(db_session):
    await _settings(db_session)
    human_id = uuid.uuid4().int % 900_000_000 + 1
    human = User(id=human_id, first_name="جنگجو", is_bot=False)
    attacker = await _group(db_session, "گروه مهاجم", -70051)
    defender = await _group(db_session, "گروه مدافع", -70052)
    db_session.add_all(
        [GroupCastle(group_id=attacker.id), GroupCastle(group_id=defender.id), human]
    )
    await db_session.flush()
    war = GroupWar(
        attacker_id=attacker.id,
        defender_id=defender.id,
        status="active",
        started_at=NOW - timedelta(hours=1),
        ends_at=NOW + timedelta(hours=1),
    )
    db_session.add(war)
    await db_session.flush()
    db_session.add(
        WarAttack(
            war_id=war.id,
            user_id=human.id,
            source_group_id=attacker.id,
            target_group_id=defender.id,
            damage=1200,
            is_buff_used=False,
            created_at=NOW - timedelta(minutes=5),
        )
    )
    await db_session.commit()

    preview = await DailyChannelDigestService.build_preview(db_session, now=NOW)

    assert preview.ready
    assert f"{fa_digits(1200)} دمیج در جنگ‌ها" in preview.text


async def test_yesterdays_leaderboard_alone_does_not_fill_a_quiet_day(db_session):
    await _settings(db_session)
    game = Game(code="digest_stale_leaderboard_test", name_fa="بازی تست")
    db_session.add(game)
    await db_session.flush()
    first = await _group(db_session, "گروه دیروز", -70061)
    second = await _group(db_session, "حریف دیروز", -70062)
    await _match(
        db_session,
        game,
        first,
        second,
        at=NOW - timedelta(days=1),
    )
    await db_session.commit()

    preview = await DailyChannelDigestService.build_preview(db_session, now=NOW)

    assert preview.reason == "quiet_day"
    assert not preview.ready
