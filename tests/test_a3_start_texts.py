"""Phase A3 regressions: /start texts and the one-button private keyboard.

Covers the rewritten private and group /start messages:
- the private /start reply carries EXACTLY ONE inline button, and it is the
  "add to group" URL deep link;
- none of the removed main-menu shortcuts reappear on /start;
- both texts are Ovarya-first, carry the support handle, mention the legacy
  mini-games (private, as secondary content) and the group title (group);
- the group /start keyboard is unchanged;
- both texts are Persian-only (injected values masked) and under 1024 chars.
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
from sib.keyboards.common import (
    get_group_welcome_keyboard,
    get_private_start_keyboard,
)

ADD_TO_GROUP_LABEL = "➕ افزودن سیب به گروه"

# The eight buttons removed from the private /start reply markup (they stay
# available on the private MAIN menu, which uses get_private_welcome_keyboard).
REMOVED_BUTTON_LABELS = [
    "🌳 باغ من",
    "🎴 کارت‌های من",
    "🛒 فروشگاه",
    "📚 ویکی کاراکترها",
    "🔄 مبادله کارت",
    "🎁 صندوقچه گنج روزانه",
    "👤 پروفایل من",
    "🎮 بازی‌ها و راهنما",
]
REMOVED_BUTTON_CALLBACKS = [
    "apple_garden",
    "apple_cards",
    "apple_shop",
    "apple_wiki",
    "apple_trade",
    "apple_chest",
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
async def test_private_start_shows_exactly_one_button(db_session):
    """The private /start reply markup has EXACTLY ONE inline button."""
    _text, kwargs = await _run_private_start()
    markup = kwargs.get("reply_markup")
    assert markup is not None
    buttons = _flatten(markup)
    assert len(buttons) == 1


@pytest.mark.asyncio
async def test_private_start_button_is_url_add_to_group(db_session):
    """The single private /start button is the add-to-group URL deep link."""
    _text, kwargs = await _run_private_start()
    button = _flatten(kwargs["reply_markup"])[0]
    assert button.text == ADD_TO_GROUP_LABEL
    assert button.url == f"https://t.me/{settings.BOT_USERNAME}?startgroup=true"
    assert button.url.startswith("https://t.me/")
    # It is a URL button, not a callback button.
    assert button.callback_data is None


@pytest.mark.asyncio
async def test_private_start_does_not_contain_removed_buttons(db_session):
    """None of the eight removed main-menu buttons reappear on /start."""
    _text, kwargs = await _run_private_start()
    buttons = _flatten(kwargs["reply_markup"])
    texts = [b.text for b in buttons]
    callbacks = [b.callback_data for b in buttons if b.callback_data]
    for label in REMOVED_BUTTON_LABELS:
        assert label not in texts
    for callback in REMOVED_BUTTON_CALLBACKS:
        assert callback not in callbacks


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
# Supplementary: the gated /start path shows the same one-button welcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_membership_gate_welcome_matches_private_start(db_session):
    """The membership gate (only reachable from /start) resolves into the
    SAME Ovarya welcome with the SAME single add-to-group button."""
    gate_text, gate_kwargs = await _run_membership_gate_welcome(first_name="Sima")
    start_text, start_kwargs = await _run_private_start(first_name="Sima")
    assert gate_text == start_text

    gate_markup = gate_kwargs.get("reply_markup")
    assert gate_markup is not None
    buttons = _flatten(gate_markup)
    assert len(buttons) == 1
    assert buttons[0].text == ADD_TO_GROUP_LABEL
    assert buttons[0].url == f"https://t.me/{settings.BOT_USERNAME}?startgroup=true"
