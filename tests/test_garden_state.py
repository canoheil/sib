"""Garden state regression suite (bug report requirement #1).

The production bug: GardenService compared naive MariaDB DATETIME values
against naive or aware `now` values inconsistently, raising
`TypeError: can't subtract offset-naive and offset-aware datetimes` or
miscomputing states - the garden broke in production even after the
f34448f tz patch.

These tests pin the contract:
- naive AND aware DB datetimes work and produce IDENTICAL states;
- the lifecycle is deterministic: EMPTY -> GROWING -> MATURE -> FRUIT_READY
  -> HARVEST -> next cycle;
- every garden action re-renders the garden panel (no manual refresh).
"""

from datetime import datetime, timedelta, timezone

import pytest

from sib.database.models import Garden, GardenPlot, PlotState
from sib.services.config_store import CONFIG_DEFAULTS
from sib.services.garden import GardenService
from sib.services.seedling import SeedlingService


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _mk_plot(**times) -> GardenPlot:
    plot = GardenPlot(garden_id=1, plot_index=0, cycle_count=0)
    for key, value in times.items():
        setattr(plot, key, value)
    return plot


class _Garden:
    """Minimal stand-in for the Garden aggregate (plots relationship)."""

    def __init__(self, plots):
        self.plots = plots


async def _fresh_garden(session, user_id: int) -> Garden:
    """Re-load the garden aggregate with plots inside the open session."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    return await session.scalar(
        select(Garden)
        .where(Garden.user_id == user_id)
        .options(selectinload(Garden.plots))
    )


# ---------------------------------------------------------------------------
# 1. Timezone robustness: naive and aware DB datetimes behave identically.
# ---------------------------------------------------------------------------

def test_derive_state_consistent_for_naive_and_aware_db_values():
    base = datetime(2026, 9, 27, 12, 0, 0)  # naive, like MariaDB DATETIME
    planted = base
    mature = base + timedelta(hours=2)
    fruit = base + timedelta(hours=4)

    naive_plot = _mk_plot(planted_at=planted, mature_at=mature, fruit_ready_at=fruit)
    aware_plot = _mk_plot(
        planted_at=_aware(planted),
        mature_at=_aware(mature),
        fruit_ready_at=_aware(fruit),
    )

    for probe_naive in (
        base - timedelta(minutes=1),
        base + timedelta(hours=1),
        base + timedelta(hours=3),
        base + timedelta(hours=5),
    ):
        probe_aware = _aware(probe_naive)
        s_naive = GardenService._derive_state(naive_plot, probe_naive)
        s_aware = GardenService._derive_state(aware_plot, probe_aware)
        assert s_naive == s_aware

    # Explicit expected states along the timeline.
    assert GardenService._derive_state(naive_plot, base - timedelta(minutes=1)) == PlotState.GROWING
    assert GardenService._derive_state(naive_plot, base) == PlotState.GROWING
    assert GardenService._derive_state(naive_plot, mature) == PlotState.MATURE
    assert GardenService._derive_state(naive_plot, fruit) == PlotState.FRUIT_READY

    # Mismatched combinations (naive plot + aware now and vice versa) work.
    assert GardenService._derive_state(naive_plot, _aware(fruit)) == PlotState.FRUIT_READY
    assert GardenService._derive_state(aware_plot, fruit) == PlotState.FRUIT_READY

    # Empty plot is empty regardless of clock.
    empty = _mk_plot()
    assert GardenService._derive_state(empty, base) == PlotState.EMPTY
    assert GardenService._derive_state(empty, _aware(base)) == PlotState.EMPTY


def test_view_handles_naive_and_aware_values_without_typeerror():
    """The exact production crash path: view() subtracting naive and aware."""
    base = datetime(2026, 9, 27, 12, 0, 0)
    plot = _mk_plot(
        planted_at=base,  # naive DB value
        mature_at=_aware(base + timedelta(hours=2)),  # aware value mixed in
        fruit_ready_at=base + timedelta(hours=4),
    )
    garden = _Garden([plot])

    # Must not raise TypeError for any combination of naive/aware inputs.
    views_naive = GardenService.view(garden, now=base + timedelta(hours=1))
    views_aware = GardenService.view(garden, now=_aware(base + timedelta(hours=1)))
    views_default = GardenService.view(garden)

    assert len(views_naive) == len(views_aware) == len(views_default) == 1
    v = views_naive[0]
    assert v.state == PlotState.GROWING
    assert v.planted_at is not None and v.planted_at.tzinfo is not None  # normalized
    assert v.mature_at is not None and v.mature_at.tzinfo is not None
    assert v.fruit_ready_at is not None and v.fruit_ready_at.tzinfo is not None
    # Deterministic countdown: 1h left until maturity.
    assert v.seconds_to_next_stage == 3600
    assert views_aware[0].seconds_to_next_stage == 3600
    assert views_aware[0].state == views_naive[0].state


def test_view_state_walk_is_deterministic():
    base = datetime(2026, 9, 27, 12, 0, 0)
    plot = _mk_plot(
        planted_at=base,
        mature_at=base + timedelta(hours=2),
        fruit_ready_at=base + timedelta(hours=5),
    )
    garden = _Garden([plot])

    timeline = [
        (base + timedelta(minutes=1), PlotState.GROWING),
        (base + timedelta(hours=2), PlotState.MATURE),
        (base + timedelta(hours=4), PlotState.MATURE),
        (base + timedelta(hours=5), PlotState.FRUIT_READY),
        (base + timedelta(hours=9), PlotState.FRUIT_READY),
    ]
    for probe, expected in timeline:
        v = GardenService.view(garden, now=probe)[0]
        assert v.state == expected, f"at {probe}"

    # Countdowns: GROWING counts to mature_at, MATURE counts to fruit_ready_at.
    assert GardenService.view(garden, now=base)[0].seconds_to_next_stage == 7200
    assert GardenService.view(garden, now=base + timedelta(hours=3))[0].seconds_to_next_stage == 7200


# ---------------------------------------------------------------------------
# 2. Full lifecycle through the service (plant -> grow -> ripe -> harvest).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_garden_lifecycle_deterministic(db_session):
    from sqlalchemy import select

    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "باغبان رگرسیون")
    await SeedlingService.admin_adjust(db_session, user.id, 2, reason="test_setup")

    garden = await GardenService.get_or_create(db_session, user.id)
    assert garden.plots, "garden must start with plots"
    plot_index = garden.plots[0].plot_index
    v = GardenService.view(garden)[0]
    assert v.state == PlotState.EMPTY

    # --- PLANT ---
    await GardenService.plant(db_session, user.id, plot_index)
    await db_session.commit()

    garden = await _fresh_garden(db_session, user.id)
    v = GardenService.view(garden)[0]
    assert v.state == PlotState.GROWING
    assert v.mature_at is not None and v.mature_at.tzinfo is not None
    assert await SeedlingService.get_total(db_session, user.id) == 1  # consumed

    # --- GROW: force maturity by shifting timestamps into the past
    # (naive values, exactly as MariaDB would return them). ---
    now = datetime.now(timezone.utc)
    plot_id = await db_session.scalar(
        select(GardenPlot.id)
        .where(GardenPlot.garden_id == user.id)
        .where(GardenPlot.plot_index == plot_index)
    )
    plot = await db_session.get(GardenPlot, plot_id)
    plot.mature_at = now.replace(tzinfo=None) - timedelta(hours=1)
    plot.fruit_ready_at = now.replace(tzinfo=None) + timedelta(hours=1)
    await db_session.commit()

    garden = await _fresh_garden(db_session, user.id)
    v = GardenService.view(garden)[0]
    assert v.state == PlotState.MATURE

    # --- RIPE ---
    plot = await db_session.get(GardenPlot, plot_id)
    plot.fruit_ready_at = now.replace(tzinfo=None) - timedelta(minutes=1)
    await db_session.commit()

    garden = await _fresh_garden(db_session, user.id)
    v = GardenService.view(garden)[0]
    assert v.state == PlotState.FRUIT_READY

    # --- HARVEST ---
    apples = await GardenService.harvest(db_session, user.id, plot_index)
    await db_session.commit()

    garden = await _fresh_garden(db_session, user.id)
    v = GardenService.view(garden)[0]
    expected_amount = CONFIG_DEFAULTS.get("garden_fruit_amount", 3)
    assert apples == expected_amount
    # Next cycle is underway deterministically.
    assert v.cycle_count == 1
    assert v.state in (PlotState.GROWING, PlotState.MATURE, PlotState.FRUIT_READY)

    # Not ripe again yet -> harvest rejected.
    with pytest.raises(ValueError):
        await GardenService.harvest(db_session, user.id, plot_index)

    # Planting on a non-empty plot is rejected.
    with pytest.raises(ValueError):
        await GardenService.plant(db_session, user.id, plot_index)


# ---------------------------------------------------------------------------
# 3. Handlers re-render the garden after EVERY action (no manual refresh).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_garden_actions_rerender_panel(db_session):
    from unittest.mock import AsyncMock, MagicMock

    from sib.handlers.garden import (
        handle_garden_expand,
        handle_garden_plant,
        handle_garden_refresh,
    )
    from sib.tests.test_parties import create_test_user

    user = await create_test_user(db_session, "باغبان رندر")
    await SeedlingService.admin_adjust(db_session, user.id, 1, reason="test_setup")
    garden = await GardenService.get_or_create(db_session, user.id)
    plot_index = garden.plots[0].plot_index
    await db_session.commit()

    def make_cb():
        cb = MagicMock()
        cb.id = "cb-garden"
        cb.data = "garden"
        cb.from_user = MagicMock()
        cb.from_user.id = user.id
        cb.from_user.is_bot = False
        cb.from_user.first_name = "باغبان رندر"
        cb.message = MagicMock()
        cb.message.chat = MagicMock()
        cb.message.chat.id = user.id
        cb.message.chat.type = "private"
        cb.message.message_id = 55
        cb.answer = AsyncMock()
        cb.edit_text = AsyncMock()
        return cb

    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)

    def rendered_text():
        """Last screen text rendered through ANY transition path.

        The garden screen is a banner screen: depending on the message it
        may be re-rendered by edit_message_text, edit_message_caption,
        edit_message_media (caption=...) or a replaced send_photo/send_message.
        The invariant under test is: the panel is ALWAYS re-rendered after
        every action, with the garden screen's own content.
        """
        calls = []
        for name in ("edit_message_text", "edit_message_caption"):
            mock = getattr(bot, name, None)
            if mock is not None and getattr(mock, "await_count", 0):
                args = mock.await_args
                text = args.args[0] if args.args else args.kwargs.get("caption") or args.kwargs.get("text")
                calls.append(text)
        for name in ("edit_message_media",):
            mock = getattr(bot, name, None)
            if mock is not None and getattr(mock, "await_count", 0):
                media = mock.await_args.kwargs.get("media")
                calls.append(getattr(media, "caption", None))
        for name in ("send_photo", "send_message"):
            mock = getattr(bot, name, None)
            if mock is not None and getattr(mock, "await_count", 0):
                args = mock.await_args
                if name == "send_photo":
                    calls.append(args.kwargs.get("caption") or (args.args[1] if len(args.args) > 1 else None))
                else:
                    calls.append(args.args[1] if len(args.args) > 1 else args.args[0])
        assert calls, "the panel must be re-rendered through some path"
        return calls[-1]

    def rerendered():
        return (
            bot.edit_message_text.called
            or bot.edit_message_caption.called
            or bot.edit_message_media.called
            or bot.send_photo.called
            or bot.send_message.called
        )

    # Plant -> rerender.
    cb = make_cb()
    cb.data = f"garden_plant:{plot_index}"
    await handle_garden_plant(cb, bot)
    assert rerendered(), "plant must re-render the panel"

    # Refresh -> rerender.
    bot.reset_mock()
    cb = make_cb()
    await handle_garden_refresh(cb, bot)
    assert rerendered(), "refresh must re-render the panel"

    # Expand -> rerender even when the action is rejected.
    bot.reset_mock()
    cb = make_cb()
    await handle_garden_expand(cb, bot)
    assert rerendered(), "expand must re-render the panel"

    # The rendered panel text contains the plot state machine vocabulary.
    text = rendered_text()
    assert any(word in text for word in ("رشد", "میوه", "برداشت", "خالی", "باغ"))
