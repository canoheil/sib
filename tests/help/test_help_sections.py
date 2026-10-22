"""Phase A5 — the structured help section.

The guide is a TREE (sib/services/help_content.py): an index with one
button per section, every section its own page, one «🔙 بازگشت به راهنما»
button back to the index, and every page edited IN PLACE.

These tests pin:
* the shape of the tree (every expected section is reachable);
* the copy rules (Persian only, non-empty, under one Telegram page);
* the navigation (section -> index -> section, no new messages);
* the coverage (every text shortcut, the invasion, the apple mine, the
  chests and the support handle must be mentioned).
"""

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    Message,
    MessageEntity,
    User as TelegramUser,
)

from sib.handlers import start as start_module
from sib.handlers.start import (
    handle_help_command,
    handle_help_menu,
    handle_help_navigation,
)
from sib.services.help_content import (
    HELP_BACK_LABEL,
    HELP_SECTIONS,
    INJECTED_VALUES,
    SUPPORT_HANDLE,
    get_section,
    render_index_text,
    render_section_text,
)
from sib.services.sib_claim import fa_digits

#: The ten top-level sections of the new guide.
EXPECTED_SECTION_KEYS = (
    "quick_start",
    "world",
    "cards",
    "invasion",
    "mine",
    "apples",
    "social",
    "games",
    "shortcuts",
    "support",
)

#: The wake words the guide must document (the «سیب <target>» family plus
#: the bare «سیب» claim and the two other group wake words).
EXPECTED_SHORTCUTS = (
    "سیب منو",
    "سیب بازی",
    "سیب باغ",
    "سیب کارت",
    "سیب اسکین",
    "سیب شاپ",
    "سیب لیدربرد",
    "سیب راهنما",
    "سیب صندوق",
    "معدن سیبی",
    "پروفایلش",
)

#: The five shop chests (data/shop_chests/*.json).
EXPECTED_CHESTS = (
    ("صندوق سیبی", 150),
    ("صندوق باغ افسون", 80),
    ("صندوق امپراتوری", 200),
    ("صندوق فرزانگان", 200),
    ("صندوق مهتاب", 200),
)

LATIN_LETTER = re.compile(r"[A-Za-z]")
LATIN_DIGIT = re.compile(r"[0-9]")
ARABIC_LETTER = re.compile(r"[يﻱﻲﻲكﻙﻚﻪ]")
ARABIC_INDIC_DIGIT = re.compile(r"[٠-٩]")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.send_message = AsyncMock()
    return bot


def _callback(data: str, *, chat_id: int = -100700, message_id: int = 42) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=555, is_bot=False, first_name="سارا")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=chat_id, type="supergroup", title="گروه")
    cb.message.message_id = message_id
    cb.message.text = "screen"
    cb.message.edit_text = AsyncMock()
    cb.message.reply = AsyncMock()
    cb.message.reply_photo = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _msg(text: str, *, user_id: int = 5, chat_type: str = "supergroup", chat_id: int = -400):
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.chat.title = "گروه"
    msg.chat.username = None
    msg.from_user = TelegramUser(
        id=user_id, is_bot=False, first_name="سارا", username="sara"
    )
    sent = MagicMock()
    sent.message_id = 990
    sent.chat = MagicMock(id=chat_id)
    msg.reply = AsyncMock(return_value=sent)
    msg.reply_photo = AsyncMock(return_value=sent)
    return msg


def _edited(bot) -> tuple:
    """(text, keyboard) of the in-place edit."""
    call = bot.edit_message_text.call_args
    assert call is not None, "the screen must be edited in place"
    text = call.kwargs.get("text", call.args[0] if call.args else None)
    return text, call.kwargs.get("reply_markup")


def _buttons(kb) -> list:
    return [b for row in kb.inline_keyboard for b in row]


def _mask(text: str) -> str:
    """Remove the injected values (the support handle) from the copy."""
    masked = text
    for value in INJECTED_VALUES:
        masked = masked.replace(value, "")
    return masked


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def test_help_index_renders_all_expected_sections():
    """The index lists EVERY section, in the tree's order, one button each."""
    assert tuple(s.section_key for s in HELP_SECTIONS) == EXPECTED_SECTION_KEYS

    text = render_index_text()
    for section in HELP_SECTIONS:
        assert section.title_fa in text, section.section_key

    kb = start_module._help_menu_screen()[1]
    callbacks = [b.callback_data for b in _buttons(kb)]
    for section in HELP_SECTIONS:
        assert f"help:section:{section.section_key}" in callbacks


def test_every_section_body_is_non_empty():
    """No section may be an empty page."""
    for section in HELP_SECTIONS:
        assert section.body_fa.strip(), section.section_key
        assert section.title_fa.strip(), section.section_key
        assert len(section.body_fa.strip()) > 40, section.section_key


def test_every_section_body_is_persian_only():
    """Persian script, Persian digits, Persian punctuation (masked values)."""
    for section in HELP_SECTIONS:
        body = _mask(section.body_fa)
        assert not LATIN_LETTER.search(body), section.section_key
        assert not LATIN_DIGIT.search(body), section.section_key
        assert not ARABIC_LETTER.search(body), section.section_key
        assert not ARABIC_INDIC_DIGIT.search(body), section.section_key
        # No em dash and no ASCII quote in Persian prose.
        assert "—" not in body, section.section_key
        assert '"' not in body, section.section_key
        # The «گیومه» is the only quote style.
        assert body.count("«") == body.count("»"), section.section_key


def test_every_section_body_under_1024_chars():
    """One Telegram page per section (the limit is 1024 characters)."""
    for section in HELP_SECTIONS:
        assert len(section.body_fa) < 1024, section.section_key
        assert len(render_section_text(section)) < 1024, section.section_key


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_section_callback_edits_message():
    """A section button EDITS the message in place - no new message."""
    bot = _bot()
    cb = _callback("help:section:invasion")

    await handle_help_navigation(cb, bot)

    text, kb = _edited(bot)
    assert "تهاجم شیاسیب" in text
    assert kb is not None
    # In place: no new message was sent anywhere.
    bot.send_message.assert_not_called()
    cb.message.reply.assert_not_called()
    cb.message.reply_photo.assert_not_called()


@pytest.mark.asyncio
async def test_help_back_button_returns_to_index():
    """The section page carries exactly one button, and it restores the index."""
    bot = _bot()
    cb = _callback("help:section:cards")

    await handle_help_navigation(cb, bot)
    _text, kb = _edited(bot)

    buttons = _buttons(kb)
    assert len(buttons) == 1
    assert buttons[0].text == HELP_BACK_LABEL
    assert buttons[0].callback_data == "help:index"

    # Pressing it restores the index.
    bot2 = _bot()
    cb2 = _callback("help:index")
    await handle_help_navigation(cb2, bot2)
    text2, kb2 = _edited(bot2)
    assert text2 == render_index_text()
    assert any(
        b.callback_data == "help:section:invasion" for b in _buttons(kb2)
    )


@pytest.mark.asyncio
async def test_help_menu_button_opens_the_same_index():
    """The «📖 راهنما» button and «help:index» render the SAME index."""
    bot = _bot()
    await handle_help_menu(_callback("help_menu"), bot)
    text, kb = _edited(bot)
    assert text == render_index_text()
    assert any(
        b.callback_data == "help:section:quick_start" for b in _buttons(kb)
    )


@pytest.mark.asyncio
async def test_help_command_routes_to_the_existing_help_index(monkeypatch):
    """A Telegram /help command sends the existing Persian Guide index."""
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=TelegramChat(id=5, type="private"),
        from_user=TelegramUser(id=5, is_bot=False, first_name="سارا"),
        text="/help",
        entities=[MessageEntity(type="bot_command", offset=0, length=5)],
    )
    send_screen = AsyncMock()
    monkeypatch.setattr("sib.services.banner.send_screen_reply", send_screen)

    # Trigger the real router so the Command("help") filter is exercised as
    # well as the handler and its shared help-menu renderer.
    await start_module.router.message.trigger(message, bot=MagicMock())

    send_screen.assert_awaited_once()
    args = send_screen.await_args.args
    assert args[0] is message
    assert args[1] == "help"
    assert args[2] == render_index_text()
    keyboard = args[3]
    assert all(
        f"help:section:{section.section_key}"
        in [button.callback_data for button in _buttons(keyboard)]
        for section in HELP_SECTIONS
    )
    assert handle_help_command in [
        handler.callback for handler in start_module.router.message.handlers
    ]


@pytest.mark.asyncio
async def test_sib_raahnamaa_opens_help_index(db_session):
    """The «سیب راهنما» shortcut opens the NEW structured index."""
    from sib.handlers.start import handle_sib_shortcut_word

    msg = _msg("سیب راهنما", chat_id=-424)
    await handle_sib_shortcut_word(msg, MagicMock())

    call = msg.reply.call_args
    assert call is not None, "the guide must be shown"
    text = call.args[0]
    kb = call.kwargs.get("reply_markup")

    assert text == render_index_text()
    for section in HELP_SECTIONS:
        assert f"help:section:{section.section_key}" in [
            b.callback_data for b in _buttons(kb)
        ]


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_help_mentions_all_text_shortcuts():
    """Every wake word of the bot is documented in the shortcuts section."""
    body = get_section("shortcuts").body_fa
    for phrase in EXPECTED_SHORTCUTS:
        assert phrase in body, phrase
    # The bare «سیب» claim is a group action, and it is documented as one.
    assert "سیب" in body


def test_help_contains_profile_reply_shortcut():
    """P3: «پروفایلش» is documented as a reply-to-view shortcut."""
    body = get_section("shortcuts").body_fa
    assert "«پروفایلش»" in body
    assert "با ریپلای روی پیام یک بازیکن" in body
    assert "برای دیدن پروفایل آن بازیکن" in body


def test_help_mentions_invasion():
    """The Invasion v2 is explained: how to join, what it deals, what it pays."""
    body = get_section("invasion").body_fa
    assert "شیاسیب" in body
    assert "یورش" in body
    assert "کارت اصلی" in body
    assert "سیب طلایی" in body
    # The guaranteed golden apple reward (3) with the Persian digits.
    assert fa_digits(3) in body


def test_help_mentions_apple_mine():
    """The Apple Mine is explained: command, cooldown, pickaxe ladder."""
    body = get_section("mine").body_fa
    assert "معدن سیبی" in body
    assert "۱۵ دقیقه فاصله لازم است." in body
    for label in ("سیب معمولی", "سیب طلایی", "سیب الماسی", "سیب ابسیدینی"):
        assert label in body, label
    # M2-RECON: the pickaxe is the progression; card selection is gone.
    assert "کلنگ" in body
    assert fa_digits(300) in body  # first upgrade cost
    assert "کارت" not in body


def test_help_mentions_chests():
    """The five shop chests and their prices are listed."""
    body = get_section("apples").body_fa
    for name, price in EXPECTED_CHESTS:
        assert name in body, name
        assert fa_digits(price) in body, name
    # The daily chest is a different system and is named as such.
    assert "صندوقچهٔ گنج روزانه" in body


def test_help_mentions_support_handle():
    """The official support handle appears in the support section."""
    from sib.core.text import escape_markdown

    body = get_section("support").body_fa
    assert SUPPORT_HANDLE in body
    assert "@sibgame_official" in body
    # ... and the whole guide mentions it exactly once.
    whole = "\n".join(s.body_fa for s in HELP_SECTIONS)
    assert whole.count(SUPPORT_HANDLE) == 1
    # The RENDERED page escapes the underscore: Telegram's legacy Markdown
    # rejects an unpaired '_' («can't parse entities»).
    page = render_section_text(get_section("support"))
    assert escape_markdown(SUPPORT_HANDLE) in page
