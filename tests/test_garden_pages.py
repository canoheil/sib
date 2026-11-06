"""Garden pagination: two pages of 10 plots for capacity-20 gardens.

Pins the contract:

* capacity > 10 renders a page title («🌳 باغ من (صفحه N از ۲)») and the
  navigation row; Page 1 shows plots 1..10 with only [➡️ صفحه بعد],
  Page 2 shows plots 11..20 with only [⬅️ صفحه قبل];
* smaller gardens render exactly as before (no title, no page buttons);
* out-of-range page numbers are clamped to the valid pages;
* planting/harvesting on page-2 plots (indices 10..19) works through the
  real handlers, and the tree panel's back button returns to the plot's page.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from sqlalchemy import select

from sib.database.models import GardenPlot, PlotState
from sib.handlers.garden import _render_garden, _tree_view
from sib.services.garden import GardenService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_user


def _fa(number: int) -> str:
    from sib.services.sib_claim import fa_digits

    return fa_digits(number)


def _plot_callbacks(kb) -> list[str]:
    return [
        b.callback_data
        for row in kb.inline_keyboard
        for b in row
        if b.callback_data.startswith("garden_tree:")
    ]


def _nav_callbacks(kb) -> list[str]:
    """Only the actual «صفحه قبل / صفحه بعد» navigation buttons."""
    return [
        b.callback_data
        for row in kb.inline_keyboard
        for b in row
        if ("صفحه قبل" in b.text or "صفحه بعد" in b.text)
    ]


async def _set_capacity(db_session, user_id: int, capacity: int) -> None:
    garden = await GardenService.get_or_create(db_session, user_id)
    existing = {p.plot_index for p in garden.plots}
    for idx in range(len(existing), capacity):
        db_session.add(GardenPlot(garden_id=user_id, plot_index=idx))
    garden.capacity_plots = capacity
    await db_session.commit()


@pytest.mark.asyncio
async def test_capacity_20_page_1_shows_plots_1_to_10(db_session):
    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۱")
    await _set_capacity(db_session, user.id, 20)

    text, kb = await _render_garden(user.id, db_session, page=1)

    assert f"🌳 باغ من (صفحه {_fa(1)} از {_fa(2)})" in text
    assert _plot_callbacks(kb) == [f"garden_tree:{i}" for i in range(10)]
    assert f"قطعه {_fa(10)}" in text
    assert f"قطعه {_fa(11)}" not in text
    # Only the forward navigation button on page 1; no way backwards.
    nav = _nav_callbacks(kb)
    assert nav == ["garden_page:2"]
    next_button = [
        b
        for row in kb.inline_keyboard
        for b in row
        if b.callback_data == "garden_page:2"
    ][0]
    assert "صفحه بعد" in next_button.text


@pytest.mark.asyncio
async def test_capacity_20_page_2_shows_plots_11_to_20(db_session):
    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۲")
    await _set_capacity(db_session, user.id, 20)

    text, kb = await _render_garden(user.id, db_session, page=2)

    assert f"🌳 باغ من (صفحه {_fa(2)} از {_fa(2)})" in text
    assert _plot_callbacks(kb) == [f"garden_tree:{i}" for i in range(10, 20)]
    assert f"قطعه {_fa(11)}" in text
    assert f"قطعه {_fa(20)}" in text
    assert f"قطعه {_fa(10)}" not in text
    # Only the back navigation button on page 2; no way forwards.
    nav = _nav_callbacks(kb)
    assert nav == ["garden_page:1"]
    prev_button = [
        b
        for row in kb.inline_keyboard
        for b in row
        if b.callback_data == "garden_page:1"
    ][0]
    assert "صفحه قبل" in prev_button.text


@pytest.mark.asyncio
async def test_pages_are_rendered_on_demand_not_split_by_query(db_session):
    """Plot rows stay 0..19; pages are a pure render-time slice."""
    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۳")
    await _set_capacity(db_session, user.id, 20)
    rows = (
        (
            await db_session.scalars(
                select(GardenPlot.plot_index)
                .where(GardenPlot.garden_id == user.id)
                .order_by(GardenPlot.plot_index)
            )
        )
        .all()
    )
    assert list(rows) == list(range(20))


@pytest.mark.asyncio
async def test_small_garden_renders_as_before_without_pages(db_session):
    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۴")
    await GardenService.get_or_create(db_session, user.id)  # capacity 5
    await db_session.commit()

    text, kb = await _render_garden(user.id, db_session)

    assert "باغ من (صفحه" not in text
    assert _nav_callbacks(kb) == []
    assert "garden_refresh" in {
        b.callback_data for row in kb.inline_keyboard for b in row
    }


@pytest.mark.asyncio
async def test_capacity_10_garden_has_no_pages(db_session):
    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۵")
    await _set_capacity(db_session, user.id, 10)

    text, kb = await _render_garden(user.id, db_session, page=1)

    assert "باغ من (صفحه" not in text
    assert _plot_callbacks(kb) == [f"garden_tree:{i}" for i in range(10)]
    assert _nav_callbacks(kb) == []


@pytest.mark.asyncio
async def test_capacity_11_shows_next_on_page_1_and_prev_on_page_2(db_session):
    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۶")
    await _set_capacity(db_session, user.id, 11)

    page1_text, page1_kb = await _render_garden(user.id, db_session, page=1)
    assert f"صفحه {_fa(1)} از {_fa(2)}" in page1_text
    assert _plot_callbacks(page1_kb) == [f"garden_tree:{i}" for i in range(10)]
    assert _nav_callbacks(page1_kb) == ["garden_page:2"]

    page2_text, page2_kb = await _render_garden(user.id, db_session, page=2)
    assert f"صفحه {_fa(2)} از {_fa(2)}" in page2_text
    assert _plot_callbacks(page2_kb) == ["garden_tree:10"]
    assert _nav_callbacks(page2_kb) == ["garden_page:1"]


@pytest.mark.asyncio
async def test_out_of_range_pages_are_clamped(db_session):
    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۷")
    await _set_capacity(db_session, user.id, 15)

    clamped_up_text, clamped_up_kb = await _render_garden(user.id, db_session, page=99)
    assert f"صفحه {_fa(2)} از {_fa(2)}" in clamped_up_text
    assert _plot_callbacks(clamped_up_kb) == [
        f"garden_tree:{i}" for i in range(10, 15)
    ]

    clamped_down_text, clamped_down_kb = await _render_garden(user.id, db_session, page=0)
    assert f"صفحه {_fa(1)} از {_fa(2)}" in clamped_down_text
    assert _plot_callbacks(clamped_down_kb) == [f"garden_tree:{i}" for i in range(10)]


@pytest.mark.asyncio
async def test_tree_back_button_returns_to_the_plots_own_page(db_session):
    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۸")
    await _set_capacity(db_session, user.id, 20)

    _tree1_text, tree1_kb = await _tree_view(user.id, 3)
    tree1_backs = [
        b.callback_data
        for row in tree1_kb.inline_keyboard
        for b in row
        if b.text.endswith("بازگشت به باغ")
    ]
    assert tree1_backs == ["garden_page:1"]

    _tree2_text, tree2_kb = await _tree_view(user.id, 17)
    tree2_backs = [
        b.callback_data
        for row in tree2_kb.inline_keyboard
        for b in row
        if b.text.endswith("بازگشت به باغ")
    ]
    assert tree2_backs == ["garden_page:2"]


def _make_callback(user_id: int, data: str, first_name: str = "باغبان"):
    cb = MagicMock()
    cb.id = "cb-garden-page"
    cb.data = data
    cb.from_user = MagicMock()
    cb.from_user.id = user_id
    cb.from_user.is_bot = False
    cb.from_user.first_name = first_name
    cb.message = MagicMock()
    cb.message.chat = MagicMock()
    cb.message.chat.id = user_id
    cb.message.chat.type = "private"
    cb.message.message_id = 77
    cb.answer = AsyncMock()
    cb.edit_text = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_plant_on_plot_12_from_page_2_works(db_session):
    """``garden_plant:11`` (قطعه ۱۲, page 2) plants for real."""
    from sib.handlers.garden import handle_garden_plant

    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۹")
    await _set_capacity(db_session, user.id, 20)
    await SeedlingService.admin_adjust(db_session, user.id, 2, reason="test_setup")
    await db_session.commit()

    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)

    cb = _make_callback(user.id, "garden_plant:11", first_name="باغبان صفحه‌بندی ۹")
    await handle_garden_plant(cb, bot)

    plot = await db_session.scalar(
        select(GardenPlot).where(
            GardenPlot.garden_id == user.id, GardenPlot.plot_index == 11
        )
    )
    assert plot is not None
    assert plot.planted_at is not None  # actually planted, not just re-rendered


@pytest.mark.asyncio
async def test_harvest_on_plot_18_from_page_2_works(db_session):
    """``garden_harvest:17`` (قطعه ۱۸, page 2) harvests a ripe tree."""
    from sib.handlers.garden import handle_garden_harvest

    user = await create_test_user(db_session, "باغبان صفحه‌بندی ۱۰")
    await _set_capacity(db_session, user.id, 20)
    await SeedlingService.admin_adjust(db_session, user.id, 1, reason="test_setup")
    await db_session.commit()

    await GardenService.plant(db_session, user.id, 17)
    await db_session.commit()

    garden = await GardenService.get_or_create(db_session, user.id)
    plot = next(p for p in garden.plots if p.plot_index == 17)
    past = datetime.now(timezone.utc) - timedelta(days=5)
    plot.planted_at = past
    plot.mature_at = past + timedelta(days=1)
    plot.fruit_ready_at = past + timedelta(days=2)
    await db_session.commit()

    bot = AsyncMock()
    bot.edit_message_text = AsyncMock(return_value=True)

    cb = _make_callback(user.id, "garden_harvest:17", first_name="باغبان صفحه‌بندی ۱۰")
    await handle_garden_harvest(cb, bot)

    refreshed = await db_session.scalar(
        select(GardenPlot)
        .where(GardenPlot.garden_id == user.id, GardenPlot.plot_index == 17)
        .execution_options(populate_existing=True)
    )
    assert refreshed is not None
    assert refreshed.cycle_count == 1  # the harvest really happened
