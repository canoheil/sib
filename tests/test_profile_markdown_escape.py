"""P3 — player names can never break a Markdown message.

Telegram's legacy ``Markdown`` parser rejects a message with an unmatched
``_``, ``*`` or backtick, or an unbalanced ``[``. The rejected message is
never delivered, so a name such as ``Soheil_Dev`` used to make the whole
profile fail to send. Every player-facing name on these screens must now go
through ``safe_name`` / ``escape_markdown`` / ``plain_display_name``.

``assert_legacy_markdown_balanced`` is a structural stand-in for Telegram's
parser: it ignores escaped characters (``\\x``) and requires every toggle
marker to be closed.
"""

from unittest.mock import MagicMock

import pytest
from aiogram.types import Chat as TelegramChat
from aiogram.types import User as TelegramUser

from sib.core.bidi import LRM
from sib.database import User
from sib.handlers.admin_center import _user_panel
from sib.handlers.apple_battle import cmd_profile
from sib.handlers.start import _main_menu_text_and_kb
from sib.tests import test_profile_reply_view as reply_view
from sib.tests.test_marriage_profile import (
    OWNER_ID,
    SPOUSE_ID,
    _marry,
    _panel_profile,
    _profile_message,
    _profile_text,
    _tg,
    _wake_word_profile,
)

PRIVATE_CHAT = TelegramChat(id=OWNER_ID, type="private")

# The shared test DB keeps user rows between tests, so these tests own IDs
# no other module uses (renaming a shared row would break its assertions).
MD_USER_ID = 8990
MD_TARGET_ID = 8991
MD_ADMIN_ID = 8992


def assert_legacy_markdown_balanced(text: str) -> None:
    """Fail when a toggle marker or link bracket is left open.

    Backslash escapes are skipped so ``\\_`` and ``\\*`` count as literal
    characters, exactly as Telegram treats them.
    """
    counts = {ch: 0 for ch in "_*`[]"}
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2  # the escape and the character it protects
            continue
        if ch in counts:
            counts[ch] += 1
        i += 1
    assert counts["_"] % 2 == 0, f"unmatched _ in {text!r}"
    assert counts["*"] % 2 == 0, f"unmatched * in {text!r}"
    assert counts["`"] % 2 == 0, f"unmatched ` in {text!r}"
    # Telegram only fails on an opening "[" with no closing "]"; a stray
    # unescaped "]" is plain text.
    assert counts["["] <= counts["]"], f"unclosed [ in {text!r}"


# ---------------------------------------------------------------------------
# The required regression: a profile for a name with underscores renders.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_handles_underscores_in_name(db_session):
    """A first name like «Soheil_Dev» renders the profile without an exception."""
    name = "Soheil_Dev"
    text = await _wake_word_profile(db_session, _tg(OWNER_ID, name))

    assert "پروفایل سیبی " + LRM + "Soheil\\_Dev" + LRM in text
    assert_legacy_markdown_balanced(text)


@pytest.mark.asyncio
async def test_profile_panel_handles_underscores_in_name(db_session):
    text = await _panel_profile(db_session, _tg(OWNER_ID, "Soheil_Dev"))

    assert "پروفایل سیبی " + LRM + "Soheil\\_Dev" + LRM in text
    assert_legacy_markdown_balanced(text)


@pytest.mark.asyncio
async def test_profile_command_handles_underscores_in_full_name(db_session):
    """/profile uses ``full_name`` (first + last), which was never escaped."""
    user = TelegramUser(
        id=MD_USER_ID, is_bot=False, first_name="Soheil_Dev", last_name="Ali*Rad"
    )
    msg = _profile_message(user, text="/profile")

    await cmd_profile(msg, MagicMock())

    text = _profile_text(msg)
    assert "Soheil\\_Dev" in text
    assert_legacy_markdown_balanced(text)


@pytest.mark.asyncio
async def test_main_menu_greeting_handles_underscores_in_name(db_session):
    """The private main menu greets the player by name in Markdown."""
    text, _kb = _main_menu_text_and_kb(_tg(OWNER_ID, "Soheil_Dev"), PRIVATE_CHAT)

    assert "سلام " + LRM + "Soheil\\_Dev" + LRM + "!" in text
    assert_legacy_markdown_balanced(text)


@pytest.mark.asyncio
async def test_reply_profile_view_handles_underscores_in_target_name(db_session):
    """The «پروفایلش» public view escapes the target's name too."""
    await reply_view._mk_user(db_session, MD_TARGET_ID, first_name="Soheil_Dev")
    await db_session.commit()
    msg = reply_view._command_message(
        _tg(reply_view.VIEWER_ID, "سارا"),
        reply_to_user=_tg(MD_TARGET_ID, "Soheil_Dev"),
    )

    from sib.handlers.start import handle_reply_profile_view

    await handle_reply_profile_view(msg, MagicMock())

    text = reply_view._rendered(msg)
    assert "پروفایل سیبی " + LRM + "Soheil\\_Dev" + LRM in text
    assert_legacy_markdown_balanced(text)
    assert (await db_session.get(User, MD_TARGET_ID)).first_name == "Soheil_Dev"


@pytest.mark.asyncio
async def test_admin_user_panel_escapes_username_underscores(db_session):
    """Telegram usernames may contain «_»; the admin panel prints them in Markdown."""
    user = User(id=MD_ADMIN_ID, first_name="Soheil_Dev", username="soheil_dev_99", language_code="fa")
    db_session.add(user)
    await db_session.commit()

    text, _kb = await _user_panel(db_session, MD_ADMIN_ID)

    assert "soheil\\_dev\\_99" in text
    assert_legacy_markdown_balanced(text)


@pytest.mark.asyncio
async def test_spouse_name_with_underscores_stays_balanced(db_session):
    """The spouse line is escaped too, so a married profile still renders."""
    await _marry(db_session, OWNER_ID, SPOUSE_ID, love_score=30)
    spouse = await db_session.get(User, SPOUSE_ID)
    spouse.first_name = "Pari_*Ahmadi"
    await db_session.commit()

    text = await _wake_word_profile(db_session, _tg(OWNER_ID, "Soheil_Dev"))

    assert "💍 همسر: " + LRM + "Pari\\_\\*Ahmadi" + LRM in text
    assert_legacy_markdown_balanced(text)


# ---------------------------------------------------------------------------
# Hostile names: every combination that breaks legacy Markdown.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Soheil_Dev", "*bold*", "`code", "[link](x)", "a_b_c", "_", "\\", "**__``[["],
)
@pytest.mark.asyncio
async def test_profile_markdown_is_balanced_for_hostile_names(db_session, name):
    text = await _wake_word_profile(db_session, _tg(OWNER_ID, name))

    assert "پروفایل سیبی" in text
    assert_legacy_markdown_balanced(text)
    # Brackets from a hostile name are escaped, so they can never open a link.
    if "[" in name:
        assert "\\[" in text
