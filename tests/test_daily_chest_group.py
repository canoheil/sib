"""Daily Sib Treasure Chest inside groups: one shared post, presser-only rewards.

The group main menu posts a dedicated closed-chest message in the same group
(committed 1:1 artwork, text fallback). Its open button is public by design:
every member who presses it receives THEIR OWN once-per-UTC-day reward through
the existing DailyChestService, announced in the group after the commit.
Refusals (already claimed, expired, malformed, disabled, failed) are private
alerts. The private-chat chest flow is covered at the end and stays unchanged.
"""

import asyncio
import functools
import json
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    FSInputFile,
    Message,
    MessageEntity,
    User as TelegramUser,
)
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from sib.database.connection import get_session
from sib.database.models import DailyChestClaim, SystemConfig
from sib.database.models.daily_chest import REWARD_APPLES, REWARD_CARD
from sib.database.models.panel import PanelOwner
from sib.handlers import daily_chest as chest
from sib.keyboards.common import get_group_welcome_keyboard, get_private_start_keyboard
from sib.middleware.panel_guard import PanelGuardMiddleware
from sib.services import daily_chest as daily_chest_service
from sib.services import marriage_art
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.daily_chest import ChestClaimResult, DailyChestService, GroupSlotContention, utc_day
from sib.services.panel import is_owner_scoped_callback
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import CAPTION_LIMIT, caption_length
from sib.tests.conftest import requires_row_locks
from sib.tests.test_daily_chest import StubRng
from sib.tests.test_parties import create_test_user

GROUP_ID = -1009850
GROUP_TITLE = "گروه صندوقچه"
MENU_MESSAGE_ID = 9850
POST_MESSAGE_ID = 9851
# Pinned UTC clock: every "today" in this module is 2026-10-09.
FIXED_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
TODAY = "2026-10-09"
YESTERDAY = "2026-10-08"
OPEN_DATA = f"group_chest:open:{TODAY}"
OPEN_LABEL = "🔓 باز کردن صندوقچه"

ASSETS_DIR = Path(marriage_art.__file__).resolve().parent.parent / "assets"

MALFORMED_CALLBACKS = [
    "group_chest:open",
    "group_chest:open:",
    "group_chest:open:2026-10",
    "group_chest:open:2026-10-9",
    "group_chest:open:2026-02-30",
    "group_chest:open:2026-10-09:extra",
    "group_chest:open:2026-10-09\n",
    "group_chest:open:۲۰۲۶-۱۰-۰۹",  # Persian digits are not canonical
    "group_chest:close:2026-10-09",
    "group_chest_extra",
]


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(chest, "_utc_now", lambda: FIXED_NOW)


@pytest.fixture(autouse=True)
def _clear_scene_cache():
    marriage_art._file_id_cache.clear()
    yield
    marriage_art._file_id_cache.clear()


@pytest.fixture
def fixed_reward(monkeypatch):
    """Every open rolls 15 Apples, through the real claim path (deterministic)."""
    original_claim = DailyChestService.claim

    async def claim_15_apples(session, user_id, *, rng=None, now=None):
        return await original_claim(
            session, user_id, rng=StubRng(REWARD_APPLES, quantity=15), now=now
        )

    monkeypatch.setattr(DailyChestService, "claim", claim_15_apples)


@pytest.fixture
def clock(monkeypatch):
    """Move the pinned chest clock forward inside one test (cooldown windows)."""
    state = {"now": FIXED_NOW}
    monkeypatch.setattr(chest, "_utc_now", lambda: state["now"])

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return SimpleNamespace(advance=advance)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _callback(user, data, *, chat_type="supergroup", chat_id=GROUP_ID, message_id=POST_MESSAGE_ID):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = (
        None
        if user is None
        else TelegramUser(id=user.id, is_bot=False, first_name=user.first_name)
    )
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type=chat_type, title=GROUP_TITLE)
    cb.message.message_id = message_id
    cb.answer = AsyncMock()
    return cb


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=POST_MESSAGE_ID, photo=None))
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=POST_MESSAGE_ID + 1, photo=None))
    bot.edit_message_text = AsyncMock(return_value=True)
    return bot


def _buttons(kb):
    return [button for row in kb.inline_keyboard for button in row]


async def _menu_press(bot, user) -> str:
    """Press the group menu button; return the posted open button's data."""
    await chest.handle_group_chest(
        _callback(user, "group_chest", message_id=MENU_MESSAGE_ID), bot
    )
    return _buttons(bot.send_photo.call_args.kwargs["reply_markup"])[0].callback_data


async def _open(bot, user, data=OPEN_DATA):
    cb = _callback(user, data)
    await chest.handle_group_chest(cb, bot)
    return cb


def _alert(cb):
    """(text, show_alert) of the single answer this callback received."""
    call = cb.answer.await_args
    return call.args[0], call.kwargs.get("show_alert", False)


def _notices(bot) -> list:
    return [call.args[1] for call in bot.send_message.await_args_list]


def _telegram_refusal(message: str) -> TelegramBadRequest:
    """A real aiogram API error, as Telegram returns for a refused or stale call."""
    return TelegramBadRequest(method=MagicMock(), message=message)


async def _claims(session) -> list:
    await session.commit()  # fresh snapshot: the handler committed in its own session
    return list((await session.scalars(select(DailyChestClaim))).all())


async def _apples(session, user_id: int) -> int:
    await session.commit()
    return await AppleWalletService.get_balance(session, user_id)


def _jpeg_dimensions(path: Path) -> tuple:
    """(width, height) from the JPEG frame header, stdlib only (as in test_sib_hug)."""
    data = path.read_bytes()
    assert data[:3] == b"\xff\xd8\xff", "not a JPEG"
    assert data[-2:] == b"\xff\xd9", "JPEG end-of-image marker missing"
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    raise AssertionError("no JPEG frame header found")


# ---------------------------------------------------------------------------
# 1. Group main menu entry and public callback contract
# ---------------------------------------------------------------------------
def test_group_main_menu_offers_the_chest_next_to_the_private_entry_point():
    group = [b for b in _buttons(get_group_welcome_keyboard()) if b.callback_data == "group_chest"]
    assert len(group) == 1
    assert group[0].text == "🎁 صندوقچه گنج روزانه"
    private = [b.callback_data for b in _buttons(get_private_start_keyboard("sib_bot"))]
    assert "apple_chest" in private  # the private entry point is untouched


def test_chest_callbacks_are_public_while_the_private_panel_stays_owner_scoped():
    assert not is_owner_scoped_callback("group_chest")
    assert not is_owner_scoped_callback(OPEN_DATA)
    assert is_owner_scoped_callback("daily_chest:open")
    assert utc_day(FIXED_NOW) == TODAY


def test_callback_parser_accepts_only_the_canonical_forms():
    assert chest.parse_group_chest_callback("group_chest") == ("menu", None)
    assert chest.parse_group_chest_callback(OPEN_DATA) == ("open", TODAY)
    assert chest.parse_group_chest_callback("group_chest:open:2024-02-29") == (
        "open",
        "2024-02-29",
    )
    for bad in [None, "", "group_chest:", "group_chest:open", "group_chest:open:2025-02-29"]:
        assert chest.parse_group_chest_callback(bad) is None, bad
    for bad in MALFORMED_CALLBACKS:
        assert chest.parse_group_chest_callback(bad) is None, bad


@pytest.mark.asyncio
async def test_group_chest_callbacks_reach_only_the_chest_handler():
    """Production routing: no other router may capture the group chest callbacks."""

    async def reached_by(data: str) -> list:
        event = SimpleNamespace(data=data, from_user=None, message=None)
        return await _reached_by("callback_query", event)

    assert await reached_by(chest.GROUP_CHEST_CALLBACK) == ["handle_group_chest"]
    assert await reached_by(OPEN_DATA) == ["handle_group_chest"]
    # The private entry points keep their own handlers.
    assert await reached_by("apple_chest") == ["handle_apple_chest"]
    assert await reached_by("daily_chest:open") == ["handle_chest_open"]


# ---------------------------------------------------------------------------
# 2. Selecting the button shows the closed chest in the same group
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_menu_press_posts_the_closed_chest_in_the_same_group(db_session):
    member = await create_test_user(db_session, "منو")
    bot = _bot()
    cb = _callback(member, "group_chest", message_id=MENU_MESSAGE_ID)

    await chest.handle_group_chest(cb, bot)

    assert bot.send_photo.await_count == 1
    assert not bot.send_message.called  # no private redirect, no text fallback
    args, kwargs = bot.send_photo.call_args
    assert args[0] == GROUP_ID  # the same group the menu was pressed in
    assert isinstance(args[1], FSInputFile)
    assert Path(str(args[1].path)).name == "chest.jpg"  # the committed artwork
    assert kwargs["caption"] == chest.GROUP_CHEST_TEXT
    assert chest.CHEST_TITLE in kwargs["caption"]
    buttons = _buttons(kwargs["reply_markup"])
    assert [(b.text, b.callback_data) for b in buttons] == [(OPEN_LABEL, OPEN_DATA)]
    assert _alert(cb) == (None, False)  # plain acknowledgement, no alert
    assert await _claims(db_session) == []  # posting never grants anything


@pytest.mark.asyncio
async def test_post_falls_back_to_text_with_the_same_button_when_photo_fails(db_session):
    member = await create_test_user(db_session, "عکس‌نرفت")
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=Exception("upload refused"))

    cb = _callback(member, "group_chest", message_id=MENU_MESSAGE_ID)
    await chest.handle_group_chest(cb, bot)

    args, kwargs = bot.send_message.call_args
    assert args[0] == GROUP_ID
    assert args[1] == chest.GROUP_CHEST_TEXT
    assert [b.callback_data for b in _buttons(kwargs["reply_markup"])] == [OPEN_DATA]
    assert _alert(cb) == (None, False)


@pytest.mark.asyncio
async def test_post_without_artwork_is_a_text_post_that_still_opens(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)  # artwork absent
    member = await create_test_user(db_session, "بی‌عکس")
    bot = _bot()

    await chest.handle_group_chest(_callback(member, "group_chest"), bot)

    assert not bot.send_photo.called
    args, kwargs = bot.send_message.call_args
    assert args[1] == chest.GROUP_CHEST_TEXT
    assert [b.callback_data for b in _buttons(kwargs["reply_markup"])] == [OPEN_DATA]


@pytest.mark.asyncio
async def test_post_that_cannot_be_sent_at_all_gets_a_generic_alert(db_session):
    member = await create_test_user(db_session, "ارسال‌نشد")
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=_telegram_refusal("upload refused"))
    bot.send_message = AsyncMock(side_effect=_telegram_refusal("chat unavailable"))

    cb = _callback(member, "group_chest", message_id=MENU_MESSAGE_ID)
    await chest.handle_group_chest(cb, bot)  # must not raise

    assert _alert(cb) == (chest.CHEST_FAILURE_TEXT, True)  # no Telegram internals
    assert await _claims(db_session) == []


@pytest.mark.asyncio
async def test_posted_photo_is_cached_by_file_id_for_later_posts(db_session, clock):
    member = await create_test_user(db_session, "کش")
    bot = _bot()
    bot.send_photo = AsyncMock(
        return_value=MagicMock(message_id=POST_MESSAGE_ID, photo=[MagicMock(file_id="FILE-CHEST")])
    )

    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
    clock.advance(30)  # the second post in this group waits out the 30 s cooldown
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)

    assert bot.send_photo.call_args_list[0].args[1] != "FILE-CHEST"  # first: the file
    assert bot.send_photo.call_args_list[1].args[1] == "FILE-CHEST"  # then: cached id


@pytest.mark.asyncio
async def test_chest_is_group_only_and_refuses_private_chats(db_session):
    bot = _bot()
    private_user = SimpleNamespace(id=7, first_name="خصوصی")
    for data in ("group_chest", OPEN_DATA):
        cb = _callback(private_user, data, chat_type="private", chat_id=7)
        await chest.handle_group_chest(cb, bot)
        assert _alert(cb) == (chest.GROUP_ONLY_TEXT, True)
    assert not bot.send_photo.called and not bot.send_message.called


def test_committed_chest_artwork_is_a_square_jpeg_registered_for_group_posts():
    expected = ASSETS_DIR / "daily_chest" / "chest.jpg"
    assert marriage_art.GROUP_SCENES["daily_chest"] == "daily_chest/chest.jpg"
    assert marriage_art.scene_path("daily_chest") == expected
    assert expected.is_file()
    assert _jpeg_dimensions(expected) == (1024, 1024)  # 1:1
    assert 0 < expected.stat().st_size <= 5 * 1024 * 1024


def test_chest_manifest_records_the_committed_artwork():
    manifest = json.loads((ASSETS_DIR / "daily_chest" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["technical"]["aspect"] == "1:1"
    assert manifest["technical"]["dimensions"] == "1024x1024"
    assert [(scene["key"], scene["file"]) for scene in manifest["scenes"]] == [
        ("daily_chest", "chest.jpg")
    ]


def test_chest_post_caption_fits_one_telegram_caption():
    assert caption_length(chest.GROUP_CHEST_TEXT) <= CAPTION_LIMIT


def test_plain_text_chest_messages_do_not_expose_markdown():
    plain_texts = (
        chest.GROUP_CHEST_TEXT,
        chest.CHEST_DISABLED_TEXT,
        chest.CHEST_CONFIG_ERROR_TEXT,
        chest.CHEST_FAILURE_TEXT,
        chest.OPEN_TOAST,
        chest.ALREADY_TOAST,
        chest.GROUP_ONLY_TEXT,
        chest.GROUP_INVALID_TEXT,
        chest.GROUP_EXPIRED_TEXT,
        chest._group_cooldown_text(30),
        chest.format_next_reset(chest.next_reset_at(FIXED_NOW), FIXED_NOW),
    )
    assert all("**" not in text for text in plain_texts)


# ---------------------------------------------------------------------------
# 3. The presser receives the reward (not the user who opened the post)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_presser_receives_the_reward_and_the_opener_gets_nothing(db_session, fixed_reward):
    opener = await create_test_user(db_session, "بازکننده")
    presser = await create_test_user(db_session, "فشاردهنده")
    bot = _bot()
    open_data = await _menu_press(bot, opener)

    cb = await _open(bot, presser, open_data)

    claims = await _claims(db_session)
    assert [(c.user_id, c.reward_type, c.quantity) for c in claims] == [
        (presser.id, REWARD_APPLES, 15)
    ]
    assert await _apples(db_session, presser.id) == 15
    assert await _apples(db_session, opener.id) == 0
    assert _alert(cb) == (chest.OPEN_TOAST, False)
    notices = _notices(bot)
    assert len(notices) == 1
    assert f"tg://user?id={presser.id}" in notices[0]  # the group sees who won
    assert f"tg://user?id={opener.id}" not in notices[0]
    assert "**۱۵ سیب معمولی**" in notices[0]


@pytest.mark.asyncio
async def test_repeated_presses_by_one_member_pay_exactly_once(db_session, fixed_reward):
    member = await create_test_user(db_session, "پرکلیک")
    bot = _bot()
    open_data = await _menu_press(bot, member)

    for _ in range(3):
        await _open(bot, member, open_data)

    claims = await _claims(db_session)
    assert len(claims) == 1 and claims[0].user_id == member.id
    assert await _apples(db_session, member.id) == 15
    assert len(_notices(bot)) == 1  # one public reward line, never three


# ---------------------------------------------------------------------------
# 4. A member who already claimed today cannot claim again
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_already_claimed_member_is_refused_privately_with_the_next_reset(db_session, fixed_reward):
    member = await create_test_user(db_session, "دوباره‌زن")
    bot = _bot()
    open_data = await _menu_press(bot, member)
    await _open(bot, member, open_data)
    bot.send_message.reset_mock()

    cb = await _open(bot, member, open_data)

    text, show_alert = _alert(cb)
    assert show_alert is True
    assert text.startswith(chest.ALREADY_TOAST)
    assert "صندوق بعدی" in text
    assert len(text) <= 200  # Telegram's alert limit
    assert not bot.send_message.called  # a refusal is never announced in the group
    assert await _apples(db_session, member.id) == 15


# ---------------------------------------------------------------------------
# 5. Two different members claim independently from one post
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_members_claim_their_own_rewards_from_one_post(db_session, fixed_reward):
    first = await create_test_user(db_session, "عضو اول")
    second = await create_test_user(db_session, "عضو دوم")
    bot = _bot()
    open_data = await _menu_press(bot, first)

    await _open(bot, first, open_data)
    await _open(bot, second, open_data)

    claims = await _claims(db_session)
    assert sorted(c.user_id for c in claims) == sorted([first.id, second.id])
    assert await _apples(db_session, first.id) == 15
    assert await _apples(db_session, second.id) == 15
    notices = _notices(bot)
    assert len(notices) == 2
    assert f"tg://user?id={first.id}" in notices[0]
    assert f"tg://user?id={second.id}" in notices[1]


# ---------------------------------------------------------------------------
# 6. Concurrent and replayed presses cannot duplicate a reward
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stale_eligibility_read_cannot_pay_twice_from_the_post(db_session, fixed_reward, monkeypatch):
    member = await create_test_user(db_session, "نخ‌کش")
    bot = _bot()
    open_data = await _menu_press(bot, member)
    await _open(bot, member, open_data)  # the one real claim
    bot.send_message.reset_mock()

    async def no_claim_seen(*args, **kwargs):
        return None  # a lying eligibility check: the database gate must still hold

    monkeypatch.setattr(DailyChestService, "get_today_claim", no_claim_seen)
    cb = await _open(bot, member, open_data)

    text, show_alert = _alert(cb)
    assert show_alert is True and text.startswith(chest.ALREADY_TOAST)
    assert len(await _claims(db_session)) == 1
    assert await _apples(db_session, member.id) == 15
    assert not bot.send_message.called


@pytest.mark.asyncio
async def test_simultaneous_presses_pay_at_most_once_on_any_engine(db_session, fixed_reward):
    """Four simultaneous presses, each on its own database connection.

    SQLite queues the writers on its database lock here; MySQL/MariaDB queues
    them on the per-user row lock (see the row-lock test below). Either way one
    press commits the day's claim and every other press is answered
    already-claimed, so the member gets exactly one reward.
    """
    member = await create_test_user(db_session, "همزمان‌سیب")
    open_data = await _menu_press(_bot(), member)

    async def press():
        await chest.handle_group_chest(_callback(member, open_data), _bot())

    await asyncio.gather(*(press() for _ in range(4)))

    claims = await _claims(db_session)
    assert len(claims) == 1
    assert await _apples(db_session, member.id) == 15


@pytest.mark.asyncio
@requires_row_locks
async def test_concurrent_presses_from_one_member_pay_exactly_once(db_session, fixed_reward):
    """True concurrency (MySQL/MariaDB row locks): simultaneous presses, one reward."""
    member = await create_test_user(db_session, "همزمان")
    open_data = await _menu_press(_bot(), member)

    async def press():
        await chest.handle_group_chest(_callback(member, open_data), _bot())

    await asyncio.gather(press(), press(), press())

    claims = await _claims(db_session)
    assert len(claims) == 1
    assert await _apples(db_session, member.id) == 15


# ---------------------------------------------------------------------------
# 7. Expired, malformed and invalid callbacks are refused safely
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_post_from_an_earlier_utc_day_is_expired_and_grants_nothing(db_session, fixed_reward):
    member = await create_test_user(db_session, "دیروزی")
    bot = _bot()

    cb = await _open(bot, member, f"group_chest:open:{YESTERDAY}")

    assert _alert(cb) == (chest.GROUP_EXPIRED_TEXT, True)
    assert await _claims(db_session) == []
    assert await _apples(db_session, member.id) == 0
    assert not bot.send_message.called


@pytest.mark.asyncio
@pytest.mark.parametrize("data", MALFORMED_CALLBACKS)
async def test_malformed_group_callbacks_are_refused_without_writes(db_session, fixed_reward, data):
    member = await create_test_user(db_session, "ناقص")
    bot = _bot()

    cb = await _open(bot, member, data)

    assert _alert(cb) == (chest.GROUP_INVALID_TEXT, True)
    assert await _claims(db_session) == []
    assert not bot.send_message.called and not bot.send_photo.called


@pytest.mark.asyncio
async def test_open_press_without_a_user_is_refused(db_session):
    bot = _bot()
    cb = _callback(None, OPEN_DATA)

    await chest.handle_group_chest(cb, bot)

    assert _alert(cb) == (chest.GROUP_INVALID_TEXT, True)
    assert not bot.send_message.called


# ---------------------------------------------------------------------------
# 8. Configuration, failures and presentation never leak or roll back rewards
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_disabled_chest_posts_nothing_and_refuses_opens(db_session, fixed_reward):
    member = await create_test_user(db_session, "خاموش")
    await ConfigService.set(db_session, "daily_chest_enabled", False)
    await db_session.commit()
    bot = _bot()

    menu_cb = _callback(member, "group_chest")
    await chest.handle_group_chest(menu_cb, bot)
    assert _alert(menu_cb) == (chest.CHEST_DISABLED_TEXT, True)
    assert not bot.send_photo.called and not bot.send_message.called

    open_cb = await _open(bot, member, OPEN_DATA)
    assert _alert(open_cb) == ("🎁 صندوقچه‌ی امروز فعلاً بسته‌ست!", True)
    assert await _claims(db_session) == []


@pytest.mark.asyncio
async def test_broken_reward_pool_fails_with_a_safe_alert(db_session):
    member = await create_test_user(db_session, "پیکربندی‌خراب")
    await ConfigService.set(
        db_session, "daily_chest_weights", {"apples": -5, "unknown_reward": 3}
    )
    await db_session.commit()
    bot = _bot()

    menu_cb = _callback(member, "group_chest")
    await chest.handle_group_chest(menu_cb, bot)
    assert _alert(menu_cb) == (chest.CHEST_CONFIG_ERROR_TEXT, True)
    assert not bot.send_photo.called

    open_cb = await _open(bot, member, OPEN_DATA)
    assert _alert(open_cb) == (chest.CHEST_CONFIG_ERROR_TEXT, True)
    assert await _claims(db_session) == []


@pytest.mark.asyncio
async def test_unexpected_failure_shows_a_generic_alert_without_internals(db_session, monkeypatch):
    member = await create_test_user(db_session, "خطادیده")
    bot = _bot()

    async def broken_claim(*args, **kwargs):
        raise RuntimeError("SECRET-DB-DETAIL: deadlock on daily_chest_claims")

    monkeypatch.setattr(DailyChestService, "claim", broken_claim)
    cb = await _open(bot, member, OPEN_DATA)

    text, show_alert = _alert(cb)
    assert (text, show_alert) == (chest.CHEST_FAILURE_TEXT, True)
    assert "SECRET" not in text and "deadlock" not in text
    assert not bot.send_message.called
    assert await _claims(db_session) == []


@pytest.mark.asyncio
async def test_a_refused_callback_answer_never_costs_or_doubles_the_reward(db_session, fixed_reward):
    member = await create_test_user(db_session, "پاسخ‌نرفت")
    bot = _bot()
    open_data = await _menu_press(bot, member)
    cb = _callback(member, open_data)
    cb.answer = AsyncMock(side_effect=_telegram_refusal("query is too old"))

    await chest.handle_group_chest(cb, bot)  # must not raise

    assert cb.answer.await_count == 1  # the refused answer really happened
    assert len(await _claims(db_session)) == 1
    assert await _apples(db_session, member.id) == 15
    assert len(_notices(bot)) == 1  # the public line still goes out


@pytest.mark.asyncio
async def test_failed_group_notice_never_undoes_the_committed_reward(db_session, fixed_reward):
    member = await create_test_user(db_session, "اعلان‌نرفت")
    bot = _bot()
    open_data = await _menu_press(bot, member)
    bot.send_message = AsyncMock(side_effect=Exception("chat unavailable"))

    cb = await _open(bot, member, open_data)

    assert _alert(cb) == (chest.OPEN_TOAST, False)
    assert len(await _claims(db_session)) == 1
    assert await _apples(db_session, member.id) == 15


@pytest.mark.asyncio
async def test_the_post_is_shared_and_the_panel_guard_never_locks_it(db_session, fixed_reward):
    opener = await create_test_user(db_session, "صاحب‌پست")
    presser = await create_test_user(db_session, "عضو‌دیگر")
    bot = _bot()
    open_data = await _menu_press(bot, opener)

    async def through_guard(event):
        called = []

        async def handler(evt, data):
            called.append(evt)
            return "HANDLED"

        result = await PanelGuardMiddleware()(handler, event, {})
        return result, called

    for user in (opener, presser):
        result, called = await through_guard(_callback(user, open_data))
        assert result == "HANDLED" and called, user.id

    owners = (
        await db_session.scalars(
            select(PanelOwner).where(PanelOwner.message_id == POST_MESSAGE_ID)
        )
    ).all()
    assert owners == []  # the shared post is never claimed by anyone


# ---------------------------------------------------------------------------
# 9. Public reward line: presser named, Markdown-safe, card-aware
# ---------------------------------------------------------------------------
def test_group_notice_names_the_presser_and_escapes_the_display_name():
    presser = SimpleNamespace(id=4242, first_name="ali_*x")
    result = ChestClaimResult(
        status="claimed", next_reset=FIXED_NOW, reward_type=REWARD_APPLES, quantity=15
    )

    notice = chest._group_reward_notice(presser, result)

    assert notice.startswith("🎁 [\u200eali\\_\\*x\u200e](tg://user?id=4242)")
    assert "**۱۵ سیب معمولی**" in notice


def test_jackpot_notice_shows_the_granted_card_for_the_group():
    presser = SimpleNamespace(id=5, first_name="سارا")
    character = SimpleNamespace(name_fa="سیب سبز", power=10, speed=6, intelligence=4)
    result = ChestClaimResult(
        status="claimed",
        next_reset=FIXED_NOW,
        reward_type=REWARD_CARD,
        quantity=1,
        character=character,
        card=SimpleNamespace(level=2),
    )

    notice = chest._group_reward_notice(presser, result)

    assert "جکپات" in notice
    assert "**سیب سبز**" in notice
    assert "سطح **۲**" in notice
    assert "—" not in notice


# ---------------------------------------------------------------------------
# 10. The private-chat chest flow is unchanged
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_private_chest_flow_is_unchanged(db_session, fixed_reward):
    member = await create_test_user(db_session, "خصوصی‌کار")
    bot = _bot()

    menu_cb = _callback(member, "apple_chest", chat_type="private", chat_id=member.id, message_id=501)
    await chest.handle_apple_chest(menu_cb, bot)

    assert bot.edit_message_text.await_args.args[0] == chest.CHEST_AVAILABLE_TEXT
    kb = bot.edit_message_text.await_args.kwargs["reply_markup"]
    assert [b.callback_data for b in _buttons(kb)] == ["daily_chest:open", "main_menu"]
    assert not bot.send_photo.called  # the private chest stays text-only

    open_cb = _callback(member, "daily_chest:open", chat_type="private", chat_id=member.id, message_id=501)
    await chest.handle_chest_open(open_cb, bot)

    claims = await _claims(db_session)
    assert [(c.user_id, c.quantity) for c in claims] == [(member.id, 15)]
    assert await _apples(db_session, member.id) == 15
    assert _alert(open_cb) == (chest.OPEN_TOAST, False)


@pytest.mark.asyncio
async def test_private_open_survives_a_refused_screen_edit(db_session, fixed_reward):
    member = await create_test_user(db_session, "ویرایش‌نرفت‌خصوصی")
    bot = _bot()
    menu_cb = _callback(member, "apple_chest", chat_type="private", chat_id=member.id, message_id=503)
    await chest.handle_apple_chest(menu_cb, bot)  # binds the panel to this member
    bot.edit_message_text = AsyncMock(side_effect=_telegram_refusal("message to edit not found"))

    open_cb = _callback(member, "daily_chest:open", chat_type="private", chat_id=member.id, message_id=503)
    await chest.handle_chest_open(open_cb, bot)  # must not raise

    assert bot.edit_message_text.await_count >= 1  # the refused edit really happened
    claims = await _claims(db_session)
    assert [(c.user_id, c.quantity) for c in claims] == [(member.id, 15)]
    assert await _apples(db_session, member.id) == 15
    assert _alert(open_cb) == (chest.OPEN_TOAST, False)


# ---------------------------------------------------------------------------
# Shared helpers for the anti-spam and shortcut sections
# ---------------------------------------------------------------------------
def _text(user, text: str, *, chat_type: str = "supergroup", chat_id: int = GROUP_ID) -> MagicMock:
    """A text message from ``user`` as the handler receives it; ``reply`` is recorded."""
    message = MagicMock(spec=Message)
    message.text = text
    message.message_id = 7001
    message.from_user = TelegramUser(id=user.id, is_bot=False, first_name=user.first_name)
    message.chat = TelegramChat(
        id=chat_id, type=chat_type, title=None if chat_type == "private" else GROUP_TITLE
    )
    message.reply = AsyncMock()
    return message


def _message_event(text: str, *, chat_type: str = "supergroup", entities=None) -> Message:
    """A real aiogram Message, used only to evaluate dispatch filters."""
    return Message(
        message_id=7001,
        date=FIXED_NOW,
        chat=TelegramChat(
            id=GROUP_ID, type=chat_type, title=None if chat_type == "private" else GROUP_TITLE
        ),
        from_user=TelegramUser(id=777, is_bot=False, first_name="عضو"),
        text=text,
        entities=entities,
    )


def _slot_key(chat_id: int = GROUP_ID) -> str:
    return f"group_chest_post:{chat_id}"


async def _slot_value(session, chat_id: int = GROUP_ID):
    await session.commit()  # fresh snapshot: the handler committed in its own session
    return await session.scalar(
        select(SystemConfig.value).where(SystemConfig.key == _slot_key(chat_id))
    )


async def _write_slot(session, raw: str, chat_id: int = GROUP_ID) -> None:
    session.add(SystemConfig(key=_slot_key(chat_id), value=raw))
    await session.commit()


async def _set_setting(session, key: str, value) -> None:
    await ConfigService.set(session, key, value)
    await session.commit()


def _window_text(seconds: int) -> str:
    return chest.GROUP_COOLDOWN_TEXT.format(seconds=fa_digits(seconds))


@functools.lru_cache(maxsize=1)
def _included_routers() -> tuple:
    """Top-level routers ``register_all_handlers`` includes, in include order.

    Recorded through a patched ``include_router``: aiogram refuses to attach one
    router to two dispatchers, so nothing is attached here.
    """
    from aiogram import Dispatcher

    from sib.handlers import register_all_handlers

    included: list = []
    with patch.object(Dispatcher, "include_router", side_effect=included.append):
        register_all_handlers(Dispatcher())
    return tuple(included)


def _walk(router):
    yield router
    for sub in router.sub_routers:
        yield from _walk(sub)


async def _reached_by(observer: str, event) -> list:
    """Names of the handlers matching ``event`` in dispatch order (first one wins).

    ``observer`` is the aiogram event name: ``"message"`` or ``"callback_query"``.
    Filters run through aiogram's own ``FilterObject.call``, so async filters
    (such as ``Command``) are awaited and receive only the arguments they declare.
    """
    names = []
    for root in _included_routers():
        for router in _walk(root):
            for handler in getattr(router, observer).handlers:
                matched = True
                try:
                    for flt in handler.filters:
                        if not await flt.call(event, bot=None):
                            matched = False
                            break
                except Exception:  # a filter that needs more context cannot match
                    matched = False
                if matched:
                    names.append(handler.callback.__name__)
    return names


# ---------------------------------------------------------------------------
# 10. Anti-spam: one interface per group per cooldown, shared by both entries
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_menu_press_inside_the_cooldown_posts_nothing_and_answers_privately(db_session, clock):
    member = await create_test_user(db_session, "صف")
    bot = _bot()
    await chest.handle_group_chest(_callback(member, "group_chest", message_id=MENU_MESSAGE_ID), bot)
    clock.advance(29)

    cb = _callback(member, "group_chest", message_id=MENU_MESSAGE_ID)
    await chest.handle_group_chest(cb, bot)

    assert bot.send_photo.await_count == 1
    assert bot.send_message.await_count == 0
    assert _alert(cb) == (_window_text(1), False)  # a private toast, never a group message


@pytest.mark.asyncio
async def test_the_cooldown_lasts_exactly_thirty_seconds(db_session, clock):
    member = await create_test_user(db_session, "مرز")
    bot = _bot()
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)

    clock.advance(29.999)
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
    assert bot.send_photo.await_count == 1  # one millisecond still to wait

    clock.advance(0.001)
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
    assert bot.send_photo.await_count == 2  # the window has passed


@pytest.mark.asyncio
async def test_shortcut_and_menu_button_share_one_cooldown(db_session, clock):
    member = await create_test_user(db_session, "هم‌زمان")
    bot = _bot()
    await chest.handle_chest_shortcut(_text(member, "سیب صندوق"), bot)
    assert bot.send_photo.await_count == 1

    clock.advance(5)
    cb = _callback(member, "group_chest")
    await chest.handle_group_chest(cb, bot)  # menu inside the window: refused privately
    assert _alert(cb) == (_window_text(25), False)
    assert bot.send_photo.await_count == 1

    clock.advance(5)
    first_refusal = _text(member, "سیب صندوق")
    await chest.handle_chest_shortcut(first_refusal, bot)  # shortcut: refused and voiced once
    first_refusal.reply.assert_awaited_once_with(_window_text(20))
    assert bot.send_photo.await_count == 1

    clock.advance(3)
    repeat = _text(member, "سیب صندوق")
    await chest.handle_chest_shortcut(repeat, bot)  # same window: already voiced, so silent
    repeat.reply.assert_not_awaited()
    assert bot.send_photo.await_count == 1


@pytest.mark.asyncio
async def test_a_new_window_voices_its_first_refusal_again(db_session, clock):
    member = await create_test_user(db_session, "پنجره")
    bot = _bot()
    await chest.handle_chest_shortcut(_text(member, "سیب صندوق"), bot)  # posts at t=0

    clock.advance(30)
    await chest.handle_chest_shortcut(_text(member, "سیب صندوق"), bot)  # posts at t=30
    assert bot.send_photo.await_count == 2

    clock.advance(10)
    refused = _text(member, "سیب صندوق")
    await chest.handle_chest_shortcut(refused, bot)  # first refusal of the new window
    refused.reply.assert_awaited_once_with(_window_text(20))


@pytest.mark.asyncio
async def test_each_group_has_its_own_cooldown(db_session):
    member = await create_test_user(db_session, "دو گروه")
    bot = _bot()
    await chest.handle_chest_shortcut(_text(member, "سیب صندوق", chat_id=GROUP_ID), bot)
    await chest.handle_group_chest(_callback(member, "group_chest", chat_id=GROUP_ID + 1), bot)

    assert [call.args[0] for call in bot.send_photo.await_args_list] == [GROUP_ID, GROUP_ID + 1]


@pytest.mark.asyncio
async def test_simultaneous_menu_and_shortcut_presses_post_exactly_one_interface(db_session):
    members = [await create_test_user(db_session, f"هم{i}") for i in range(6)]
    await db_session.commit()
    bot = _bot()
    calls = []
    for index, member in enumerate(members):
        if index % 2:
            calls.append(chest.handle_group_chest(_callback(member, "group_chest"), bot))
        else:
            calls.append(chest.handle_chest_shortcut(_text(member, "سیب صندوق"), bot))

    await asyncio.gather(*calls)

    assert bot.send_photo.await_count == 1
    assert bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_two_contenders_on_the_same_expired_slot_cannot_both_win(
    db_session, clock, monkeypatch
):
    member = await create_test_user(db_session, "اول")
    bot = _bot()
    await chest.handle_chest_shortcut(_text(member, "سیب صندوق"), bot)  # posts at t=0
    clock.advance(31)
    await db_session.commit()
    now = FIXED_NOW + timedelta(seconds=31)

    original_swap = daily_chest_service._swap_group_post
    both_read = asyncio.Event()
    reads = []

    async def rendezvous_swap(session, key, expected, new):
        reads.append(expected)
        if len(reads) == 2:
            both_read.set()
        await both_read.wait()  # neither swaps until both have read the expired slot
        return await original_swap(session, key, expected, new)

    monkeypatch.setattr(daily_chest_service, "_swap_group_post", rendezvous_swap)

    async def contend():
        try:
            async with get_session() as session:
                return await DailyChestService.reserve_group_post(
                    session, GROUP_ID, now=now, notify=False
                )
        except GroupSlotContention as error:
            return error

    outcomes = await asyncio.gather(contend(), contend())

    assert sorted(type(outcome).__name__ for outcome in outcomes) == [
        "GroupPostSlot",
        "GroupSlotContention",
    ]
    assert json.loads(await _slot_value(db_session))["posted_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_failed_photo_with_text_fallback_still_holds_the_cooldown(db_session, clock):
    member = await create_test_user(db_session, "فال‌بک")
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=Exception("upload refused"))
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
    assert bot.send_message.await_count == 1  # the text fallback is the post

    clock.advance(5)
    cb = _callback(member, "group_chest")
    await chest.handle_group_chest(cb, bot)
    assert bot.send_message.await_count == 1  # still one interface in the window
    assert _alert(cb)[1] is False


@pytest.mark.asyncio
async def test_when_nothing_can_be_sent_the_slot_is_freed_for_the_next_press(db_session):
    member = await create_test_user(db_session, "رها")
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=_telegram_refusal("gone"))
    bot.send_message = AsyncMock(side_effect=_telegram_refusal("gone too"))
    cb = _callback(member, "group_chest", message_id=MENU_MESSAGE_ID)
    await chest.handle_group_chest(cb, bot)

    assert _alert(cb) == (chest.CHEST_FAILURE_TEXT, True)
    assert await _slot_value(db_session) is None  # nothing was posted, so nothing is held

    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=POST_MESSAGE_ID, photo=None))
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)  # same instant
    assert bot.send_photo.await_count == 1  # Telegram recovered: the group was never locked


@pytest.mark.asyncio
async def test_an_unexpected_send_error_also_frees_the_slot(db_session, monkeypatch):
    member = await create_test_user(db_session, "خطای‌ناشناس")
    bot = _bot()
    original_send = chest.safe_send_screen
    monkeypatch.setattr(chest, "safe_send_screen", AsyncMock(side_effect=RuntimeError("boom")))
    cb = _callback(member, "group_chest", message_id=MENU_MESSAGE_ID)
    await chest.handle_group_chest(cb, bot)  # must not raise

    assert _alert(cb) == (chest.CHEST_FAILURE_TEXT, True)
    assert await _slot_value(db_session) is None  # nothing reached the group, so nothing is held

    monkeypatch.setattr(chest, "safe_send_screen", original_send)
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)  # same instant
    assert bot.send_photo.await_count == 1


@pytest.mark.asyncio
async def test_shortcut_that_cannot_post_replies_generically_and_frees_the_slot(db_session):
    member = await create_test_user(db_session, "میانبر‌خراب")
    bot = _bot()
    bot.send_photo = AsyncMock(side_effect=_telegram_refusal("gone"))
    bot.send_message = AsyncMock(side_effect=_telegram_refusal("gone too"))
    message = _text(member, "سیب صندوق")

    await chest.handle_chest_shortcut(message, bot)

    message.reply.assert_awaited_once_with(chest.CHEST_FAILURE_TEXT)
    assert await _slot_value(db_session) is None


@pytest.mark.asyncio
async def test_a_transient_database_error_is_retried_and_does_not_block_the_post(
    db_session, monkeypatch
):
    member = await create_test_user(db_session, "تکرار")
    bot = _bot()
    real_reserve = DailyChestService.reserve_group_post
    attempts = []

    async def flaky_reserve(session, chat_id, **kwargs):
        attempts.append(chat_id)
        if len(attempts) == 1:
            raise OperationalError("SELECT", {}, Exception("database is locked"))
        return await real_reserve(session, chat_id, **kwargs)

    monkeypatch.setattr(DailyChestService, "reserve_group_post", flaky_reserve)

    await chest.handle_chest_shortcut(_text(member, "سیب صندوق"), bot)

    assert len(attempts) == 2
    assert bot.send_photo.await_count == 1


@pytest.mark.asyncio
async def test_a_reservation_that_keeps_failing_fails_safely_without_posting(
    db_session, monkeypatch
):
    member = await create_test_user(db_session, "قفل")
    bot = _bot()

    async def always_locked(session, chat_id, **kwargs):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(DailyChestService, "reserve_group_post", always_locked)
    cb = _callback(member, "group_chest")

    await chest.handle_group_chest(cb, bot)

    assert bot.send_photo.await_count == 0
    assert _alert(cb) == (chest.CHEST_FAILURE_TEXT, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"posted_at": "soon"}',
        '{"posted_at": "2026-10-09T12:00:00"}',  # no timezone: unreadable
        '{"posted_at": "2026-10-09T13:00:00+00:00"}',  # an hour ahead: not a real post
    ],
)
async def test_an_unreadable_or_far_future_slot_never_locks_a_group(db_session, raw):
    member = await create_test_user(db_session, "قفل‌نشو")
    await _write_slot(db_session, raw)
    bot = _bot()

    await chest.handle_group_chest(_callback(member, "group_chest"), bot)

    assert bot.send_photo.await_count == 1


@pytest.mark.asyncio
async def test_a_slot_a_few_seconds_ahead_of_the_clock_still_holds_the_group(db_session):
    member = await create_test_user(db_session, "کمی‌جلوتر")
    ahead = (FIXED_NOW + timedelta(seconds=10)).isoformat()
    await _write_slot(db_session, json.dumps({"posted_at": ahead, "notified": False}))
    bot = _bot()
    cb = _callback(member, "group_chest")

    await chest.handle_group_chest(cb, bot)

    assert bot.send_photo.await_count == 0  # clock skew cannot open a second window
    assert _alert(cb) == (_window_text(30), False)


@pytest.mark.asyncio
async def test_the_cooldown_length_comes_from_configuration(db_session, clock):
    member = await create_test_user(db_session, "تنظیم")
    await _set_setting(db_session, "group_chest_cooldown_seconds", 5)
    bot = _bot()
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)

    clock.advance(4)
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
    assert bot.send_photo.await_count == 1

    clock.advance(1)
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
    assert bot.send_photo.await_count == 2


@pytest.mark.asyncio
async def test_a_zero_cooldown_setting_means_no_spacing(db_session):
    member = await create_test_user(db_session, "بی‌فاصله")
    await _set_setting(db_session, "group_chest_cooldown_seconds", 0)
    bot = _bot()

    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)

    assert bot.send_photo.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", ["soon", -5])
async def test_an_invalid_cooldown_setting_falls_back_to_the_default(db_session, clock, bad_value):
    member = await create_test_user(db_session, "تنظیم‌نادرست")
    await _set_setting(db_session, "group_chest_cooldown_seconds", bad_value)
    bot = _bot()
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)

    clock.advance(29)
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
    assert bot.send_photo.await_count == 1  # still inside the default 30 s window

    clock.advance(1)
    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
    assert bot.send_photo.await_count == 2  # a bad value never switches the guard off


@pytest.mark.asyncio
async def test_release_only_frees_the_reservation_it_made(db_session):
    async with get_session() as session:
        slot = await DailyChestService.reserve_group_post(
            session, GROUP_ID, now=FIXED_NOW, notify=False
        )
    assert slot.status == daily_chest_service.GROUP_POST_RESERVED

    async with get_session() as session:  # some other reservation's token: not ours
        assert not await DailyChestService.release_group_post(session, GROUP_ID, "other")
    assert await _slot_value(db_session) is not None

    async with get_session() as session:
        assert await DailyChestService.release_group_post(session, GROUP_ID, slot.token)
    assert await _slot_value(db_session) is None


@pytest.mark.asyncio
async def test_open_stays_open_to_every_member_during_the_cooldown(db_session, clock, fixed_reward):
    opener = await create_test_user(db_session, "صاحب")
    first = await create_test_user(db_session, "اول‌باز")
    second = await create_test_user(db_session, "دوم‌باز")
    bot = _bot()
    open_data = await _menu_press(bot, opener)

    clock.advance(5)
    await chest.handle_chest_shortcut(_text(first, "سیب صندوق"), bot)  # a new post is refused
    assert bot.send_photo.await_count == 1

    for member in (first, second):  # ...but the existing post stays open for everyone
        cb = await _open(bot, member, open_data)
        assert _alert(cb)[0] == chest.OPEN_TOAST
    claims = await _claims(db_session)
    assert sorted(claim.user_id for claim in claims) == sorted([first.id, second.id])


@pytest.mark.asyncio
async def test_a_shortcut_reply_that_telegram_refuses_never_raises(db_session):
    member = await create_test_user(db_session, "پاسخ‌نرفت")
    bot = _bot()
    await chest.handle_chest_shortcut(_text(member, "سیب صندوق"), bot)  # posts
    refused = _text(member, "سیب صندوق")
    refused.reply.side_effect = _telegram_refusal("not allowed to reply")

    await chest.handle_chest_shortcut(refused, bot)  # cooldown refusal; the reply fails

    assert bot.send_photo.await_count == 1


# ---------------------------------------------------------------------------
# 11. «سیب صندوق»: exact phrase, same group flow, private chest in private
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shortcut_in_a_group_posts_the_same_interface_as_the_menu(db_session):
    member = await create_test_user(db_session, "میانبر")
    bot = _bot()
    message = _text(member, "سیب صندوق")

    await chest.handle_chest_shortcut(message, bot)

    assert bot.send_photo.await_count == 1
    assert bot.send_photo.await_args.args[0] == GROUP_ID
    buttons = _buttons(bot.send_photo.await_args.kwargs["reply_markup"])
    assert [(button.text, button.callback_data) for button in buttons] == [(OPEN_LABEL, OPEN_DATA)]
    message.reply.assert_not_awaited()  # the post itself is the answer


@pytest.mark.asyncio
async def test_shortcut_in_private_chat_opens_the_private_chest_like_the_command(
    db_session, fixed_reward
):
    member = await create_test_user(db_session, "خصوصی")
    bot = _bot()
    shortcut = _text(member, "سیب صندوق", chat_type="private", chat_id=member.id)
    command = _text(member, "/chest", chat_type="private", chat_id=member.id)

    await chest.handle_chest_shortcut(shortcut, bot)
    await chest.cmd_chest(command)

    bot.send_photo.assert_not_awaited()  # no group post in a private chat
    shortcut.reply.assert_awaited_once()
    command.reply.assert_awaited_once()
    assert shortcut.reply.await_args == command.reply.await_args  # same screen and keyboard
    open_buttons = [b.callback_data for b in _buttons(shortcut.reply.await_args.kwargs["reply_markup"])]
    assert "daily_chest:open" in open_buttons


@pytest.mark.asyncio
async def test_disabled_chest_answers_the_shortcut_without_posting(db_session):
    member = await create_test_user(db_session, "بسته")
    await _set_setting(db_session, "daily_chest_enabled", False)
    bot = _bot()
    message = _text(member, "سیب صندوق")

    await chest.handle_chest_shortcut(message, bot)

    bot.send_photo.assert_not_awaited()
    message.reply.assert_awaited_once_with(chest.CHEST_DISABLED_TEXT)
    assert await _slot_value(db_session) is None  # a disabled chest reserves nothing


@pytest.mark.parametrize(
    "text",
    [
        "سیب صندوق",
        "سيب صندوق",  # Arabic yeh
        "  سیب صندوق  ",
        "\tسيب   صندوق\n",
    ],
)
@pytest.mark.asyncio
async def test_shortcut_phrase_variants_reach_the_shortcut_first(text):
    for chat_type in ("group", "supergroup", "private"):
        event = _message_event(text, chat_type=chat_type)
        assert (await _reached_by("message", event))[0] == "handle_chest_shortcut", (text, chat_type)


@pytest.mark.parametrize(
    "text",
    [
        "سیب صندوقچه",  # a different word
        "امروز سیب صندوق رو باز کردم",  # the phrase inside a longer sentence
        "سیب صندوق خوبه",
        "صندوق سیب",
        "سیب",  # the wake word stays with start
        "سیب بازی",
        "بغل سیبی",
    ],
)
@pytest.mark.asyncio
async def test_longer_or_unrelated_messages_do_not_open_the_shortcut(text):
    assert "handle_chest_shortcut" not in await _reached_by("message", _message_event(text))


@pytest.mark.asyncio
async def test_existing_text_and_command_triggers_keep_their_handlers():
    command = _message_event(
        "/chest", entities=[MessageEntity(type="bot_command", offset=0, length=6)]
    )
    assert (await _reached_by("message", command))[0] == "cmd_chest"
    assert (await _reached_by("message", _message_event("بغل سیبی")))[0] == "handle_sib_hug"
    assert (await _reached_by("message", _message_event("بوس سیبی")))[0] == "handle_sib_kiss"


@pytest.mark.asyncio
async def test_a_refusal_during_a_failing_post_does_not_hold_the_group(db_session):
    """A refusal voiced while a post is still in flight must not keep that post's slot.

    The refused request marks the window as notified mid-send. When the send then
    fails, the release still matches the reservation by its token and frees it.
    """
    member = await create_test_user(db_session, "در‌حال‌ارسال")
    refused = _text(member, "سیب صندوق")
    bot = _bot()

    async def photo_fails_while_another_request_arrives(*args, **kwargs):
        await chest.handle_chest_shortcut(refused, bot)  # arrives during the send
        raise _telegram_refusal("gone")

    bot.send_photo = AsyncMock(side_effect=photo_fails_while_another_request_arrives)
    bot.send_message = AsyncMock(side_effect=_telegram_refusal("gone too"))

    await chest.handle_chest_shortcut(_text(member, "سیب صندوق"), bot)

    refused.reply.assert_awaited_once_with(_window_text(30))  # voiced once, mid-send
    assert await _slot_value(db_session) is None  # the failed post freed its own slot


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_shortcut_and_menu_serve_basic_groups_and_supergroups_alike(db_session, chat_type):
    member = await create_test_user(db_session, "گروه‌ساده")
    bot = _bot()

    await chest.handle_chest_shortcut(_text(member, "سیب صندوق", chat_type=chat_type), bot)
    cb = _callback(member, "group_chest", chat_type=chat_type)
    await chest.handle_group_chest(cb, bot)

    assert bot.send_photo.await_count == 1  # one interface, shared by both entries
    assert bot.send_photo.await_args.args[0] == GROUP_ID
    assert _alert(cb) == (_window_text(30), False)  # the menu press is refused privately
