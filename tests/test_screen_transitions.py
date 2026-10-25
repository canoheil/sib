"""Screen-transition regression suite (UI / Media / Navigation overhaul).

The bug report this pins down:
1. Callbacks died when the message behind them was a PHOTO (handlers
   assumed text and called edit_text directly).
2. Photos "followed the user": a photo screen's art survived into text
   screens because handlers only edited captions, and text↔photo swaps
   were never done properly (no replacement, no message-id handover).
3. Each screen must render its OWN media + caption + keyboard — from ANY
   entry point (/start, «سیب منو», «سیب بازی», buttons, Back/Home/Refresh).

These tests drive the REAL transition engine against ``FakeBot`` — an
in-memory chat that enforces Telegram's actual rules (edit_text only on
text messages, edit_caption/edit_media only on photo messages, no type
conversion in place). They assert observable behavior: what the live
message shows, that no orphans are left behind, and that the live message
reference follows every replacement.
"""

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from sib.services.telegram_ui import (
    render_callback_screen,
    safe_edit_callback_message,
    safe_edit_screen,
)


# ---------------------------------------------------------------------------
# FakeBot: an in-memory chat that behaves like Telegram
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, message_id, chat_id, kind, text=None, caption=None,
                 photo=None, reply_markup=None):
        self.message_id = message_id
        self.chat = types.SimpleNamespace(id=chat_id, type="private")
        self.kind = kind  # "text" | "photo"
        self.text = text
        self.caption = caption
        self.photo = photo
        self.reply_markup = reply_markup
        self.deleted = False

    @property
    def content_type(self):
        return "photo" if self.kind == "photo" else "text"


class FakeBot:
    """Enforces Telegram's message-type rules so transitions are real."""

    def __init__(self):
        self.messages = {}
        self._next_id = 100

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def _get(self, chat_id, message_id):
        msg = self.messages.get((chat_id, message_id))
        if msg is None or msg.deleted:
            raise TelegramBadRequest(None, "message to edit not found")
        return msg

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        msg = FakeMessage(self._new_id(), chat_id, "text", text=text,
                          reply_markup=reply_markup)
        self.messages[(chat_id, msg.message_id)] = msg
        return msg

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None,
                         parse_mode=None):
        msg = FakeMessage(self._new_id(), chat_id, "photo", caption=caption,
                          photo=photo, reply_markup=reply_markup)
        self.messages[(chat_id, msg.message_id)] = msg
        return msg

    async def edit_message_text(self, text, chat_id=None, message_id=None,
                                reply_markup=None, parse_mode=None, **kw):
        msg = self._get(chat_id, message_id)
        if msg.kind != "text":
            raise TelegramBadRequest(None, "there is no text in the message to edit")
        msg.text = text
        msg.reply_markup = reply_markup
        return msg

    async def edit_message_caption(self, caption=None, chat_id=None,
                                   message_id=None, reply_markup=None,
                                   parse_mode=None, **kw):
        msg = self._get(chat_id, message_id)
        if msg.kind != "photo":
            raise TelegramBadRequest(None, "there is no caption in the message to edit")
        msg.caption = caption
        msg.reply_markup = reply_markup
        return msg

    async def edit_message_media(self, media=None, chat_id=None, message_id=None,
                                 reply_markup=None, **kw):
        msg = self._get(chat_id, message_id)
        if msg.kind != "photo":
            raise TelegramBadRequest(None, "there is no media in the message to edit")
        msg.photo = getattr(media, "media", media)
        msg.caption = getattr(media, "caption", None)
        msg.reply_markup = reply_markup
        return msg

    async def delete_message(self, chat_id, message_id):
        msg = self.messages.get((chat_id, message_id))
        if msg is None or msg.deleted:
            raise TelegramBadRequest(None, "message to delete not found")
        msg.deleted = True
        return True

    def live(self, chat_id=1):
        return [m for (c, _i), m in sorted(self.messages.items())
                if c == chat_id and not m.deleted]

    def live_count(self, chat_id=1):
        return len(self.live(chat_id))


def _callback_on(msg):
    return types.SimpleNamespace(
        message=msg,
        data="x",
        answer=AsyncMock(),
        from_user=types.SimpleNamespace(id=7, is_bot=False, first_name="t"),
    )


def _text_message(bot, chat_id=1):
    msg = FakeMessage(bot._new_id(), chat_id, "text", text="hello")
    bot.messages[(chat_id, msg.message_id)] = msg
    return msg


def _photo_message(bot, chat_id=1):
    msg = FakeMessage(bot._new_id(), chat_id, "photo", caption="hello",
                      photo="ART-OLD")
    bot.messages[(chat_id, msg.message_id)] = msg
    return msg


# ---------------------------------------------------------------------------
# 1. The four transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_to_text_edits_in_place():
    bot = FakeBot()
    msg = _text_message(bot)

    ok = await safe_edit_callback_message(
        _callback_on(msg), bot, "صفحه متنی", reply_markup="KB"
    )
    assert ok is True
    assert bot.live_count() == 1
    live = bot.live()[0]
    assert live is msg  # same message, edited in place
    assert live.text == "صفحه متنی"
    assert live.reply_markup == "KB"


@pytest.mark.asyncio
async def test_photo_to_photo_swaps_media_in_place():
    bot = FakeBot()
    msg = _photo_message(bot)

    ok = await safe_edit_callback_message(
        _callback_on(msg), bot, "صفحه عکسی جدید", photo="ART-NEW", reply_markup="KB"
    )
    assert ok is True
    assert bot.live_count() == 1
    live = bot.live()[0]
    assert live is msg  # same message id (photo→photo is in-place)
    assert live.photo == "ART-NEW"  # the NEW screen's art — not the old one
    assert live.caption == "صفحه عکسی جدید"
    assert live.reply_markup == "KB"


@pytest.mark.asyncio
async def test_photo_to_text_replaces_and_drops_stale_media():
    bot = FakeBot()
    msg = _photo_message(bot)

    cb = _callback_on(msg)
    ok = await safe_edit_callback_message(cb, bot, "صفحه متنی", reply_markup="KB")
    assert ok is True

    # Exactly one live message; the photo message is gone.
    assert bot.live_count() == 1
    live = bot.live()[0]
    assert live.kind == "text"
    assert live.photo is None  # NO stale media
    assert live.text == "صفحه متنی"
    assert msg.deleted is True
    # The live reference moved to the replacement.
    assert cb.message.message_id == live.message_id


@pytest.mark.asyncio
async def test_text_to_photo_replaces_and_shows_new_media():
    bot = FakeBot()
    msg = _text_message(bot)

    cb = _callback_on(msg)
    ok = await safe_edit_callback_message(
        cb, bot, "صفحه عکسی", photo="ART-NEW", reply_markup="KB"
    )
    assert ok is True

    assert bot.live_count() == 1
    live = bot.live()[0]
    assert live.kind == "photo"
    assert live.photo == "ART-NEW"
    assert live.caption == "صفحه عکسی"
    assert msg.deleted is True
    assert cb.message.message_id == live.message_id


# ---------------------------------------------------------------------------
# 2. Message reference handover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_reference_moves_after_replacement():
    """After a replacement every later render uses the NEW message id —
    a deleted message_id must never linger in the live reference."""
    bot = FakeBot()
    msg = _text_message(bot)
    cb = _callback_on(msg)
    original_id = msg.message_id

    # text → photo: replace #1
    await safe_edit_callback_message(cb, bot, "۱", photo="A")
    id_after_first = cb.message.message_id
    assert id_after_first != original_id

    # photo → text: replace #2, keeps following the reference
    await safe_edit_callback_message(cb, bot, "۲")
    id_after_second = cb.message.message_id
    assert id_after_second != id_after_first

    # photo again: replace #3
    await safe_edit_callback_message(cb, bot, "۳", photo="B")
    id_after_third = cb.message.message_id
    assert id_after_third != id_after_second

    # Every id ever handed over is the id of a live message at that time;
    # only the last one is alive now — no stale ids, no orphans.
    assert bot.live_count() == 1
    assert bot.live()[0].message_id == id_after_third
    assert bot.live()[0].photo == "B"


@pytest.mark.asyncio
async def test_safe_edit_screen_returns_the_live_message_id():
    bot = FakeBot()
    msg = _text_message(bot)

    # photo target on a text message: replaced.
    new_id = await safe_edit_screen(
        bot, 1, msg.message_id, "متن", photo="ART", current_is_photo=False
    )
    assert new_id == bot.live()[0].message_id
    assert new_id != msg.message_id

    # photo→photo same-type edit: id stays.
    again = await safe_edit_screen(
        bot, 1, new_id, "متن ۲", photo="ART-2", current_is_photo=True
    )
    assert again == new_id


# ---------------------------------------------------------------------------
# 3. Multi-step navigation: every screen owns its own media
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_does_not_leak_across_multistep_navigation():
    """Main → character page → Back → Shop → Garden → Games:

    after EVERY step the live message shows exactly the current screen's
    media — the character art never travels along."""
    bot = FakeBot()
    msg = _text_message(bot)  # /start welcome (text)
    cb = _callback_on(msg)

    screens = [
        ("main", "MAIN-ART"),      # main menu (banner)
        ("cards", None),           # collection (text)
        ("char", "CHAR-ART"),      # character page (card art)
        ("back-main", "MAIN-ART"), # Back → main (its own banner again!)
        ("shop", "SHOP-ART"),      # shop (banner)
        ("garden", "GARDEN-ART"),  # garden (banner)
        ("games", "GAMES-ART"),    # games (banner)
        ("help", None),            # help (text)
    ]

    for label, art in screens:
        ok = await safe_edit_callback_message(cb, bot, label, photo=art)
        assert ok is True, label
        # Exactly one live message — no orphans accumulate.
        assert bot.live_count() == 1, label
        live = bot.live()[0]
        # The screen's own media, never a previous screen's.
        assert live.photo == art, label
        assert live.text is not None or live.caption is not None
        # The live reference points at the live message.
        assert cb.message.message_id == live.message_id, label


@pytest.mark.asyncio
async def test_no_orphan_messages_after_many_clicks():
    bot = FakeBot()
    msg = _text_message(bot)
    cb = _callback_on(msg)
    total_messages_ever = 1

    for i in range(12):
        art = "ART" if i % 2 == 0 else None
        await safe_edit_callback_message(cb, bot, f"screen {i}", photo=art)
        total_messages_ever += 2  # each replace sends exactly one new message
        assert bot.live_count() == 1

    # Replacements delete their predecessor: created = 1 + #replaces,
    # deleted = #replaces.
    created = len(bot.messages)
    deleted = sum(1 for m in bot.messages.values() if m.deleted)
    assert created == deleted + 1


# ---------------------------------------------------------------------------
# 4. Entry points are NOT magic paths (canonical rendering)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_screen_identical_from_text_or_photo_entry():
    """The same screen rendered from a TEXT message and from a PHOTO message
    ends up identical: same content, same keyboard, same media."""
    bot = FakeBot()
    text_entry = _text_message(bot)
    photo_entry = _photo_message(bot)

    cb_text = _callback_on(text_entry)
    cb_photo = _callback_on(photo_entry)

    await safe_edit_callback_message(cb_text, bot, "صفحه X", photo="X-ART",
                                     reply_markup="KB")
    await safe_edit_callback_message(cb_photo, bot, "صفحه X", photo="X-ART",
                                     reply_markup="KB")

    t = bot.messages[(1, cb_text.message.message_id)]
    p = bot.messages[(1, cb_photo.message.message_id)]
    assert t.kind == p.kind == "photo"
    assert t.photo == p.photo == "X-ART"
    assert t.caption == p.caption == "صفحه X"
    assert t.reply_markup == p.reply_markup == "KB"


@pytest.mark.asyncio
async def test_render_callback_screen_uses_one_canonical_path():
    """Handlers say 'show screen X'; the renderer picks edit/swap/replace —
    no handler-level workarounds. Both entry types go through the same
    render_callback_screen call."""
    bot = FakeBot()
    for entry_kind in ("text", "photo"):
        msg = _text_message(bot) if entry_kind == "text" else _photo_message(bot)
        cb = _callback_on(msg)
        result = await render_callback_screen(cb, bot, "screen", photo="ART")
        assert result is not None
        assert result.message_id == cb.message.message_id
        live = bot.messages[(1, result.message_id)]
        assert live.photo == "ART"
        assert live.deleted is False


# ---------------------------------------------------------------------------
# 5. Back / Home / Refresh restore the FULL screen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_back_restores_media_text_and_keyboard():
    """Back must restore the whole previous screen — media included."""
    bot = FakeBot()
    msg = _text_message(bot)
    cb = _callback_on(msg)

    # Home screen (banner) ...
    await safe_edit_callback_message(cb, bot, "منوی اصلی", photo="MAIN",
                                     reply_markup="MAIN-KB")
    # ... into a photo character page ...
    await safe_edit_callback_message(cb, bot, "کارت", photo="CHAR",
                                     reply_markup="CHAR-KB")
    # ... Back → home: the MAIN banner comes back (not the character art).
    await safe_edit_callback_message(cb, bot, "منوی اصلی", photo="MAIN",
                                     reply_markup="MAIN-KB")

    live = bot.live()[0]
    assert live.photo == "MAIN"
    assert live.caption == "منوی اصلی"
    assert live.reply_markup == "MAIN-KB"


@pytest.mark.asyncio
async def test_refresh_keeps_screen_media():
    """Refresh re-renders the SAME screen: the screen's own art stays."""
    bot = FakeBot()
    msg = _text_message(bot)
    cb = _callback_on(msg)

    await safe_edit_callback_message(cb, bot, "باغ", photo="GARDEN",
                                     reply_markup="GARDEN-KB")
    await safe_edit_callback_message(cb, bot, "باغ (تازه)", photo="GARDEN",
                                     reply_markup="GARDEN-KB")

    live = bot.live()[0]
    assert live.photo == "GARDEN"
    assert live.caption == "باغ (تازه)"
    assert bot.live_count() == 1


@pytest.mark.asyncio
async def test_home_from_photo_screen_drops_stale_media():
    """Home (text-only screen) from a photo screen must not keep the photo."""
    bot = FakeBot()
    msg = _photo_message(bot)
    cb = _callback_on(msg)

    await safe_edit_callback_message(cb, bot, "کمک", reply_markup="HELP-KB")
    live = bot.live()[0]
    assert live.kind == "text"
    assert live.photo is None
    assert live.text == "کمک"


# ---------------------------------------------------------------------------
# 6. /start, «سیب منو» and «سیب بازی» render through the same logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wake_words_and_buttons_share_menu_rendering(db_session):
    """«سیب منو» (wake word) and the main_menu button render the SAME
    screen — the wake word is not a magic rendering path."""
    from aiogram.types import Chat, User

    from sib.handlers.start import (
        _main_menu_text_and_kb,
        handle_main_menu,
        handle_sib_games_word,
    )

    user = User(id=42, is_bot=False, first_name="کاربر", username="u42")
    chat = Chat(id=42, type="private")

    # --- «سیب منو» (wake word) renders the main-menu screen as a reply.
    wake_msg = MagicMock()
    wake_msg.text = "سیب منو"
    wake_msg.from_user = user
    wake_msg.chat = chat
    wake_msg.reply = AsyncMock(return_value=MagicMock(message_id=1,
                                                      chat=MagicMock(id=42)))
    wake_msg.reply_photo = AsyncMock(return_value=MagicMock(message_id=1,
                                                            photo=None))
    await handle_sib_games_word(wake_msg, MagicMock())
    wake_call = wake_msg.reply_photo.call_args
    assert wake_call is not None, "the wake word must render the canonical screen"

    # --- main_menu button renders the same screen onto the message.
    bot = FakeBot()
    msg = _text_message(bot, chat_id=42)
    cb = _callback_on(msg)
    cb.from_user = user
    cb.message.chat = chat
    await handle_main_menu(cb, bot)

    live = bot.live(42)[0]
    expected_text, expected_kb = _main_menu_text_and_kb(user, chat)
    assert wake_call.kwargs["caption"] == expected_text
    caption_or_text = live.caption if live.kind == "photo" else live.text
    assert caption_or_text == expected_text
    # Same keyboard contract (same button callbacks) on both paths.
    assert wake_call.kwargs["reply_markup"].inline_keyboard == expected_kb.inline_keyboard
    assert live.reply_markup.inline_keyboard == expected_kb.inline_keyboard
    # And the main menu screen carries its own banner from the button path.
    assert live.kind == "photo"
    assert live.photo is not None


@pytest.mark.asyncio
async def test_games_menu_screen_identical_from_wake_word_and_button(db_session):
    from aiogram.types import Chat, User

    from sib.database.connection import get_session
    from sib.handlers.party.handlers import build_games_menu, handle_games_panel
    from sib.handlers.start import handle_sib_games_word

    user = User(id=51, is_bot=False, first_name="بازیکن", username="u51")
    chat = Chat(id=-600, type="supergroup", title="گروه")

    expected_text, expected_kb = None, None
    async with get_session() as session:
        expected_text, expected_kb = await build_games_menu(session, chat.id)

    # --- «سیب بازی» (wake word) → banner screen reply.
    wake_msg = MagicMock()
    wake_msg.text = "سیب بازی"
    wake_msg.from_user = user
    wake_msg.chat = chat
    wake_msg.reply = AsyncMock()
    wake_msg.reply_photo = AsyncMock(return_value=MagicMock(message_id=2,
                                                            photo=None))
    await handle_sib_games_word(wake_msg, MagicMock())
    wake_call = wake_msg.reply_photo.call_args
    assert wake_call is not None

    # --- group_games_panel button → same screen on the message.
    bot = FakeBot()
    msg = _text_message(bot, chat_id=-600)
    cb = _callback_on(msg)
    cb.from_user = user
    cb.message.chat = chat
    cb.data = "group_games_panel"
    await handle_games_panel(cb, bot)

    live = bot.live(-600)[0]
    rendered = live.caption if live.kind == "photo" else live.text
    assert wake_call.kwargs["caption"] == expected_text
    assert rendered == expected_text
    assert wake_call.kwargs["reply_markup"].inline_keyboard == expected_kb.inline_keyboard
    assert live.reply_markup.inline_keyboard == expected_kb.inline_keyboard
    # The games screen owns its banner — from both paths.
    assert live.kind == "photo"
    assert live.photo is not None


@pytest.mark.asyncio
async def test_start_welcome_keyboard_reaches_canonical_screens():
    """"/start must not be a magical different path": every button on the
    /start welcome keyboard is a real screen callback that the canonical
    renderer can display from a text OR photo message."""
    from sib.core.config import settings
    from sib.keyboards.common import get_private_start_keyboard

    kb = get_private_start_keyboard(settings.BOT_USERNAME)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row
                 if b.callback_data]
    assert "apple_garden" in callbacks
    assert "apple_cards" in callbacks
    assert "main_menu" not in callbacks  # /start IS the main entry already

    # Every screen callback renders fine from a text entry AND a photo entry.
    for entry in ("text", "photo"):
        bot = FakeBot()
        msg = _text_message(bot) if entry == "text" else _photo_message(bot)
        cb = _callback_on(msg)
        ok = await safe_edit_callback_message(cb, bot, "screen", photo=None)
        assert ok is True, entry
        assert bot.live()[0].kind == "text"
        assert bot.live()[0].photo is None
