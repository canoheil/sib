"""Phase 2/3/4/6 regressions: wake-word routing, 'باغ من', garden price,
Apple Battle entry cleanup."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from aiogram.types import User as TelegramUser

from sib.database.models import PanelOwner
from sib.handlers.garden import _render_garden
from sib.handlers.start import (
    handle_my_garden_word,
    handle_sib_games_word,
    handle_sib_garden_word,
)
from sib.keyboards.common import (
    get_group_welcome_keyboard,
    get_private_welcome_keyboard,
    get_profile_keyboard,
)
from sib.keyboards.party import get_games_menu_keyboard
from sib.services.config_store import ConfigService
from sib.services.garden import GardenService
from sib.services.panel import PanelOwnershipService


def _msg(text: str, user_id: int = 5, chat_type: str = "supergroup", chat_id: int = -400):
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
    msg.reply = AsyncMock()
    msg.reply.return_value = MagicMock(message_id=990, chat=MagicMock(id=chat_id))
    return msg


def _all_buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Phase 2: سیب منو / سیب بازی routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_sib_menu_opens_main_menu(db_session):
    msg = _msg("سیب منو", chat_type="private", chat_id=5)
    await handle_sib_games_word(msg, MagicMock())
    text = msg.reply.call_args.args[0]
    assert "منوی اصلی سیب" in text


@pytest.mark.asyncio
async def test_group_sib_menu_opens_main_menu(db_session):
    # Decision (ask_user): in GROUPS 'سیب منو' must open the GROUP MAIN MENU
    # (it wrongly opened the Games Menu before).
    msg = _msg("سیب منو", chat_type="supergroup")
    await handle_sib_games_word(msg, MagicMock())
    text = msg.reply.call_args.args[0]
    assert "منوی اصلی سیب" in text


@pytest.mark.asyncio
async def test_private_sib_bazi_opens_games_menu(db_session):
    msg = _msg("سیب بازی", chat_type="private", chat_id=5)
    await handle_sib_games_word(msg, MagicMock())
    text = msg.reply.call_args.args[0]
    assert "منوی بازی‌های سیب" in text


@pytest.mark.asyncio
async def test_group_sib_bazi_opens_games_menu(db_session):
    msg = _msg("سیب بازی", chat_type="supergroup")
    await handle_sib_games_word(msg, MagicMock())
    text = msg.reply.call_args.args[0]
    assert "منوی بازی‌های سیب" in text


def test_wake_words_are_mutually_exclusive():
    import re

    patterns = [
        re.compile(r"^\s*سیب\s*$"),                      # claim
        re.compile(r"^\s*سیب\s+(بازی|منو)\s*$"),         # menus
        re.compile(r"^\s*سیب\s+پروفایل\s*$"),            # profile
        re.compile(r"^\s*سیب\s+باغ\s*$"),                # garden
        re.compile(r"^\s*باغ\s+من\s*$"),                 # my garden
    ]
    words = ("سیب", "سیب بازی", "سیب منو", "سیب پروفایل", "سیب باغ", "باغ من")
    for w in words:
        matched = [bool(p.match(w)) for p in patterns]
        assert sum(matched) == 1, f"{w!r} must match exactly one handler"


# ---------------------------------------------------------------------------
# Phase 3: 'باغ من' == 'سیب باغ' (sender's garden, shared path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_my_garden_word_opens_senders_garden_in_group(db_session):
    from sib.database.models import User

    if await db_session.get(User, 77) is None:
        db_session.add(User(id=77, first_name="کاربر", language_code="fa"))
    await db_session.commit()

    msg = _msg("باغ من", user_id=77, chat_id=-401)
    await handle_my_garden_word(msg, MagicMock())
    text = msg.reply.call_args.args[0]
    assert "باغ سیبِ سارا" in text  # the SENDER's garden

    sent = msg.reply.return_value
    owner = await PanelOwnershipService.get_owner(db_session, sent.chat.id, sent.message_id)
    assert owner == 77  # claimed to the sender


@pytest.mark.asyncio
async def test_sib_garden_and_my_garden_share_one_path(db_session):
    """'باغ من' must not be a second garden implementation."""
    from sib.handlers import start as start_mod

    # Both handlers delegate to the same shared opener.
    import inspect

    src_my = inspect.getsource(start_mod.handle_my_garden_word)
    src_sib = inspect.getsource(start_mod.handle_sib_garden_word)
    assert "_open_sender_garden" in src_my and "_open_sender_garden" in src_sib


@pytest.mark.asyncio
async def test_garden_panel_is_owner_locked_for_others(db_session):
    from sib.middleware.panel_guard import PanelGuardMiddleware
    from sib.database.models import User

    if await db_session.get(User, 78) is None:
        db_session.add(User(id=78, first_name="کاربر", language_code="fa"))
    await db_session.commit()

    msg = _msg("باغ من", user_id=78, chat_id=-402)
    await handle_my_garden_word(msg, MagicMock())
    sent = msg.reply.return_value

    from aiogram.types import CallbackQuery

    cb = MagicMock(spec=CallbackQuery)
    cb.data = "garden_harvest:0"
    cb.from_user = TelegramUser(id=999, is_bot=False, first_name="دیگری")
    cb.message = MagicMock()
    cb.message.chat = MagicMock(id=sent.chat.id, type="supergroup")
    cb.message.message_id = sent.message_id
    cb.answer = AsyncMock()

    handled = []

    async def handler(event, data):
        handled.append(event)
        return "OK"

    result = await PanelGuardMiddleware()(handler, cb, {})
    assert result is None and handled == []  # non-owner cannot mutate


# ---------------------------------------------------------------------------
# Phase 4: garden plot price comes from the same config the purchase uses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_garden_price_displayed_matches_purchase_cost(db_session):
    from sib.database.models import User

    if await db_session.get(User, 1) is None:
        db_session.add(User(id=1, first_name="کاربر", language_code="fa"))
    await db_session.commit()

    prices = await ConfigService.get(db_session, "garden_plot_prices")
    cost = prices[0]

    text, kb = await _render_garden(1, db_session)
    unlock_btn = [b for b in _all_buttons(kb) if b.callback_data == "garden_unlock"][0]
    assert f"🍎 {cost} سیب" in unlock_btn.text
    assert "باز کردن قطعه بعدی" in unlock_btn.text

    # And the purchase charges exactly that config value for ONE plot.
    from sib.services.apple_wallet import AppleWalletService
    from sib.tests.test_parties import create_test_user

    u = await create_test_user(db_session, "باغبان")
    await AppleWalletService.earn(db_session, u.id, cost, reason="test")
    await db_session.commit()
    cap_before = (await GardenService.get_or_create(db_session, u.id)).capacity_plots
    await GardenService.unlock_plot(db_session, u.id)
    await db_session.commit()
    assert await AppleWalletService.get_balance(db_session, u.id) == 0
    garden = await GardenService.get_or_create(db_session, u.id)
    assert garden.capacity_plots == cap_before + 1


# ---------------------------------------------------------------------------
# Phase 6: Apple Battle has exactly ONE entry (inside the games menu)
# ---------------------------------------------------------------------------


def test_apple_battle_single_entry_in_games_menu():
    # The required entry lives in the games menu.
    kb = get_games_menu_keyboard([], active_count=0)
    battle = [b for b in _all_buttons(kb) if "نبرد سیبی" in (b.text or "")]
    assert len(battle) == 1
    assert battle[0].callback_data == "apple_battle:group_games_panel"

    # Redundant duplicates are gone from main menu, group welcome, profile.
    for fn in (
        lambda: get_private_welcome_keyboard("sibbot"),
        get_group_welcome_keyboard,
        lambda: get_profile_keyboard(),
    ):
        buttons = _all_buttons(fn())
        assert not any("نبرد سیبی" in (b.text or "") for b in buttons)
        assert not any(
            (b.callback_data or "").startswith("apple_battle") for b in buttons
        ), "no dead apple_battle callbacks outside the games menu"


@pytest.mark.asyncio
async def test_apple_battle_games_entry_still_opens_queue(db_session):
    from unittest.mock import MagicMock as _M

    from sib.handlers.start import handle_apple_battle

    bot = _M()
    cb = _M()
    cb.data = "apple_battle:group_games_panel"
    cb.from_user = TelegramUser(id=5, is_bot=False, first_name="کاربر")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = MagicMock(id=-300, type="supergroup")
    cb.message.message_id = 71
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_apple_battle(cb, bot)
    # The screen renders through the central transition engine: read the
    # edit from whichever edit path carried it (message- or bot-level).
    call = cb.message.edit_text.call_args or bot.edit_message_text.call_args
    assert call is not None, "the battle entry screen must be rendered"
    text = call.args[0] if call.args else call.kwargs["text"]
    assert "نبرد سیبی" in text
    kb = call.kwargs["reply_markup"]
    backs = [b for b in _all_buttons(kb) if "بازگشت" in b.text]
    assert backs[0].callback_data == "group_games_panel"
