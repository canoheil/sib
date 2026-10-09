"""Daily Sib Treasure Chest inside groups: one shared post, presser-only rewards.

The group main menu posts a dedicated closed-chest message in the same group
(committed 1:1 artwork, text fallback). Its open button is public by design:
every member who presses it receives THEIR OWN once-per-UTC-day reward through
the existing DailyChestService, announced in the group after the commit.
Refusals (already claimed, expired, malformed, disabled, failed) are private
alerts. The private-chat chest flow is covered at the end and stays unchanged.
"""

import asyncio
import json
import struct
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Chat as TelegramChat, FSInputFile, Message, User as TelegramUser
from sqlalchemy import select

from sib.database.models import DailyChestClaim
from sib.database.models.daily_chest import REWARD_APPLES, REWARD_CARD
from sib.database.models.panel import PanelOwner
from sib.handlers import daily_chest as chest
from sib.keyboards.common import get_group_welcome_keyboard, get_private_welcome_keyboard
from sib.middleware.panel_guard import PanelGuardMiddleware
from sib.services import marriage_art
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.daily_chest import ChestClaimResult, DailyChestService, utc_day
from sib.services.panel import is_owner_scoped_callback
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
    private = [b.callback_data for b in _buttons(get_private_welcome_keyboard("sib_bot"))]
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
async def test_posted_photo_is_cached_by_file_id_for_later_posts(db_session):
    member = await create_test_user(db_session, "کش")
    bot = _bot()
    bot.send_photo = AsyncMock(
        return_value=MagicMock(message_id=POST_MESSAGE_ID, photo=[MagicMock(file_id="FILE-CHEST")])
    )

    await chest.handle_group_chest(_callback(member, "group_chest"), bot)
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
    assert "۱۵ تا سیب معمولی" in notices[0]


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
    assert "باز شدن بعدی" in text
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
    assert _alert(open_cb) == ("صندوقچه گنج روزانه فعال نیست. 🎁", True)
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

    assert notice.startswith("🎁 [ali\\_\\*x](tg://user?id=4242)")
    assert "۱۵ تا سیب معمولی" in notice


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
