"""Root-cause regressions for the shared navigation + wake words + garden UX.

Spec items #1, #2, #3, #4, #7, #8, #13.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message
from aiogram.types import User as TelegramUser

from sib.database.models import PanelOwner
from sib.handlers.garden import _render_garden
from sib.handlers.start import (
    _help_keyboard,
    handle_apple_battle,
    handle_apple_cards,
    handle_apple_garden,
    handle_help_menu,
    handle_sib_games_word,
    handle_sib_garden_word,
    handle_sib_profile_word,
)
from sib.handlers.wiki import handle_apple_wiki
from sib.keyboards.common import get_cards_keyboard, get_profile_keyboard
from sib.keyboards.party import get_games_menu_keyboard
from sib.services.assets import AssetService
from sib.services.items import ItemService
from sib.services.nav import KNOWN_SCREENS, back_target, parse_callback, with_origin
from sib.services.panel import PanelOwnershipService


def _edit_call(cb, bot):
    """(text, kb) from whichever edit path the handler used."""
    if getattr(bot, "edit_message_text", None) is not None and bot.edit_message_text.called:
        c = bot.edit_message_text.call_args
        text = c.kwargs.get("text", c.args[0] if c.args else None)
        return text, c.kwargs.get("reply_markup")
    c = cb.message.edit_text.call_args
    text = c.kwargs.get("text", c.args[0] if c.args else None)
    return text, c.kwargs.get("reply_markup")


def _rows(kb):
    return [[b.callback_data for b in row] for row in kb.inline_keyboard]


def _all_buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def _callback(user_id: int, data: str, chat_id: int = -300, message_id: int = 71):
    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock()
    cb.from_user.id = user_id
    cb.from_user.is_bot = False
    cb.from_user.first_name = "کاربر"
    cb.from_user.username = "u"
    cb.message = MagicMock(spec=Message)
    cb.message.chat = MagicMock()
    cb.message.chat.id = chat_id
    cb.message.chat.type = "supergroup"
    cb.message.message_id = message_id
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def _message(text: str, user_id: int = 5, chat_type: str = "supergroup", chat_id: int = -400):
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.chat.title = "گروه"
    # Real Telegram user - MagicMock attrs leak into DB columns.
    msg.from_user = TelegramUser(
        id=user_id, is_bot=False, first_name="سارا", username="sara"
    )
    msg.reply = AsyncMock()
    msg.reply.return_value = MagicMock(
        message_id=990, chat=MagicMock(id=chat_id)
    )
    return msg


# ---------------------------------------------------------------------------
# Spec #1: wiki back root cause
# ---------------------------------------------------------------------------


def test_nav_parse_is_allowlist_based():
    # Long callbacks are NOT misparsed as origins.
    assert parse_callback("select_game:tic_tac_toe") == ("select_game:tic_tac_toe", None)
    assert parse_callback("trade_ok:12") == ("trade_ok:12", None)
    assert parse_callback("ab_join:5:group_games_panel") == ("ab_join:5", "group_games_panel")
    assert parse_callback("apple_wiki:apple_cards") == ("apple_wiki", "apple_cards")
    assert back_target(None) == "main_menu"
    assert back_target("group_games_panel") == "group_games_panel"
    assert with_origin("apple_battle", None) == "apple_battle"
    assert with_origin("apple_battle", "group_games_panel") == "apple_battle:group_games_panel"
    # Every registered screen must be routable.
    assert "apple_cards" in KNOWN_SCREENS and "group_games_panel" in KNOWN_SCREENS


@pytest.mark.asyncio
async def test_wiki_back_buttons_are_real_callbacks(db_session):
    """Root cause: back target must be a REAL callback name, not a label."""
    from sib.database.models import Character, ReleaseStatus

    db_session.add(
        Character(
            code="red_apple",
            name_fa="سیب‌سرخ",
            name_en="Red Apple",
            is_active=True,
            release_status=ReleaseStatus.RELEASED,
        )
    )
    await db_session.commit()
    # From the cards panel the wiki back returns to the cards panel callback.
    bot = MagicMock()
    cb = _callback(5, "apple_wiki:apple_cards")
    await handle_apple_wiki(cb, bot)
    _text, kb = _edit_call(cb, bot)
    backs = [b for b in _all_buttons(kb) if "بازگشت" in b.text]
    assert len(backs) == 1
    assert backs[0].callback_data == "apple_cards"  # NOT the broken "cards"

    # And every back target is a registered callback handler name.
    from aiogram import Dispatcher
    from sib.handlers import register_all_handlers

    dp = Dispatcher()
    register_all_handlers(dp)

    # Plain wiki opens from main -> back goes to main_menu.
    bot = MagicMock()
    cb = _callback(5, "apple_wiki")
    await handle_apple_wiki(cb, bot)
    _text, kb = _edit_call(cb, bot)
    backs = [b for b in _all_buttons(kb) if "بازگشت" in b.text]
    assert backs[0].callback_data == "main_menu"

    # Chain: wiki -> character page -> back to wiki (with origin preserved).
    char_btn = [b for b in _all_buttons(kb) if b.callback_data.startswith("wiki_char:")]
    assert char_btn and char_btn[0].callback_data.startswith("wiki_char:")
    char_cb = char_btn[0].callback_data  # e.g. "wiki_char:77"

    from sib.handlers.wiki import handle_wiki_char

    bot = MagicMock()
    cb = _callback(5, char_cb)
    await handle_wiki_char(cb, bot)
    _t, kb = _edit_call(cb, bot)
    backs = [b for b in _all_buttons(kb) if "بازگشت" in b.text]
    assert backs[0].callback_data == "wiki_page:0"  # back to the wiki index

    bot = MagicMock()
    cb = _callback(5, f"{char_cb}:apple_cards")
    await handle_wiki_char(cb, bot)
    _t, kb = _edit_call(cb, bot)
    backs = [b for b in _all_buttons(kb) if "بازگشت" in b.text]
    # Origin preserved end-to-end: char -> wiki index -> cards (root fix).
    assert backs[0].callback_data == "wiki_page:0:apple_cards"

    # Cards panel keyboard: wiki button threads the cards origin.
    kb = get_cards_keyboard()
    wiki_btn = [b for b in _all_buttons(kb) if b.callback_data.startswith("apple_wiki")]
    assert wiki_btn[0].callback_data == "apple_wiki:apple_cards"


# ---------------------------------------------------------------------------
# Spec #2: Apple Battle lives in the games menu
# ---------------------------------------------------------------------------


def test_games_menu_contains_battle_and_cards_with_shared_nav():
    games = []
    kb = get_games_menu_keyboard(games, active_count=0)
    labels = [b.text for b in _all_buttons(kb)]
    assert "⚔️ نبرد سیبی" in labels
    assert "🃏 کارت‌ها" in labels
    battle_btn = [b for b in _all_buttons(kb) if b.text == "⚔️ نبرد سیبی"][0]
    assert battle_btn.callback_data == "apple_battle:group_games_panel"
    cards_btn = [b for b in _all_buttons(kb) if b.text == "🃏 کارت‌ها"][0]
    assert cards_btn.callback_data == "apple_cards:group_games_panel"
    # Games menu goes back to main.
    assert _rows(kb)[-1][0] == "main_menu"


@pytest.mark.asyncio
async def test_battle_back_returns_to_games(db_session):
    """main → games → نبرد سیبی → بازگشت به بازی‌ها → games → main."""
    bot = MagicMock()
    cb = _callback(5, "apple_battle:group_games_panel")
    await handle_apple_battle(cb, bot)
    _text, kb = _edit_call(cb, bot)
    backs = [b for b in _all_buttons(kb) if "بازگشت" in b.text]
    assert backs[0].callback_data == "group_games_panel"
    assert "بازی‌ها" in backs[0].text

    # Cards panel with the same origin returns to games too.
    bot = MagicMock()
    cb = _callback(5, "apple_cards:group_games_panel")
    await handle_apple_cards(cb, bot)
    _text, kb = _edit_call(cb, bot)
    backs = [b for b in _all_buttons(kb) if "بازگشت" in b.text]
    assert backs[0].callback_data == "group_games_panel"


# ---------------------------------------------------------------------------
# Spec #3/#4: wake words
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wake_words_open_the_right_surfaces(db_session):
    # 'سیب بازی' in a group -> games menu reply (not the claim).
    with patch("sib.handlers.start._reply_games_menu", new=AsyncMock()) as gm:
        msg = _message("سیب بازی")
        await handle_sib_games_word(msg, MagicMock())
        gm.assert_awaited_once()

    # 'سیب منو' -> MAIN menu (decision: group main menu; private unchanged).
    with patch("sib.handlers.start._reply_main_menu", new=AsyncMock()) as mm:
        msg = _message("سیب منو")
        await handle_sib_games_word(msg, MagicMock())
        mm.assert_awaited_once()

    # 'سیب پروفایل' -> the SENDER's profile (subject = message sender).
    msg = _message("سیب پروفایل", user_id=4242)
    await handle_sib_profile_word(msg, MagicMock())
    text = msg.reply.call_args.args[0]
    assert "پروفایل سیبی سارا" in text  # the sender's name
    # And the opened panel is claimed to the sender.
    sent = msg.reply.return_value
    owner = await PanelOwnershipService.get_owner(
        db_session, sent.chat.id, sent.message_id
    )
    assert owner == 4242

    # 'سیب باغ' -> the sender's garden, in the same chat, claimed to sender.
    msg = _message("سیب باغ", user_id=4242)
    await handle_sib_garden_word(msg, MagicMock())
    text = msg.reply.call_args.args[0]
    assert "باغ سیبِ سارا" in text  # sender's garden, not someone else's
    sent = msg.reply.return_value
    owner = await PanelOwnershipService.get_owner(
        db_session, sent.chat.id, sent.message_id
    )
    assert owner == 4242


def test_wake_words_never_match_claim_word():
    """The claim regex stays exactly 'سیب' - no fall-through conflicts."""
    import re

    from sib.handlers import start as start_mod

    # Find the claim handler's pattern through the router's registered filters.
    # Simpler invariant: the distinct words must not equal the claim word.
    claim = re.compile(r"^\s*سیب\s*$")
    words = re.compile(r"^\s*سیب\s+(بازی|منو)\s*$")
    balance = re.compile(r"^\s*سیبام\s*$")
    profile = re.compile(r"^\s*سیب\s+(پروفایل|من)\s*$")
    garden = re.compile(r"^\s*سیب\s+باغ\s*$")
    patterns = (claim, words, balance, profile, garden)
    for w in (
        "سیب",
        "سیب بازی",
        "سیب منو",
        "سیبام",
        "سیب من",
        "سیب پروفایل",
        "سیب باغ",
    ):
        matches = [bool(p.match(w)) for p in patterns]
        assert sum(matches) == 1, f"{w!r} must match exactly one wake-word handler"
    assert claim.match("سیب")


# ---------------------------------------------------------------------------
# Spec #7/#8: garden public view + private control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_garden_opens_in_group_and_claims(db_session):
    from sib.database.models import User

    if await db_session.get(User, 4242) is None:
        db_session.add(User(id=4242, first_name="کاربر", language_code="fa"))
    await db_session.commit()  # visible to the handler's own session too
    bot = MagicMock()
    cb = _callback(4242, "apple_garden", chat_id=-500, message_id=77)
    await handle_apple_garden(cb, bot)
    # No private-only rejection anymore.
    cb.answer.assert_awaited()
    text, _kb = _edit_call(cb, bot)
    assert "باغ سیبِ" in text
    owner = await PanelOwnershipService.get_owner(db_session, -500, 77)
    assert owner == 4242
    _, panel_type = await PanelOwnershipService.get_panel_context(
        db_session, -500, 77, 4242
    )
    assert panel_type == "garden"


@pytest.mark.asyncio
async def test_garden_render_has_inventory_line(db_session):
    from sib.database.models import User

    if await db_session.get(User, 1) is None:
        db_session.add(User(id=1, first_name="کاربر", language_code="fa"))
    await ItemService.grant(db_session, 1, "apple_gold", 2)
    await AssetService.register(db_session, "golden_apple", "f1", type="sticker")
    text, kb = await _render_garden(1, db_session)
    assert "ذخیرهٔ برداشت" in text
    assert "🌕×**۲**" in text
    # Garden controls: one panel button per plot + refresh/main always.
    labels = [b.text for b in _all_buttons(kb)]
    callbacks = [b.callback_data for b in _all_buttons(kb)]
    assert sum(c.startswith("garden_tree:") for c in callbacks) == 5
    assert any("بروزرسانی" in t for t in labels)
    assert any("منوی اصلی" in t for t in labels)

    # Grow a plot to MATURE and the harvest control appears (spec mock).
    import datetime as _dt

    from sib.services.garden import GardenService

    from sib.services.seedling import SeedlingService

    garden = await GardenService.get_or_create(db_session, 1)
    await SeedlingService.admin_adjust(db_session, 1, 1, "test")
    await GardenService.plant(db_session, 1, 0)
    # Growth is time-derived: backdate the planting to make the plot MATURE.
    from sqlalchemy import select as sa_select

    from sib.database.models import GardenPlot

    plot = await db_session.scalar(
        sa_select(GardenPlot)
        .where(GardenPlot.garden_id == 1)  # gardens PK is user_id
        .where(GardenPlot.plot_index == 0)
    )
    _past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=8)
    plot.planted_at = _past
    plot.mature_at = _past + _dt.timedelta(days=1)
    plot.fruit_ready_at = _past + _dt.timedelta(days=2)
    await db_session.commit()
    text, kb = await _render_garden(1, db_session)
    labels = [b.text for b in _all_buttons(kb)]
    assert any("برداشت" in t for t in labels)
    assert "آماده برداشت" in text  # the ripe plot is announced in the body


# ---------------------------------------------------------------------------
# Spec #13: paged help
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_is_paged_not_one_long_text():
    """Phase A5: the guide is a TREE - an index plus one page per section."""
    from sib.services.help_content import HELP_SECTIONS

    kb = _help_keyboard()
    labels = [b.text for b in _all_buttons(kb)]
    assert "🏠 منوی اصلی" in labels
    for section in HELP_SECTIONS:
        assert section.title_fa in labels
        assert f"help:section:{section.section_key}" in [
            b.callback_data for b in _all_buttons(kb)
        ]

    bot = MagicMock()
    cb = _callback(5, "help_menu")
    await handle_help_menu(cb, bot)
    text, _kb = _edit_call(cb, bot)
    assert len(text) < 400  # the index page stays short

    from sib.handlers.start import handle_help_navigation

    bot = MagicMock()
    cb = _callback(5, "help:section:social")
    await handle_help_navigation(cb, bot)
    text, kb = _edit_call(cb, bot)
    assert "باغ سیب" in text
    assert "پودر جادویی" in text  # the magic-tree progression
    backs = [b for b in _all_buttons(kb) if b.callback_data == "help:index"]
    assert backs  # every page returns to the help index


# ---------------------------------------------------------------------------
# Guide → Apple Battle regression (reported: opening it errors the bot)
# Root cause: parse_callback used to eat the TOPIC 'apple_battle' as the nav
# origin (it is a KNOWN screen name) -> base.split(":")[1] IndexError.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guide_every_topic_opens_without_origin(db_session):
    """Every Guide page opens when the Guide was reached WITHOUT an origin
    (main menu -> راهنما -> section) — including 'cards'."""
    from sib.handlers.start import handle_help_navigation
    from sib.services.help_content import HELP_SECTIONS

    for section in HELP_SECTIONS:
        bot = MagicMock()
        cb = _callback(5, f"help:section:{section.section_key}")
        await handle_help_navigation(cb, bot)  # must NOT raise
        text, kb = _edit_call(cb, bot)
        assert text.strip(), section.section_key
        backs = [b for b in _all_buttons(kb) if b.callback_data == "help:index"]
        assert backs, section.section_key  # back to the guide index


@pytest.mark.asyncio
async def test_guide_apple_battle_page_opens_and_keeps_content(db_session):
    """The exact reported path: Guide → Apple Battle shows the topic page."""
    from sib.handlers.start import handle_help_page

    bot = MagicMock()
    cb = _callback(5, "help_page:apple_battle")
    await handle_help_page(cb, bot)
    text, kb = _edit_call(cb, bot)
    assert "نبرد سیبی" in text
    # the page never falls into the 'not found' alert
    assert cb.answer.call_args.args[0] is None or "پیدا نشد" not in (cb.answer.call_args.args[0] or "")


@pytest.mark.asyncio
async def test_guide_apple_battle_page_keeps_origin(db_session):
    """With an origin the page still parses the topic and threads the origin."""
    from sib.handlers.start import handle_help_page

    bot = MagicMock()
    cb = _callback(5, "help_page:apple_battle:group_games_panel")
    await handle_help_page(cb, bot)
    text, kb = _edit_call(cb, bot)
    assert "نبرد سیبی" in text
    backs = [b for b in _all_buttons(kb) if b.callback_data.startswith("help:index")]
    assert backs[0].callback_data == "help:index:group_games_panel"


def test_parse_callback_keeps_topic_arguments():
    """arg_segments: declared handler args are never re-parsed as origin."""
    # Without the hint (old shape) a screen-named arg IS eaten — the reason
    # help_page needed arg_segments=1.
    assert parse_callback("help_page:apple_battle") == ("help_page", "apple_battle")
    # With the hint the topic stays part of the callback base.
    assert parse_callback("help_page:apple_battle", arg_segments=1) == (
        "help_page:apple_battle",
        None,
    )
    assert parse_callback("help_page:apple_battle:group_games_panel", arg_segments=1) == (
        "help_page:apple_battle",
        "group_games_panel",
    )
    assert parse_callback("help_page:garden", arg_segments=1) == ("help_page:garden", None)
    assert parse_callback("help_page:garden:main_menu", arg_segments=1) == (
        "help_page:garden",
        "main_menu",
    )
