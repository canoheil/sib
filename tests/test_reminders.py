"""Come-back reminders: who gets one, when, and everything that stops spam.

The reminder loop is the first thing in sib that messages a player without
being asked, so the rules are tested as strictly as the feature:

* nothing at all when ``reminders_enabled`` is false (the built-in default);
* nothing outside the local evening window, and nothing twice inside
  ``reminders_min_gap_hours``;
* the chest nudge only after ``reminders_chest_after_hour``;
* a garden nudge when a plot has fruit ready;
* exactly one message per player per sweep, capped by ``reminders_batch_size``;
* a blocked bot (``TelegramForbiddenError``) is recorded and never retried;
* any other Telegram failure is NOT recorded as sent, so the player keeps
  their turn;
* the player's own switch (profile «🔔/🔕 یادآوری‌ها») is honoured.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select, text

from sib.database.models import (
    DailyChestClaim,
    Garden,
    GardenPlot,
    User,
    UserReminderState,
)
from sib.handlers.reminders import (
    REMINDERS_OFF_ALERT,
    REMINDERS_ON_ALERT,
    handle_reminders_toggle,
)
from sib.services.config_store import ConfigService
from sib.services.daily_chest import utc_day
from sib.services.reminders import (
    CHEST_BUTTON,
    GARDEN_BUTTON,
    KIND_CHEST,
    KIND_GARDEN,
    ReminderService,
    ReminderTickStats,
)
from sib.tests.test_parties import create_test_user

# 19:00 Tehran == 15:30 UTC (Iran is UTC+3:30, no DST since 2022).
EVENING_UTC = datetime(2026, 10, 25, 15, 30, tzinfo=timezone.utc)
NOON_UTC = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)  # 11:30 Tehran


@pytest.fixture(autouse=True)
async def clean_players(db_session):
    """Only THIS module's players exist, so a sweep can be asserted exactly.

    ``users`` rows are not in the shared conftest cleanup (they are committed
    by other suites), so every test starts from an empty player table.
    """
    for table in (
        "user_reminder_states",
        "daily_chest_claims",
        "garden_plots",
        "gardens",
        "apple_transactions",
        "apple_wallets",
        "users",
    ):
        await db_session.execute(text(f"DELETE FROM {table}"))
    await db_session.commit()
    yield


async def _enable(db_session, **overrides) -> None:
    values = {"reminders_enabled": True, **overrides}
    async with db_session.begin():
        for key, value in values.items():
            await ConfigService.set(db_session, key, value)


async def _make_user(db_session, name: str = "سارا") -> User:
    return await create_test_user(db_session, name)


async def _claim_chest(db_session, user: User, *, day: str | None = None) -> None:
    db_session.add(
        DailyChestClaim(
            user_id=user.id,
            claim_day=day or utc_day(),
            reward_type="apples",
            quantity=10,
        )
    )
    await db_session.commit()


async def _ready_garden(
    db_session, user: User, *, plots: int = 1, now: datetime | None = None
) -> None:
    """A garden whose fruit became ready just before ``now``."""
    garden = Garden(user_id=user.id)
    db_session.add(garden)
    await db_session.flush()
    now = now or datetime.now(timezone.utc)
    for index in range(plots):
        db_session.add(
            GardenPlot(
                garden_id=garden.user_id,
                plot_index=index,
                planted_at=now - timedelta(hours=5),
                mature_at=now - timedelta(hours=2),
                fruit_ready_at=now - timedelta(minutes=5),
            )
        )
    await db_session.commit()


def _bot() -> AsyncMock:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


# --------------------------------------------------------------------------- #
# On/off and the window
# --------------------------------------------------------------------------- #
async def test_disabled_by_default_sends_nothing(db_session):
    bot = _bot()
    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert isinstance(stats, ReminderTickStats)
    assert stats.sent == 0
    assert stats.reason == "disabled"
    bot.send_message.assert_not_awaited()


async def test_outside_the_evening_window_sends_nothing(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    bot = _bot()

    # 11:30 Tehran: before the 16:00 window.
    stats = await ReminderService.run_tick(bot, now=NOON_UTC)

    assert stats.reason == "outside window"
    bot.send_message.assert_not_awaited()
    await db_session.refresh(user)


@pytest.mark.parametrize(
    "hour,start,end,expected",
    [
        (15, 16, 22, False),
        (16, 16, 22, True),
        (21, 16, 22, True),
        (22, 16, 22, False),  # half-open: the end hour is out
        (23, 22, 2, True),  # wrap-around window
        (1, 22, 2, True),
        (3, 22, 2, False),
        (10, 10, 10, False),  # empty window
    ],
)
def test_in_window_math(hour, start, end, expected):
    assert ReminderService.in_window(hour, start, end) is expected


# --------------------------------------------------------------------------- #
# Chest reminder
# --------------------------------------------------------------------------- #
async def test_chest_reminder_goes_out_once(db_session):
    await _enable(db_session)
    await _make_user(db_session)
    bot = _bot()

    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.sent == 1
    assert bot.send_message.await_count == 1
    args, kwargs = bot.send_message.await_args
    assert "صندوق روزانه" in args[1]
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == CHEST_BUTTON
    assert kwargs["parse_mode"] is None

    # The state row is the per-player clock.
    state = await db_session.get(UserReminderState, args[0])
    assert state is not None
    assert state.last_kind == KIND_CHEST
    assert state.last_sent_at is not None


async def test_no_second_message_inside_the_gap(db_session):
    await _enable(db_session)
    await _make_user(db_session)
    bot = _bot()

    await ReminderService.run_tick(bot, now=EVENING_UTC)
    again = await ReminderService.run_tick(
        bot, now=EVENING_UTC + timedelta(hours=2)
    )

    assert again.sent == 0
    assert bot.send_message.await_count == 1


async def test_message_after_the_gap_is_allowed(db_session):
    await _enable(db_session)
    await _make_user(db_session)
    bot = _bot()

    await ReminderService.run_tick(bot, now=EVENING_UTC)
    later = await ReminderService.run_tick(
        bot, now=EVENING_UTC + timedelta(hours=22)
    )

    # Past the 20h gap, chest still closed, and inside the window past the
    # chest hour: 15:30 UTC + 22h = 13:30 UTC = 17:00 Tehran.
    assert later.sent == 1


async def test_claimed_chest_means_no_chest_reminder(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    await _claim_chest(db_session, user)
    bot = _bot()

    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.sent == 0
    bot.send_message.assert_not_awaited()


async def test_chest_nudge_waits_for_its_hour(db_session):
    await _enable(db_session, reminders_chest_after_hour=20)
    await _make_user(db_session)
    bot = _bot()

    # 19:00 Tehran is inside the window but before chest_after_hour=20, and
    # there is no garden to talk about either.
    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.sent == 0
    bot.send_message.assert_not_awaited()


async def test_chest_reminder_names_the_live_streak(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    today = datetime.now(timezone.utc).date()
    for offset in (2, 1):
        await _claim_chest(db_session, user, day=(today - timedelta(days=offset)).isoformat())
    bot = _bot()

    await ReminderService.run_tick(bot, now=EVENING_UTC)

    text = bot.send_message.await_args.args[1]
    # Today is still unclaimed, so the LIVE streak is the two days behind it
    # and the copy warns that opening the chest is what keeps it.
    assert "استریکت ۲ روزه" in text
    assert "از صفر شروع می" in text
    assert str(2) not in text  # Persian digits only


# --------------------------------------------------------------------------- #
# Garden reminder
# --------------------------------------------------------------------------- #
async def test_garden_reminder_when_fruit_is_ready(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    await _claim_chest(db_session, user)  # chest done today → garden turn
    await _ready_garden(db_session, user, plots=2, now=EVENING_UTC)
    bot = _bot()

    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.sent == 1
    args, kwargs = bot.send_message.await_args
    assert "باغت" in args[1]
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == GARDEN_BUTTON
    state = await db_session.get(UserReminderState, user.id)
    assert state.last_kind == KIND_GARDEN


async def test_withered_fruit_is_not_advertised(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    await _claim_chest(db_session, user)
    await _ready_garden(db_session, user, plots=1, now=EVENING_UTC)

    plot = await db_session.scalar(select(GardenPlot).where(GardenPlot.garden_id == user.id))
    plot.is_withered = True
    await db_session.commit()

    bot = _bot()
    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    # Not "silent because of some other gate": the sweep ran and found
    # nothing worth a DM.
    assert stats.reason == "nothing to say"
    assert stats.sent == 0
    bot.send_message.assert_not_awaited()


async def test_one_healthy_plot_is_enough_even_if_another_is_withered(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    await _claim_chest(db_session, user)
    await _ready_garden(db_session, user, plots=2, now=EVENING_UTC)

    withered = (
        await db_session.scalars(
            select(GardenPlot).where(GardenPlot.garden_id == user.id)
        )
    ).all()
    withered[0].is_withered = True
    await db_session.commit()

    bot = _bot()
    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.sent == 1
    args, _kwargs = bot.send_message.await_args
    # Only the healthy plot is counted.
    assert "۱ درخت" in args[1]


async def test_unripe_garden_is_not_advertised(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    await _claim_chest(db_session, user)
    await _ready_garden(db_session, user, plots=1, now=EVENING_UTC)

    plot = await db_session.scalar(select(GardenPlot).where(GardenPlot.garden_id == user.id))
    plot.fruit_ready_at = EVENING_UTC + timedelta(hours=3)
    await db_session.commit()

    stats = await ReminderService.run_tick(_bot(), now=EVENING_UTC)
    assert stats.sent == 0


# --------------------------------------------------------------------------- #
# Respect for the player
# --------------------------------------------------------------------------- #
async def test_banned_and_bot_accounts_are_never_messaged(db_session):
    await _enable(db_session)
    banned = await _make_user(db_session, "banned")
    banned.is_banned = True
    robot = await _make_user(db_session, "robot")
    robot.is_bot = True
    inactive = await _make_user(db_session, "inactive")
    inactive.is_active = False
    await db_session.commit()
    bot = _bot()

    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.sent == 0
    bot.send_message.assert_not_awaited()


async def test_opted_out_player_is_skipped(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    async with db_session.begin():
        await ReminderService.set_opted_out(db_session, user.id, True)
    bot = _bot()

    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.sent == 0
    bot.send_message.assert_not_awaited()


async def test_blocked_bot_is_recorded_and_never_retried(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    bot = _bot()
    bot.send_message.side_effect = TelegramForbiddenError(
        method=MagicMock(), message="bot was blocked by the user"
    )

    first = await ReminderService.run_tick(bot, now=EVENING_UTC)
    assert first.blocked == 1
    state = await db_session.get(UserReminderState, user.id)
    await db_session.refresh(state)
    assert state.blocked_at is not None
    assert state.opted_out is True

    bot.send_message.reset_mock()
    second = await ReminderService.run_tick(bot, now=EVENING_UTC + timedelta(days=1))
    assert second.sent == 0
    bot.send_message.assert_not_awaited()


async def test_re_opt_in_clears_the_stale_block(db_session):
    """Pressing the switch proves the bot can reach the player again."""
    await _enable(db_session)
    user = await _make_user(db_session)
    bot = _bot()
    bot.send_message.side_effect = TelegramForbiddenError(
        method=MagicMock(), message="bot was blocked by the user"
    )
    await ReminderService.run_tick(bot, now=EVENING_UTC)

    await ReminderService.set_opted_out(db_session, user.id, False)
    # The sweep reads through its own session, so the flip must be visible.
    await db_session.commit()

    state = await db_session.get(UserReminderState, user.id)
    await db_session.refresh(state)
    assert state.blocked_at is None
    assert state.opted_out is False

    # The next sweep may message them again.
    bot.send_message.reset_mock()
    bot.send_message.side_effect = None
    stats = await ReminderService.run_tick(
        bot, now=EVENING_UTC + timedelta(hours=1)
    )
    assert stats.sent == 1
    assert stats.blocked == 0


async def test_opt_out_creates_a_row_for_a_player_who_never_got_one(db_session):
    user = await _make_user(db_session)

    await ReminderService.set_opted_out(db_session, user.id, True)

    state = await db_session.get(UserReminderState, user.id)
    assert state is not None
    assert state.opted_out is True
    assert state.blocked_at is None


async def test_other_delivery_failure_keeps_the_turn(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    bot = _bot()
    bot.send_message.side_effect = RuntimeError("network hiccup")

    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.failed == 1
    assert stats.sent == 0
    # Not marked as sent: the player keeps their turn for a later sweep.
    state = await db_session.get(UserReminderState, user.id)
    assert state is None or state.last_sent_at is None

    bot.send_message.side_effect = None
    retry = await ReminderService.run_tick(bot, now=EVENING_UTC + timedelta(minutes=5))
    assert retry.sent == 1


async def test_batch_size_caps_one_sweep(db_session):
    await _enable(db_session, reminders_batch_size=2)
    for index in range(5):
        await _make_user(db_session, f"player{index}")
    bot = _bot()

    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.sent == 2
    assert bot.send_message.await_count == 2


async def test_nothing_to_say_means_no_message(db_session):
    await _enable(db_session)
    user = await _make_user(db_session)
    await _claim_chest(db_session, user)
    bot = _bot()

    stats = await ReminderService.run_tick(bot, now=EVENING_UTC)

    assert stats.reason == "nothing to say"
    assert stats.sent == 0
    bot.send_message.assert_not_awaited()


# --------------------------------------------------------------------------- #
# The player's switch
# --------------------------------------------------------------------------- #
async def test_toggle_turns_reminders_off_and_on(db_session):
    user = await _make_user(db_session)
    callback = MagicMock()
    callback.from_user = MagicMock(id=user.id)
    callback.message = MagicMock()
    callback.message.chat = MagicMock(type="private")
    callback.answer = AsyncMock()

    async def _flip(expect_alert):
        callback.answer.reset_mock()
        with patch(
            "sib.handlers.reminders.get_session", _session_scope(db_session)
        ), patch(
            "sib.handlers.start.handle_apple_profile", new=AsyncMock()
        ) as render:
            await handle_reminders_toggle(callback, AsyncMock())
        assert callback.answer.await_args.args[0] == expect_alert
        render.assert_awaited_once()

    await _flip(REMINDERS_OFF_ALERT)
    assert await ReminderService.is_opted_out(db_session, user.id) is True

    await _flip(REMINDERS_ON_ALERT)
    assert await ReminderService.is_opted_out(db_session, user.id) is False


def _session_scope(session):
    """A ``get_session`` stand-in that yields the test session without closing it."""

    @asynccontextmanager
    async def scope():
        yield session
        await session.commit()

    return scope


async def test_toggle_is_private_only(db_session):
    user = await _make_user(db_session)
    callback = MagicMock()
    callback.from_user = MagicMock(id=user.id)
    callback.message = MagicMock()
    callback.message.chat = MagicMock(type="supergroup")
    callback.answer = AsyncMock()

    await handle_reminders_toggle(callback, AsyncMock())

    assert "چت خصوصی" in callback.answer.await_args.args[0]
    assert await ReminderService.is_opted_out(db_session, user.id) is False


async def test_profile_keyboard_shows_the_switch_state():
    from sib.keyboards.common import get_profile_keyboard

    on_buttons = [
        button
        for row in get_profile_keyboard(reminders_on=True).inline_keyboard
        for button in row
    ]
    off_buttons = [
        button
        for row in get_profile_keyboard(reminders_on=False).inline_keyboard
        for button in row
    ]
    plain_buttons = [
        button for row in get_profile_keyboard().inline_keyboard for button in row
    ]

    assert any("🔔 یادآوری‌ها: روشن" == b.text for b in on_buttons)
    assert any("🔕 یادآوری‌ها: خاموش" == b.text for b in off_buttons)
    assert all(b.callback_data != "reminders_toggle" for b in plain_buttons)
    assert any(
        b.callback_data == "reminders_toggle"
        for b in on_buttons
        if b.text.startswith("🔔")
    )


# --------------------------------------------------------------------------- #
# Migration contract
# --------------------------------------------------------------------------- #
def test_migration_063_matches_the_model():
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "database/migrations/versions/063_reminders.py"
    )
    source = path.read_text(encoding="utf-8")

    assert 'revision: str = "063_reminders"' in source
    assert 'down_revision: Union[str, None] = "062_transfers"' in source
    assert '"user_reminder_states"' in source
    for column in ("last_sent_at", "last_kind", "blocked_at", "opted_out"):
        assert f'"{column}"' in source
    assert 'ondelete="CASCADE"' in source
    # The seeds must never overwrite an operator's customised value.
    assert "ON DUPLICATE KEY UPDATE" in source
    for key in (
        "reminders_enabled",
        "reminders_min_gap_hours",
        "reminders_window_start_hour",
        "reminders_window_end_hour",
        "reminders_chest_after_hour",
        "reminders_batch_size",
    ):
        assert f'"{key}"' in source


# --------------------------------------------------------------------------- #
# Maintenance wiring
# --------------------------------------------------------------------------- #
def test_sweep_is_throttled_to_the_configured_interval():
    from sib.services.maintenance.service import MaintenanceService

    MaintenanceService._last_reminder_sweep_at = None
    assert MaintenanceService._reminder_sweep_due(EVENING_UTC) is True

    MaintenanceService._last_reminder_sweep_at = EVENING_UTC
    assert (
        MaintenanceService._reminder_sweep_due(
            EVENING_UTC + timedelta(seconds=30)
        )
        is False
    )
    assert (
        MaintenanceService._reminder_sweep_due(
            EVENING_UTC + timedelta(seconds=MaintenanceService.REMINDER_SWEEP_INTERVAL_SECONDS)
        )
        is True
    )
    MaintenanceService._last_reminder_sweep_at = None


async def test_reminders_are_wired_into_the_maintenance_cycle():
    import inspect

    from sib.services.maintenance import service as maintenance

    source = inspect.getsource(maintenance.MaintenanceService.run_once)
    assert "ReminderService.run_tick" in source
    assert "Maintenance task failed: come-back reminders" in source
