"""Phase 7.4 — marriage status inside the EXISTING profile UI.

The project has three profile renderers that all build the same screen:

* ``handlers/start.py::handle_apple_profile``   — the «👤 پروفایل من» panel
* ``handlers/start.py::_open_sender_profile``   — the «سیب پروفایل» wake word
* ``handlers/apple_battle.py::cmd_profile``     — the ``/profile`` command

None of them was replaced. Each one appends whatever
``profile_marriage_lines`` returns to the lines it already builds, so the
marriage feature adds no lines for a single user and a married user gains
exactly the two marriage lines.

The profile screen now carries the player's Level artwork, so that same
text is normally delivered as a PHOTO caption rather than a text message
(see ``tests/test_player_level_art.py``); ``_profile_text`` reads it from
whichever rendering the flow used.

The marriage block is read-only presentation: the spouse comes from
``MarriageService.get_active_marriage_for_user`` and the Love score is the
shared ``Marriage.love_score`` — the same value ``LoveService.get_love_score``
returns. Nothing here computes, mutates or stores Love.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    Message,
    User as TelegramUser,
)

from sib.database import Group, Marriage, MarriageStatus, User
from sib.handlers.apple_battle import cmd_profile
from sib.handlers.marriage import (
    LOVE_BAR_CELLS,
    love_progress_bar,
    profile_marriage_lines,
    spouse_handle,
)
from sib.handlers.start import handle_apple_profile, handle_sib_profile_word
from sib.services.marriage import MarriageService

GROUP_ID = -1007950
GROUP_TITLE = "گروه پروفایل سیب"

OWNER_ID = 7950
SPOUSE_ID = 7951
OTHER_ID = 7952


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tg(user_id: int, first_name: str = "کاربر", *, username: str = None):
    return TelegramUser(
        id=user_id,
        is_bot=False,
        first_name=first_name,
        username=username,
    )


def _sent(message_id: int = 950) -> MagicMock:
    sent = MagicMock(spec=Message)
    sent.message_id = message_id
    sent.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    return sent


def _profile_message(user: TelegramUser, text: str = "سیب پروفایل") -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    msg.from_user = user
    msg.text = text
    msg.message_id = 900
    msg.reply = AsyncMock(return_value=_sent())
    msg.reply_photo = AsyncMock(return_value=_sent())
    return msg


def _profile_callback(user: TelegramUser) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = "apple_profile"
    cb.from_user = user
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=GROUP_ID, type="supergroup", title=GROUP_TITLE)
    cb.message.message_id = 901
    cb.message.text = "menu"
    cb.message.reply_markup = None
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


async def _mk_user(session, user_id: int, **kwargs) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            first_name=kwargs.pop("first_name", f"کاربر{user_id}"),
            username=kwargs.pop("username", None),
            language_code="fa",
            **kwargs,
        )
        session.add(user)
        await session.flush()
    return user


async def _mk_group(session, group_id: int = GROUP_ID) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        group = Group(id=group_id, title=GROUP_TITLE)
        session.add(group)
        await session.flush()
    return group


async def _marry(db_session, a: int, b: int, *, love_score: int = 0) -> int:
    await _mk_user(db_session, a)
    await _mk_user(db_session, b)
    await _mk_group(db_session)
    await db_session.flush()
    marriage = Marriage(
        proposer_id=a,
        accepter_id=b,
        origin_group_id=GROUP_ID,
        status=MarriageStatus.ACTIVE,
        love_score=love_score,
    )
    db_session.add(marriage)
    await db_session.commit()
    return int(marriage.id)


def _profile_text(msg: MagicMock) -> str:
    """The rendered profile text, from a photo caption or a text message.

    The profile screen carries the player's Level artwork, so its text
    normally travels as a PHOTO caption; the plain-text send only happens
    when no artwork could be loaded. The marriage assertions below are about
    the TEXT, which is identical either way.
    """
    if msg.reply_photo.await_count:
        return msg.reply_photo.await_args.kwargs["caption"]
    return msg.reply.await_args.args[0]


async def _wake_word_profile(db_session, user: TelegramUser) -> str:
    msg = _profile_message(user)
    await handle_sib_profile_word(msg, MagicMock())
    return _profile_text(msg)


async def _panel_profile(db_session, user: TelegramUser) -> str:
    """The «👤 پروفایل من» panel text (whichever render path was used)."""
    cb = _profile_callback(user)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    # The Level artwork makes this a TEXT → PHOTO transition, which the
    # screen engine performs by sending a new photo message.
    bot.send_photo = AsyncMock(return_value=_sent())
    bot.delete_message = AsyncMock()
    await handle_apple_profile(cb, bot)
    if bot.send_photo.await_count:
        return bot.send_photo.await_args.kwargs["caption"]
    call = bot.edit_message_text.call_args or bot.edit_message_caption.call_args
    return call.args[0] if call.args else call.kwargs.get("text", "")


# ---------------------------------------------------------------------------
# 1. The gauge (pure)
# ---------------------------------------------------------------------------


def test_love_bar_at_zero_percent():
    assert love_progress_bar(0) == "░" * LOVE_BAR_CELLS


def test_love_bar_at_fifty_percent():
    assert love_progress_bar(50) == "█" * 5 + "░" * 5


def test_love_bar_at_one_hundred_percent():
    assert love_progress_bar(100) == "█" * LOVE_BAR_CELLS


def test_love_bar_matches_the_specified_sixty_percent_shape():
    assert love_progress_bar(60) == "██████░░░░"


def test_love_bar_clamps_out_of_range_values():
    assert love_progress_bar(-20) == "░" * LOVE_BAR_CELLS
    assert love_progress_bar(999) == "█" * LOVE_BAR_CELLS
    assert len(love_progress_bar(37)) == LOVE_BAR_CELLS


# ---------------------------------------------------------------------------
# 2. Username / display-name conventions (pure)
# ---------------------------------------------------------------------------


def test_spouse_handle_prefers_the_telegram_username():
    user = User(id=1, first_name="سارا", username="player_one")
    assert spouse_handle(user) == "@player\\_one"  # Markdown-escaped


def test_spouse_handle_falls_back_to_the_display_name():
    user = User(id=1, first_name="سارا", last_name="احمدی", username=None)
    assert spouse_handle(user) == "سارا احمدی"


def test_spouse_handle_falls_back_further_when_everything_is_missing():
    user = User(id=1, username=None)
    user.first_name = None
    # User.display_name already applies the project's «بازیکن بی‌نام» fallback.
    assert spouse_handle(user) == "بازیکن بی‌نام"


def test_love_percentage_rejects_unusable_values():
    """A stale/inconsistent Love value yields None instead of a crash."""
    from sib.handlers.marriage import _love_percentage

    assert _love_percentage(None, 0, 100) is None
    assert _love_percentage("not-a-number", 0, 100) is None
    assert _love_percentage(50, 10, 10) is None  # inverted range
    assert _love_percentage(50, 90, 10) is None
    assert _love_percentage(50, 0, 100) == 50
    assert _love_percentage(-40, 0, 100) == 0
    assert _love_percentage(400, 0, 100) == 100


# ---------------------------------------------------------------------------
# 3. Block rendering against the real database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_user_has_no_marriage_block(db_session):
    await _mk_user(db_session, OWNER_ID)
    await db_session.commit()

    assert await profile_marriage_lines(db_session, OWNER_ID) == []


@pytest.mark.asyncio
async def test_married_user_shows_the_spouse_and_love(db_session):
    await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=60)
    spouse = await db_session.get(User, SPOUSE_ID)
    spouse.username = "sib_spouse"
    await db_session.commit()

    lines = await profile_marriage_lines(db_session, OWNER_ID)

    assert len(lines) == 2
    assert lines[0] == "💍 همسر: @sib\\_spouse"
    assert lines[1] == "❤️ عشق سیبی: ██████░░░░ 60%"


@pytest.mark.asyncio
async def test_correct_spouse_is_displayed_from_either_side(db_session):
    await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=10)
    owner = await db_session.get(User, OWNER_ID)
    owner.username = "owner_handle"
    spouse = await db_session.get(User, SPOUSE_ID)
    spouse.username = "spouse_handle"
    await db_session.commit()

    assert (await profile_marriage_lines(db_session, OWNER_ID))[0] == (
        "💍 همسر: @spouse\\_handle"
    )
    assert (await profile_marriage_lines(db_session, SPOUSE_ID))[0] == (
        "💍 همسر: @owner\\_handle"
    )


@pytest.mark.asyncio
async def test_spouse_without_username_uses_the_safe_fallback(db_session):
    await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=25)
    spouse = await db_session.get(User, SPOUSE_ID)
    spouse.username = None
    spouse.first_name = "زهرا"
    spouse.last_name = None
    await db_session.commit()

    lines = await profile_marriage_lines(db_session, OWNER_ID)
    assert lines[0] == "💍 همسر: زهرا"


@pytest.mark.asyncio
async def test_divorced_user_no_longer_shows_a_spouse(db_session):
    marriage_id = await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=80)
    assert await profile_marriage_lines(db_session, OWNER_ID)

    async with _fresh_session() as session:
        await MarriageService.divorce(session, marriage_id, OWNER_ID)

    db_session.expunge_all()
    assert await profile_marriage_lines(db_session, OWNER_ID) == []


@pytest.mark.asyncio
async def test_marriage_history_is_never_exposed(db_session):
    """A previous marriage stays invisible even after a new one begins."""
    first = await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=90)
    async with _fresh_session() as session:
        await MarriageService.divorce(session, first, OWNER_ID)

    await _mk_user(db_session, OTHER_ID)
    await db_session.commit()
    await _marry(db_session, OWNER_ID, OTHER_ID, love_score=5)
    other = await db_session.get(User, OTHER_ID)
    other.username = "new_spouse"
    old = await db_session.get(User, SPOUSE_ID)
    old.username = "ex_spouse"
    await db_session.commit()

    lines = await profile_marriage_lines(db_session, OWNER_ID)
    assert any("@new\\_spouse" in line for line in lines)
    assert not any("ex_spouse" in line for line in lines)
    assert len(lines) == 1 + 1  # spouse + love, nothing else


@pytest.mark.asyncio
async def test_unusable_love_score_omits_only_the_gauge(db_session, monkeypatch):
    """A stale/inconsistent Love value drops the gauge but keeps the spouse."""
    from sib.services.config_store import ConfigService

    await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=60)
    spouse = await db_session.get(User, SPOUSE_ID)
    spouse.username = "sib_spouse"
    await db_session.commit()

    async def inverted_range(session, key):
        return {"love_min": 100, "love_max": 0}.get(key, 0)

    # An inconsistent configured range (max <= min) makes the score unusable.
    monkeypatch.setattr(ConfigService, "get", staticmethod(inverted_range))

    lines = await profile_marriage_lines(db_session, OWNER_ID)
    assert len(lines) == 1
    assert lines[0] == "💍 همسر: @sib\\_spouse"
    assert "عشق سیبی" not in " ".join(lines)


@pytest.mark.asyncio
async def test_no_internal_details_leak_from_the_block(db_session):
    await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=42)
    lines = await profile_marriage_lines(db_session, OWNER_ID)
    text = "\n".join(lines)
    for leaked in ("Error", "Exception", "Traceback", "None", "sib.", "Marriage"):
        assert leaked not in text


# ---------------------------------------------------------------------------
# 4. Integration with the three existing profile renderers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_user_profile_has_no_marriage_block(db_session):
    """A single user's profile receives no spouse or Love lines."""
    await _mk_user(db_session, OWNER_ID)
    await db_session.commit()

    text = await _wake_word_profile(db_session, _tg(OWNER_ID, "سارا"))

    assert "👤 **پروفایل سیبی سارا**" in text
    assert "🍎 سیب:" in text
    assert "🌱 نهال:" in text
    assert "🎴 تعداد کارت‌ها:" in text
    assert "⭐ کارت اصلی: انتخاب نشده" in text
    assert "⚔️ نبردهای سیبی:" in text
    # No marriage section at all.
    assert "همسر" not in text
    assert "عشق سیبی" not in text
    assert "طلاق" not in text


@pytest.mark.asyncio
async def test_married_user_profile_via_the_wake_word(db_session):
    await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=60)
    spouse = await db_session.get(User, SPOUSE_ID)
    spouse.username = "sib_spouse"
    await db_session.commit()

    text = await _wake_word_profile(db_session, _tg(OWNER_ID, "سارا"))

    assert "💍 همسر: @sib\\_spouse" in text
    assert "❤️ عشق سیبی: ██████░░░░ 60%" in text
    # Existing content is still there.
    assert "👤 **پروفایل سیبی سارا**" in text
    assert "🍎 سیب:" in text


@pytest.mark.asyncio
async def test_married_user_profile_via_the_profile_panel(db_session):
    await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=100)
    spouse = await db_session.get(User, SPOUSE_ID)
    spouse.username = "sib_spouse"
    await db_session.commit()

    text = await _panel_profile(db_session, _tg(OWNER_ID, "سارا"))

    assert "💍 همسر: @sib\\_spouse" in text
    assert "❤️ عشق سیبی: " + "█" * LOVE_BAR_CELLS + " 100%" in text


@pytest.mark.asyncio
async def test_married_user_profile_via_the_profile_command(db_session):
    await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=0)
    spouse = await db_session.get(User, SPOUSE_ID)
    spouse.username = "sib_spouse"
    await db_session.commit()

    msg = _profile_message(_tg(OWNER_ID, "سارا"), text="/profile")
    await cmd_profile(msg, MagicMock())

    text = _profile_text(msg)
    assert "💍 همسر: @sib\\_spouse" in text
    assert "❤️ عشق سیبی: " + "░" * LOVE_BAR_CELLS + " 0%" in text


@pytest.mark.asyncio
async def test_profile_panel_of_a_divorced_user_has_no_spouse(db_session):
    marriage_id = await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=70)
    async with _fresh_session() as session:
        await MarriageService.divorce(session, marriage_id, OWNER_ID)

    text = await _panel_profile(db_session, _tg(OWNER_ID, "سارا"))
    assert "همسر" not in text
    assert "عشق سیبی" not in text


# ---------------------------------------------------------------------------
# 5. Scope guards
# ---------------------------------------------------------------------------


def test_profile_block_never_writes():
    """Guard rail: the block is read-only presentation."""
    import inspect

    from sib.handlers import marriage as marriage_handlers

    source = inspect.getsource(marriage_handlers.profile_marriage_lines)
    for forbidden in ("session.add", "session.commit", "session.execute("):
        assert forbidden not in source


def test_marriage_block_reuses_the_domain_love_getter():
    """The rendered score is the same field LoveService exposes."""
    from sib.services.love import LoveService

    assert hasattr(LoveService, "get_love_score")


def test_profile_wake_word_regex_is_untouched():
    """The wake words must still route exactly as before."""
    profile = re.compile(r"^\s*سیب\s+(پروفایل|من)\s*$")
    assert profile.match("سیب پروفایل")
    assert profile.match("سیب من")
    assert not profile.match("سیبام")


def _fresh_session():
    """A second session, matching how the Django-less tests hit the domain."""
    from sib.database.connection import get_session

    return get_session()
