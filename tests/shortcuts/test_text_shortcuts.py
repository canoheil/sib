"""Phase A4 — text shortcut aliases for existing menus.

The five new wake words must run the SAME code path as the menu entry they
alias:

* «سیب شاپ»      -> ``cmd_shop`` (the ``/shop`` entry function)
* «سیب کارت»     -> the «کارت‌های من» screen of the profile button
* «سیب اسکین»    -> the «تغییر پوسته» (Change Skin) screen of the profile
* «سیب لیدربرد»  -> ``cmd_leaderboard`` (the ``/leaderboard`` entry function)
* «سیب راهنما»   -> the Guide index of the «📖 راهنما» button

Matching is EXACT (whole message, one anchor) — a sentence that merely
contains the phrase, a slash command or any other text never triggers.
"""

import inspect
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery
from aiogram.types import Message
from aiogram.types import User as TelegramUser

from sib.database.models import User
from sib.handlers import start as start_module
from sib.handlers.apple_mine import MINE_COMMAND_PATTERN
from sib.handlers.daily_chest import GROUP_SHORTCUT_PATTERN
from sib.handlers.marriage import (
    DIVORCE_TRIGGER_PATTERN,
    FLOWER_TRIGGER_PATTERN,
    PROPOSAL_TRIGGER_PATTERN,
)
from sib.handlers.sib_hug import HUG_TRIGGER_PATTERN
from sib.handlers.sib_kiss import KISS_TRIGGER_PATTERN
from sib.handlers.start import (
    SIB_SHORTCUT_PATTERN,
    SIB_SHORTCUT_TARGETS,
    handle_apple_cards,
    handle_profile_skin,
    handle_sib_games_word,
    handle_sib_garden_word,
    handle_sib_shortcut_word,
    sib_shortcut_target,
)
from sib.middleware.panel_guard import PanelGuardMiddleware
from sib.services.panel import PanelOwnershipService
from sib.tests.test_parties import create_test_user

SENT_MESSAGE_ID = 990

# The five aliases and the canonical target each one must resolve to.
SHORTCUTS = {
    "سیب شاپ": "shop",
    "سیب کارت": "cards",
    "سیب اسکین": "skin",
    "سیب لیدربرد": "leaderboard",
    "سیب راهنما": "help",
}

#: Every OTHER exact-match text trigger registered in the bot (wake words
#: owned by other modules are imported, not copied).
EXISTING_EXACT_PATTERNS = (
    r"^\s*سیب\s*$",                      # «سیب» -> apple claim
    r"^\s*سیب\s+(بازی|منو)\s*$",          # menus
    r"^\s*سیبام\s*$",                    # apple balance
    r"^\s*سیب\s+(پروفایل|من)\s*$",        # profile
    r"^\s*سیب\s+باغ\s*$",                # garden
    r"^\s*باغ\s+من\s*$",                 # my garden
    MINE_COMMAND_PATTERN,                 # معدن سیبی
    GROUP_SHORTCUT_PATTERN,               # سیب صندوق
    PROPOSAL_TRIGGER_PATTERN,             # ازدواج سیبی
    DIVORCE_TRIGGER_PATTERN,              # طلاق سیبی
    FLOWER_TRIGGER_PATTERN,               # گل سیبی
    HUG_TRIGGER_PATTERN,                  # بغل سیبی
    KISS_TRIGGER_PATTERN,                 # بوس سیبی
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _msg(text: str, user_id: int = 5, chat_type: str = "supergroup", chat_id: int = -400):
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.chat.title = "گروه"
    msg.chat.username = None
    # A real Telegram user: MagicMock attributes would leak into DB columns.
    msg.from_user = TelegramUser(
        id=user_id, is_bot=False, first_name="سارا", username="sara"
    )
    sent = MagicMock()
    sent.message_id = SENT_MESSAGE_ID
    sent.chat = MagicMock(id=chat_id)
    msg.reply = AsyncMock(return_value=sent)
    msg.reply_photo = AsyncMock(return_value=sent)
    return msg


def _shown(msg):
    """(text, keyboard) of the screen, whichever reply path carried it."""
    if msg.reply_photo.called:
        call = msg.reply_photo.call_args
        return call.kwargs["caption"], call.kwargs.get("reply_markup")
    call = msg.reply.call_args
    assert call is not None, "no screen was sent"
    return call.args[0], call.kwargs.get("reply_markup")


def _edit_args(bot):
    """(text, keyboard) from the edit path the transition engine used."""
    call = bot.edit_message_text.call_args
    text = call.kwargs.get("text", call.args[0] if call.args else None)
    return text, call.kwargs.get("reply_markup")


def _callbacks(kb, prefix: str):
    return sorted(
        b.callback_data
        for row in kb.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith(prefix)
    )


def _callback(user_id: int, data: str, chat_id: int = -300, message_id: int = 71):
    cb = MagicMock()
    cb.data = data
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name="سارا")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = MagicMock(id=chat_id, type="supergroup")
    cb.message.message_id = message_id
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


# ---------------------------------------------------------------------------
# Existing wake words still work (the aliases must not shadow them)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shortcut_sib_menu_opens_main_menu(db_session):
    msg = _msg("سیب منو", chat_type="private", chat_id=5)
    await handle_sib_games_word(msg, MagicMock())
    text, _kb = _shown(msg)
    assert "منوی اصلی سیب" in text


@pytest.mark.asyncio
async def test_shortcut_sib_bagh_opens_garden(db_session):
    user = await create_test_user(db_session, "باغبان")
    msg = _msg("سیب باغ", user_id=user.id, chat_id=-410)
    await handle_sib_garden_word(msg, MagicMock())
    text, _kb = _shown(msg)
    assert "باغ سیبِ سارا" in text  # the SENDER's garden

    sent = msg.reply_photo.return_value
    owner = await PanelOwnershipService.get_owner(db_session, sent.chat.id, sent.message_id)
    assert owner == user.id


# ---------------------------------------------------------------------------
# The five new aliases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shortcut_sib_shop_opens_shop(db_session):
    user = await create_test_user(db_session, "خریدار")
    msg = _msg("سیب شاپ", user_id=user.id, chat_id=-420)

    await handle_sib_shortcut_word(msg, MagicMock())

    text, kb = _shown(msg)
    assert "فروشگاه سیب" in text  # exactly /shop
    assert "shop_cards" in _callbacks(kb, "shop_")
    # Same panel rule as /shop: the screen is claimed to the sender.
    owner_id, panel_type = await PanelOwnershipService.get_panel_context(
        db_session, msg.chat.id, SENT_MESSAGE_ID, user.id
    )
    assert owner_id == user.id and panel_type == "shop"
    # ... and it IS the command entry function, not a copy of the shop.
    assert "cmd_shop" in inspect.getsource(start_module._open_shop_entry)


@pytest.mark.asyncio
async def test_shortcut_sib_kaart_opens_my_cards(db_session):
    user = await create_test_user(db_session, "کارت‌دار")
    msg = _msg("سیب کارت", user_id=user.id, chat_id=-421)

    await handle_sib_shortcut_word(msg, MagicMock())
    text, kb = _shown(msg)
    assert "کارت‌های من" in text

    # The profile button renders the SAME screen through the SAME builder.
    bot = AsyncMock()
    cb = _callback(user.id, "apple_cards")
    await handle_apple_cards(cb, bot)
    profile_text, profile_kb = _edit_args(bot)
    assert text == profile_text
    assert _callbacks(kb, "card_panel:") == _callbacks(profile_kb, "card_panel:")
    # One builder, two entries: neither path re-implements the screen.
    assert "_cards_screen" in inspect.getsource(start_module.handle_apple_cards)
    assert "_cards_screen" in inspect.getsource(start_module._open_sender_cards)
    # Opened as a top-level screen, so Back returns to the main menu.
    assert "main_menu" in _callbacks(kb, "main_menu")

    owner_id, panel_type = await PanelOwnershipService.get_panel_context(
        db_session, msg.chat.id, SENT_MESSAGE_ID, user.id
    )
    assert owner_id == user.id and panel_type == "cards"


@pytest.mark.asyncio
async def test_shortcut_sib_skin_opens_skin_swap(db_session):
    user = await create_test_user(db_session, "خوش‌پوسته")
    msg = _msg("سیب اسکین", user_id=user.id, chat_id=-422)

    await handle_sib_shortcut_word(msg, MagicMock())
    text, kb = _shown(msg)
    assert "Change Skin" in text and "تغییر پوسته" in text
    assert _callbacks(kb, "profile_skin_select:")

    # Same screen as the profile's «🎨 تغییر پوسته» button.
    bot = AsyncMock()
    cb = _callback(user.id, "profile_skin")
    await handle_profile_skin(cb, bot)
    profile_text, profile_kb = _edit_args(bot)
    assert text == profile_text
    assert _callbacks(kb, "profile_skin_select:") == _callbacks(
        profile_kb, "profile_skin_select:"
    )
    # The picker stays personal: it is claimed to the sender like the profile.
    owner_id, _panel_type = await PanelOwnershipService.get_panel_context(
        db_session, msg.chat.id, SENT_MESSAGE_ID, user.id
    )
    assert owner_id == user.id


@pytest.mark.asyncio
async def test_shortcut_sib_leaderboard_opens_leaderboard(db_session):
    user = await create_test_user(db_session, "قهرمان")
    msg = _msg("سیب لیدربرد", user_id=user.id, chat_id=-423)

    await handle_sib_shortcut_word(msg, MagicMock())

    text, kb = _shown(msg)
    assert "برترین گروه‌های سیب" in text  # exactly /leaderboard
    assert "show_leaderboard" in _callbacks(kb, "show_leaderboard")
    assert "cmd_leaderboard" in inspect.getsource(
        start_module._open_leaderboard_entry
    )


@pytest.mark.asyncio
async def test_shortcut_sib_raahnamaa_opens_help(db_session):
    user = await create_test_user(db_session, "راهنماخواه")
    msg = _msg("سیب راهنما", user_id=user.id, chat_id=-424)

    await handle_sib_shortcut_word(msg, MagicMock())

    text, kb = _shown(msg)
    assert "راهنمای بازی سیب" in text
    topics = _callbacks(kb, "help:section:")
    assert topics, "the Guide index must carry its section buttons"
    # The same buttons the «📖 راهنما» button renders.
    expected = _callbacks(start_module._help_menu_screen()[1], "help:section:")
    assert topics == expected
    # The guide is public: exactly one way home, no owner lock.
    assert "main_menu" in _callbacks(kb, "main_menu")
    assert not await PanelOwnershipService.get_owner(
        db_session, msg.chat.id, SENT_MESSAGE_ID
    )


# ---------------------------------------------------------------------------
# Exact matching only
# ---------------------------------------------------------------------------


def test_shortcut_exact_match_only():
    pattern = re.compile(SIB_SHORTCUT_PATTERN)
    for phrase in (
        "سلام سیب شاپ",
        "سیب شاپ کن",
        "می‌خوام سیب کارت ببینم",
        "سیب راهنما لطفا",
        "سیب اسکین بهتره",
        "سیب شاپ:",
        "سیب لیدربرد ۲",
        "سیبکارت",  # concatenated, not the shortcut
        "باغ سیب شاپ",
    ):
        assert not pattern.match(phrase), phrase
        assert sib_shortcut_target(phrase) is None, phrase

    # The exact phrases themselves DO resolve.
    for phrase, target in SHORTCUTS.items():
        assert sib_shortcut_target(phrase) == target


@pytest.mark.asyncio
async def test_shortcut_exact_match_only_never_replies(db_session):
    msg = _msg("سلام سیب شاپ")
    await handle_sib_shortcut_word(msg, MagicMock())
    assert not msg.reply.called and not msg.reply_photo.called


@pytest.mark.asyncio
async def test_shortcut_does_not_hijack_slash_commands(db_session):
    pattern = re.compile(SIB_SHORTCUT_PATTERN)
    for command in ("/shop", "/leaderboard", "/top", "/help", "/start", "/سیب شاپ"):
        assert not pattern.match(command), command
        assert sib_shortcut_target(command) is None, command

    msg = _msg("/shop")
    await handle_sib_shortcut_word(msg, MagicMock())
    assert not msg.reply.called and not msg.reply_photo.called


# ---------------------------------------------------------------------------
# Collision + registration
# ---------------------------------------------------------------------------


def test_shortcut_no_collision_with_existing():
    """No other exact-match trigger claims the five new strings."""
    new_pattern = re.compile(SIB_SHORTCUT_PATTERN)
    existing = [(p, re.compile(p)) for p in EXISTING_EXACT_PATTERNS]

    for phrase in SHORTCUTS:
        for source, compiled in existing:
            assert not compiled.match(phrase), f"{phrase!r} collides with {source!r}"

    # Mutually exclusive routing: each shortcut matches EXACTLY one handler
    # pattern (the new one).
    for phrase in SHORTCUTS:
        hits = [source for source, compiled in existing if compiled.match(phrase)]
        hits += [SIB_SHORTCUT_PATTERN] if new_pattern.match(phrase) else []
        assert len(hits) == 1, f"{phrase!r} matched {hits}"

    # The existing wake words are untouched by the new pattern.
    for phrase in ("سیب", "سیب منو", "سیب بازی", "سیبام", "سیب پروفایل", "سیب باغ", "باغ من"):
        assert not new_pattern.match(phrase), phrase

    # Typed Persian variants (Arabic Yeh/Kaf) resolve to the same targets...
    assert sib_shortcut_target("سيب شاپ") == "shop"
    assert sib_shortcut_target("سیب كارت") == "cards"
    assert sib_shortcut_target("سیب لیدربورد") == "leaderboard"
    # ... and never bleed into a different target.
    assert len(set(SIB_SHORTCUT_TARGETS.values())) == 5


def test_shortcuts_registered_on_the_start_router():
    """Task 3: same router as «سیب منو» / «سیب باغ», no new router."""
    callbacks = [h.callback for h in start_module.router.message.handlers]
    assert callbacks.count(handle_sib_shortcut_word) == 1
    assert handle_sib_games_word in callbacks
    assert handle_sib_garden_word in callbacks


# ---------------------------------------------------------------------------
# Permission rules are the same ones the button follows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shortcut_cards_panel_stays_owner_locked_in_group(db_session):
    user = await create_test_user(db_session, "صاحب‌پنل")
    msg = _msg("سیب کارت", user_id=user.id, chat_type="supergroup", chat_id=-430)
    await handle_sib_shortcut_word(msg, MagicMock())
    sent = msg.reply.return_value

    cb = MagicMock(spec=CallbackQuery)
    cb.data = "card_panel:123"
    cb.from_user = TelegramUser(id=999999, is_bot=False, first_name="دیگری")
    cb.message = MagicMock()
    cb.message.chat = MagicMock(id=sent.chat.id, type="supergroup")
    cb.message.message_id = sent.message_id
    cb.answer = AsyncMock()

    handled = []

    async def handler(event, data):
        handled.append(event)
        return "OK"

    result = await PanelGuardMiddleware()(handler, cb, {})
    assert result is None and handled == []


@pytest.mark.asyncio
async def test_shortcuts_work_in_private_and_group(db_session):
    """The aliases never gate on chat type: the sender is the subject."""
    user = await create_test_user(db_session, "همه‌جا")
    for chat_type, chat_id in (("private", user.id), ("supergroup", -440)):
        msg = _msg("سیب کارت", user_id=user.id, chat_type=chat_type, chat_id=chat_id)
        await handle_sib_shortcut_word(msg, MagicMock())
        text, _kb = _shown(msg)
        assert "کارت‌های من" in text

    # An unknown account is registered by the wake word, exactly like the
    # other wake words do.
    fresh = _msg("سیب کارت", user_id=777777, chat_type="private", chat_id=777777)
    await handle_sib_shortcut_word(fresh, MagicMock())
    assert await db_session.get(User, 777777) is not None
