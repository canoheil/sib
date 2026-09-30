"""Special-apple drop captions (task requirement #3).

Rules pinned here:
- the drop probabilities are UNCHANGED (level 0 = 70/20/9/1);
- every special apple has its own celebration caption on the EXISTING
  image;
- the caption escalates with rarity: Golden (20%) < Diamond (9%) <
  Obsidian (1%);
- captions come from configuration (the existing localization surface),
  not from hardcoded handler strings.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, Message, User as TelegramUser

from sib.database.models import PlotState
from sib.handlers.garden import handle_garden_harvest, handle_garden_harvest_all
from sib.services.config_store import CONFIG_DEFAULTS, ConfigService
from sib.services.garden import GardenService, HarvestOutcome
from sib.services.items import DropService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_user

SPECIAL_KEYS = ("apple_gold", "apple_diamond", "apple_obsidian")


# ---------------------------------------------------------------------------
# Probabilities are untouched
# ---------------------------------------------------------------------------


def test_level0_drop_weights_unchanged():
    weights = CONFIG_DEFAULTS["garden_drop_weights"]["0"]
    assert weights == {
        "apple_normal": 70,
        "apple_gold": 20,
        "apple_diamond": 9,
        "apple_obsidian": 1,
    }


def test_every_special_outcome_has_its_own_caption_and_asset():
    entries = CONFIG_DEFAULTS["garden_drop_entries"]
    captions = {}
    assets = {}
    for key in SPECIAL_KEYS:
        entry = entries[key]
        assert entry.get("asset"), f"{key} must keep its celebration image"
        caption = entry.get("caption")
        assert caption and caption.strip(), f"{key} needs a celebration caption"
        captions[key] = caption
        assets[key] = entry["asset"]

    # Distinct captions AND distinct images: each apple is its own event.
    assert len(set(captions.values())) == 3
    assert len(set(assets.values())) == 3
    assert assets == {
        "apple_gold": "golden_apple",
        "apple_diamond": "diamond_apple",
        "apple_obsidian": "obsidian_apple",
    }


def test_captions_escalate_with_rarity():
    entries = CONFIG_DEFAULTS["garden_drop_entries"]
    gold = entries["apple_gold"]["caption"]
    diamond = entries["apple_diamond"]["caption"]
    obsidian = entries["apple_obsidian"]["caption"]

    # Each caption names its own apple ...
    assert "طلایی" in gold
    assert "الماسی" in diamond
    assert "ابسیدینی" in obsidian

    # ... and they escalate: gold is luck, diamond is rare, obsidian is
    # legendary (an event that is practically once-in-a-lifetime).
    assert "شانس" in gold
    assert "کمیاب" in diamond
    assert "افسانه" in obsidian and "هزار" in obsidian
    assert len(set([gold, diamond, obsidian])) == 3


# ---------------------------------------------------------------------------
# Wiring: the caption travels with the image
# ---------------------------------------------------------------------------


class _FixedRng:
    def __init__(self, value: float):
        self._value = value

    def random(self):
        return self._value


async def _ripe_garden(db_session, name: str):
    user = await create_test_user(db_session, name)
    await SeedlingService.admin_adjust(db_session, user.id, 2, reason="test")
    garden = await GardenService.get_or_create(db_session, user.id)
    for index in (0, 1):
        await GardenService.plant(db_session, user.id, index)
    await db_session.commit()
    past = datetime.now(timezone.utc) - timedelta(days=5)
    for plot in garden.plots[:2]:
        plot.planted_at = past
        plot.mature_at = past + timedelta(days=1)
        plot.fruit_ready_at = past + timedelta(days=2)
    await db_session.commit()
    return user, garden


def _callback(user, data: str) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name or "باغبان")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=user.id, type="private")
    cb.message.message_id = 9
    cb.message.text = "باغ"
    cb.message.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.send_photo = AsyncMock(return_value=True)
    return bot


@pytest.mark.asyncio
async def test_single_harvest_sends_caption_with_the_image(db_session):
    user, _garden = await _ripe_garden(db_session, "باغبان کپشن")
    bot = _bot()
    cb = _callback(user, "garden_harvest:0")

    # Force the Obsidian tail of the table.
    with patch.object(GardenService, "harvest", autospec=True) as mocked:
        entry = CONFIG_DEFAULTS["garden_drop_entries"]["apple_obsidian"]
        mocked.return_value = HarvestOutcome(
            plot_index=0,
            magic_level=0,
            item_key="apple_obsidian",
            item_label="سیب ابسیدینی",
            message=entry["message"],
            caption=entry["caption"],
            asset=entry["asset"],
        )
        await handle_garden_harvest(cb, bot)

    captions = [
        call.kwargs.get("caption")
        for call in bot.send_photo.call_args_list
        if call.kwargs.get("caption")
    ]
    expected = CONFIG_DEFAULTS["garden_drop_entries"]["apple_obsidian"]["caption"]
    assert expected in captions


@pytest.mark.asyncio
async def test_real_harvest_carries_the_configured_caption(db_session):
    """The service itself attaches the caption (not only the handler)."""
    user, _garden = await _ripe_garden(db_session, "باغبان سرویس")
    outcome = await GardenService.harvest(db_session, user.id, 0, rng=_FixedRng(0.999))
    await db_session.commit()

    assert outcome.item_key == "apple_obsidian"
    entry = CONFIG_DEFAULTS["garden_drop_entries"]["apple_obsidian"]
    assert outcome.caption == entry["caption"]
    assert outcome.asset == "obsidian_apple"


@pytest.mark.asyncio
async def test_harvest_all_sends_each_special_caption(db_session):
    user, _garden = await _ripe_garden(db_session, "باغبان دسته‌ای")
    bot = _bot()
    cb = _callback(user, "garden_harvest_all")

    with patch.object(GardenService, "harvest_all", autospec=True) as mocked:
        gold = CONFIG_DEFAULTS["garden_drop_entries"]["apple_gold"]
        diamond = CONFIG_DEFAULTS["garden_drop_entries"]["apple_diamond"]
        mocked.return_value = [
            HarvestOutcome(
                plot_index=0, magic_level=0, item_key="apple_gold",
                item_label="سیب طلایی", message=gold["message"],
                caption=gold["caption"], asset=gold["asset"],
            ),
            HarvestOutcome(
                plot_index=1, magic_level=0, item_key="apple_diamond",
                item_label="سیب الماسی", message=diamond["message"],
                caption=diamond["caption"], asset=diamond["asset"],
            ),
        ]
        await handle_garden_harvest_all(cb, bot)

    captions = [
        call.kwargs.get("caption")
        for call in bot.send_photo.call_args_list
        if call.kwargs.get("caption")
    ]
    entries = CONFIG_DEFAULTS["garden_drop_entries"]
    assert entries["apple_gold"]["caption"] in captions
    assert entries["apple_diamond"]["caption"] in captions
    # Both special apples were celebrated, each with its own image.
    photos = [call.kwargs.get("photo") for call in bot.send_photo.call_args_list]
    assert len(photos) >= 2


@pytest.mark.asyncio
async def test_drop_config_stays_extensible_with_captions(db_session):
    """A new outcome can still be added as config only (caption included)."""
    await ConfigService.set(
        db_session,
        "garden_drop_weights",
        {"0": {"apple_normal": 99, "apple_obsidian": 1}},
    )
    await ConfigService.set(
        db_session,
        "garden_drop_entries",
        {
            "apple_normal": {"message": "n"},
            "apple_obsidian": {
                "message": "m",
                "caption": "custom caption",
                "asset": "obsidian_apple",
            },
        },
    )
    await db_session.commit()

    key, entry = await DropService.roll(db_session, 0, rng=_FixedRng(0.999))
    assert key == "apple_obsidian"
    assert entry["caption"] == "custom caption"
