"""Phase A3 regressions: /start texts and the private first-session menu.

Covers the private and group /start messages:
- the private /start reply carries the REAL first-session menu (chest, garden,
  cards, shop, profile, help) with the "add to group" URL deep link LAST. The
  old Phase A3 rule — exactly one button, add-to-group only — was the biggest
  drop-off point of the funnel (a player who does not administer a group had
  nothing to press) and was replaced by the starter-pack onboarding;
- both texts are Ovarya-first, carry the support handle, mention the legacy
  mini-games (private, as secondary content) and the group title (group);
- the group /start keyboard is unchanged;
- both texts are Persian-only (injected values masked) and under 1024 chars.
- the official-channel gate is SOFT: verifying membership only removes the
  join row, it never unlocks the menu.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    Message,
    User as TelegramUser,
)

from sib.core.config import settings
from sib.handlers.start import (
    SUPPORT_HANDLE,
    handle_check_membership,
    handle_start,
)
from sib.keyboards.common import get_group_welcome_keyboard

ADD_TO_GROUP_LABEL = "➕ افزودن سیب به گروه"

# The eight buttons removed from the private /start reply markup (they stay
# available on the private MAIN menu, which uses get_private_welcome_keyboard).
#: The first-session menu the private /start MUST offer (callback data).
FIRST_SESSION_CALLBACKS = [
    "apple_chest",
    "apple_garden",
    "apple_cards",
    "apple_shop",
    "apple_profile",
    "help_menu",
]
MINI_GAMES = ["کارت بالا", "سنگ‌کاغذ‌قیچی", "دوز", "چهار در یک ردیف", "کشتی جنگی"]


def _flatten(markup):
    return [button for row in markup.inline_keyboard for button in row]


async def _run_private_start(
    user_id: int = 333444, first_name: str = "سارا"
) -> tuple[str, dict]:
    """Run handle_start in a private chat as an (already verified) member."""
    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = TelegramChat(id=user_id, type="private")
    mock_msg.from_user = TelegramUser(id=user_id, is_bot=False, first_name=first_name)
    mock_msg.answer = AsyncMock()

    mock_bot = AsyncMock()
    with patch(
        "sib.services.membership.checker.MembershipService.check_user_membership"
    ) as mock_check:
        mock_check.return_value = (True, None)
        await handle_start(mock_msg, mock_bot)

    mock_msg.answer.assert_called_once()
    args, kwargs = mock_msg.answer.call_args
    return args[0], kwargs


async def _run_group_start(
    chat_id: int = -1009988776655, title: str = "گروه مدافعان اواریا"
) -> tuple[str, dict]:
    """Run handle_start in a supergroup chat."""
    mock_msg = MagicMock(spec=Message)
    mock_msg.chat = TelegramChat(id=chat_id, type="supergroup", title=title)
    mock_msg.from_user = TelegramUser(id=777, is_bot=False, first_name="کیان")
    mock_msg.reply = AsyncMock()

    mock_bot = AsyncMock()
    await handle_start(mock_msg, mock_bot)

    mock_msg.reply.assert_called_once()
    args, kwargs = mock_msg.reply.call_args
    return args[0], kwargs


async def _run_membership_gate_welcome(
    user_id: int = 444555, first_name: str = "مهدی"
) -> tuple[str, dict]:
    """Run the post-verification welcome of the /start membership gate."""
    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name=first_name)
    mock_cb.answer = AsyncMock()
    mock_cb.message = MagicMock(spec=Message)
    mock_cb.message.edit_text = AsyncMock()

    mock_bot = AsyncMock()
    with patch(
        "sib.services.membership.checker.MembershipService.check_user_membership"
    ) as mock_check:
        mock_check.return_value = (True, None)
        await handle_check_membership(mock_cb, mock_bot)

    mock_cb.message.edit_text.assert_called_once()
    args, kwargs = mock_cb.message.edit_text.call_args
    return args[0], kwargs


# ---------------------------------------------------------------------------
# Private /start: keyboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_start_shows_the_first_session_menu(db_session):
    """The private /start opens the screens a solo player can play today."""
    _text, kwargs = await _run_private_start()
    markup = kwargs.get("reply_markup")
    assert markup is not None
    buttons = _flatten(markup)
    assert len(buttons) > 1
    callbacks = [b.callback_data for b in buttons if b.callback_data]
    for callback in FIRST_SESSION_CALLBACKS:
        assert callback in callbacks


@pytest.mark.asyncio
async def test_private_start_keeps_add_to_group_as_the_last_row(db_session):
    """«افزودن سیب به گروه» is still offered — but it is the goal, not a wall."""
    _text, kwargs = await _run_private_start()
    markup = kwargs["reply_markup"]
    last_row = markup.inline_keyboard[-1]
    assert len(last_row) == 1
    button = last_row[0]
    assert button.text == ADD_TO_GROUP_LABEL
    assert button.url == f"https://t.me/{settings.BOT_USERNAME}?startgroup=true"
    assert button.callback_data is None


@pytest.mark.asyncio
async def test_private_start_has_no_channel_row_for_members(db_session):
    """A verified member sees the menu only — no channel prompt."""
    _text, kwargs = await _run_private_start()
    labels = [b.text for b in _flatten(kwargs["reply_markup"])]
    assert all("عضویت در کانال" not in label for label in labels)


# ---------------------------------------------------------------------------
# Private /start: text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_start_mentions_ovarya(db_session):
    """The private /start text is Ovarya-first (Ovarya + Shiasiib)."""
    text, _kwargs = await _run_private_start()
    assert "اواریا" in text
    assert "شیاسیب" in text


@pytest.mark.asyncio
async def test_private_start_contains_support_handle(db_session):
    """The private /start text carries the official support handle."""
    text, _kwargs = await _run_private_start()
    assert SUPPORT_HANDLE in text
    assert "@sibgame_official" in text


@pytest.mark.asyncio
async def test_private_start_mentions_mini_games(db_session):
    """All five legacy mini-games are mentioned once, as secondary content."""
    text, _kwargs = await _run_private_start()
    for game in MINI_GAMES:
        assert game in text
    # Secondary framing, not the old leading "active games" list.
    assert "بازی‌های جانبی" in text
    assert "بازی‌های فعال" not in text


# ---------------------------------------------------------------------------
# Group /start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_start_contains_group_title(db_session):
    """The group /start text greets the group by its title."""
    text, _kwargs = await _run_group_start(title="گروه مدافعان اواریا")
    assert "گروه مدافعان اواریا" in text
    # Ovarya-first framing for groups too.
    assert "اواریا" in text
    assert "شیاسیب" in text


@pytest.mark.asyncio
async def test_group_start_keeps_existing_keyboard(db_session):
    """The group /start keyboard is unchanged (same buttons/callbacks/urls)."""
    _text, kwargs = await _run_group_start()
    markup = kwargs.get("reply_markup")
    assert markup is not None

    def signature(kb):
        return [
            (b.text, b.callback_data, b.url) for row in kb.inline_keyboard for b in row
        ]

    assert signature(markup) == signature(get_group_welcome_keyboard())
    callbacks = [b.callback_data for b in _flatten(markup)]
    assert set(callbacks) == {
        "group_games_panel",
        "help_menu",
        "apple_shop",
        "apple_profile",
        "group_rating_stats",
        "group_chest",
    }


@pytest.mark.asyncio
async def test_group_start_contains_support_handle(db_session):
    """The group /start text carries the official support handle."""
    text, _kwargs = await _run_group_start()
    assert SUPPORT_HANDLE in text
    assert "@sibgame_official" in text


# ---------------------------------------------------------------------------
# Both texts: language and length
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_start_texts_persian_only(db_session):
    """Masking injected values, both /start texts contain no Latin text.

    The injected values (first name, group title, support handle) are Latin
    on purpose — after masking them, any surviving Latin letter or digit
    would mean unmasked non-Persian copy.
    """
    private_text, _kwargs = await _run_private_start(first_name="Sima_Test_User")
    group_text, _kwargs = await _run_group_start(title="Test_Group_123")

    def mask(text: str) -> str:
        for value in ("Sima_Test_User", "Test_Group_123", SUPPORT_HANDLE):
            text = text.replace(value, "")
        return text

    latin = re.compile(r"[A-Za-z0-9]")
    assert not latin.search(mask(private_text)), mask(private_text)
    assert not latin.search(mask(group_text)), mask(group_text)


@pytest.mark.asyncio
async def test_all_start_texts_under_1024_chars(db_session):
    """Both /start texts stay under the 1024-char caption limit."""
    long_name = "نام‌بسیارطولانی" * 8  # ~120 chars of injected name
    private_text, _kwargs = await _run_private_start(first_name=long_name)
    group_text, _kwargs = await _run_group_start(title=f"گروه {long_name}")
    assert len(private_text) < 1024
    assert len(group_text) < 1024


# ---------------------------------------------------------------------------
# Supplementary: verifying the channel never "unlocks" the game
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_membership_check_reopens_the_first_session_menu(db_session):
    """Verifying the channel re-renders the SAME menu without the join row.

    The gate is soft now: the menu (and the starter pack) are available before
    verification, so a successful check only removes «📢 عضویت در کانال سیب».
    """
    gate_text, gate_kwargs = await _run_membership_gate_welcome(first_name="Sima")

    assert "اواریا" in gate_text
    assert SUPPORT_HANDLE in gate_text

    gate_markup = gate_kwargs.get("reply_markup")
    assert gate_markup is not None
    callbacks = [b.callback_data for b in _flatten(gate_markup) if b.callback_data]
    for callback in FIRST_SESSION_CALLBACKS:
        assert callback in callbacks
    labels = [b.text for b in _flatten(gate_markup)]
    assert all("عضویت در کانال" not in label for label in labels)
    assert labels[-1] == ADD_TO_GROUP_LABEL
