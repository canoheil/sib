"""Real callback paths for card, collection and shop screen transitions."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from sib.handlers.shop import handle_shop_item, handle_shop_item_full, handle_shop_refresh
from sib.handlers.start import handle_apple_cards, handle_main_menu
from sib.handlers.wiki import handle_apple_wiki, handle_wiki_char, handle_wiki_char_full, handle_wiki_page
from sib.middleware.panel_guard import PanelGuardMiddleware
from sib.services.panel import PanelOwnershipService
from sib.services.telegram_ui import CAPTION_LIMIT
from sib.tests.test_character_art import _shop_character
from sib.tests.test_screen_transitions import FakeBot, _photo_message, _text_message


def _units(text):
    return len(text.encode("utf-16-le")) // 2


class RecordingBot(FakeBot):
    """Fake Telegram with the real photo-caption limit and an API call trace."""

    def __init__(self):
        super().__init__()
        self.operations = []

    @staticmethod
    def _check_caption(text):
        if text and _units(text) > CAPTION_LIMIT:
            raise TelegramBadRequest(None, "MEDIA_CAPTION_TOO_LONG")

    async def edit_message_media(self, media=None, chat_id=None, message_id=None, **kwargs):
        self.operations.append(("edit_media", chat_id, message_id))
        self._check_caption(media.caption)
        return await super().edit_message_media(media=media, chat_id=chat_id,
                                                message_id=message_id, **kwargs)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.operations.append(("edit_text", chat_id, message_id))
        return await super().edit_message_text(text, chat_id=chat_id,
                                               message_id=message_id, **kwargs)

    async def send_message(self, chat_id, text, **kwargs):
        self.operations.append(("send_text", chat_id, None))
        return await super().send_message(chat_id, text, **kwargs)

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.operations.append(("send_photo", chat_id, None))
        self._check_caption(caption)
        return await super().send_photo(chat_id, photo, caption=caption, **kwargs)

    async def delete_message(self, chat_id, message_id):
        self.operations.append(("delete", chat_id, message_id))
        return await super().delete_message(chat_id, message_id)


def _callback(msg, data, user_id=7):
    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    cb.data = data
    cb.from_user = MagicMock(id=user_id, is_bot=False, first_name="کاربر")
    cb.answer = AsyncMock()
    return cb


def _buttons(message):
    return [b for row in message.reply_markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_character_card_previous_next_edit_photo_in_place(db_session):
    """Direct card navigation should not bounce through the text wiki index."""
    characters = [
        await _shop_character(db_session, code)
        for code in ("sib_sorkh", "sib_sabz", "sib_talayi")
    ]
    bot = RecordingBot()
    original = _photo_message(bot)
    cb = _callback(original, f"wiki_char:{characters[1].id}:apple_cards")
    await handle_wiki_char(cb, bot)

    previous = next(b for b in _buttons(cb.message) if "قبلی" in b.text)
    following = next(b for b in _buttons(cb.message) if "بعدی" in b.text)
    assert previous.callback_data == f"wiki_char:{characters[0].id}:apple_cards"
    assert following.callback_data == f"wiki_char:{characters[2].id}:apple_cards"

    cb.data = following.callback_data
    await handle_wiki_char(cb, bot)
    cb.data = next(b.callback_data for b in _buttons(cb.message) if "قبلی" in b.text)
    await handle_wiki_char(cb, bot)

    assert cb.message is original
    assert cb.message.message_id == original.message_id
    assert len(bot.live()) == 1 and not original.deleted
    assert [op[0] for op in bot.operations] == ["edit_media"] * 3
    assert f"قهرمان {characters[1].code}" in cb.message.caption


@pytest.mark.asyncio
async def test_wiki_index_previous_next_are_text_edits(db_session):
    """The existing index-page Previous/Next are text->text, not replacements."""
    from sib.database.models import Character, ReleaseStatus

    db_session.add_all([
        Character(code=f"wiki_{n}", name_fa=f"شخصیت {n}", is_active=True,
                  release_status=ReleaseStatus.RELEASED)
        for n in range(7)
    ])
    await db_session.commit()
    bot = RecordingBot()
    original = _text_message(bot)
    cb = _callback(original, "wiki_page:0:apple_cards")
    await handle_wiki_page(cb, bot)
    cb.data = next(b.callback_data for b in _buttons(cb.message) if "بعدی" in b.text)
    await handle_wiki_page(cb, bot)
    cb.data = next(b.callback_data for b in _buttons(cb.message) if "قبلی" in b.text)
    await handle_wiki_page(cb, bot)
    assert cb.message is original and not original.deleted
    assert len(bot.live()) == 1
    assert [op[0] for op in bot.operations] == ["edit_text"] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("lore", [
    "الف" * 1200,  # Exceeds the limit in both Python and Telegram.
    "🤖" * 600,  # 600 code points, 1200 Telegram UTF-16 units.
])
async def test_long_photo_caption_keeps_media_and_does_not_delete_panel(db_session, lore):
    """Photo target + long lore must not silently turn into a text target."""
    character = await _shop_character(db_session, "sib_sorkh")
    character.lore_fa = lore
    await db_session.commit()

    bot = RecordingBot()
    original = _photo_message(bot)
    cb = _callback(original, f"wiki_char:{character.id}")
    await handle_wiki_char(cb, bot)
    assert cb.message is original and not original.deleted
    assert bot.live() == [original] and original.photo is not None
    assert _units(original.caption) <= CAPTION_LIMIT
    assert "…" in original.caption  # any necessary shortening must be visible
    assert [op[0] for op in bot.operations] == ["edit_media"]

    # Shortening must not discard the full lore: it is one explicit button
    # away. This optional PHOTO -> TEXT transition is a genuine type change.
    cb.data = next(b.callback_data for b in _buttons(original) if "متن کامل" in b.text)
    await handle_wiki_char_full(cb, bot)
    assert original.deleted and cb.message.kind == "text"
    assert lore in cb.message.text
    assert len(bot.live()) == 1 and bot.live()[0] is cb.message
    assert f"wiki_char:{character.id}" in _buttons(cb.message)[0].callback_data


@pytest.mark.asyncio
async def test_cards_to_wiki_and_back_handoffs_live_message_and_owner(db_session):
    """A real text<->photo change replaces once; the panel claim follows it."""
    character = await _shop_character(db_session, "sib_sabz")
    character_id = character.id
    bot = RecordingBot()
    first = _photo_message(bot)
    cb = _callback(first, "apple_cards", user_id=789)
    guard = PanelGuardMiddleware()

    async def click(handler, data):
        cb.data = data
        await guard(lambda event, _: handler(event, bot), cb, {})
        assert len(bot.live()) == 1
        assert bot.live()[0] is cb.message

    await click(handle_apple_cards, "apple_cards")
    cards_id = cb.message.message_id
    assert cards_id != first.message_id and first.deleted
    assert cb.message.kind == "text"
    assert await PanelOwnershipService.get_owner(db_session, 1, cards_id) == 789
    await db_session.rollback()

    await click(handle_apple_wiki, "apple_wiki:apple_cards")
    assert cb.message.message_id == cards_id
    await click(handle_wiki_char, f"wiki_char:{character_id}:apple_cards")
    photo_id = cb.message.message_id
    assert photo_id != cards_id and cb.message.kind == "photo"
    await db_session.rollback()
    assert await PanelOwnershipService.get_owner(db_session, 1, cards_id) is None
    assert await PanelOwnershipService.get_owner(db_session, 1, photo_id) == 789
    await db_session.rollback()

    await click(handle_wiki_page, "wiki_page:0:apple_cards")
    assert cb.message.message_id != photo_id and cb.message.kind == "text"
    await db_session.rollback()
    assert await PanelOwnershipService.get_owner(db_session, 1, photo_id) is None
    assert await PanelOwnershipService.get_owner(db_session, 1, cb.message.message_id) == 789


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_entry", [False, True])
async def test_back_to_public_main_menu_releases_personal_claim(db_session, photo_entry):
    """Both a replacement and an in-place media edit unlock the PUBLIC menu."""
    bot = RecordingBot()
    first = _photo_message(bot) if photo_entry else _text_message(bot)
    owner = _callback(first, "main_menu", user_id=789)
    await PanelOwnershipService.claim(db_session, 1, first.message_id, 789, "cards")
    await db_session.commit()

    guard = PanelGuardMiddleware()
    await guard(lambda event, _: handle_main_menu(event, bot), owner, {})
    assert len(bot.live()) == 1
    new_id = owner.message.message_id
    assert first.deleted == (not photo_entry)
    assert (new_id != first.message_id) == (not photo_entry)
    assert owner.message.kind == "photo"
    await db_session.rollback()
    assert await PanelOwnershipService.get_owner(db_session, 1, first.message_id) is None
    assert await PanelOwnershipService.get_owner(db_session, 1, new_id) is None
    await db_session.rollback()

    other = _callback(owner.message, "group_games_panel", user_id=999)
    called = []

    async def public_action(event, data):
        called.append(event.from_user.id)

    await guard(public_action, other, {})
    assert called == [999]


@pytest.mark.asyncio
async def test_failed_home_edit_does_not_unlock_the_original_personal_panel(db_session):
    bot = RecordingBot()
    original = _photo_message(bot)
    cb = _callback(original, "main_menu", user_id=789)
    await PanelOwnershipService.claim(db_session, 1, original.message_id, 789, "cards")
    await db_session.commit()
    bot.edit_message_media = AsyncMock(
        side_effect=TelegramBadRequest(None, "message can't be edited")
    )

    await PanelGuardMiddleware()(lambda event, _: handle_main_menu(event, bot), cb, {})
    assert cb.message is original and not original.deleted
    await db_session.rollback()
    assert await PanelOwnershipService.get_owner(db_session, 1, original.message_id) == 789


@pytest.mark.asyncio
async def test_shop_detail_to_banner_is_photo_media_swap(db_session):
    """Shop detail and shop banner each supply their own media to the renderer."""
    from sib.services.shop import ShopService

    character = await _shop_character(db_session, "sib_talayi")
    entries = await ShopService.list_entries(db_session, user_id=7)
    entry = next(e for e, c, owned in entries if c.id == character.id)
    await db_session.commit()
    bot = RecordingBot()
    original = _photo_message(bot)
    cb = _callback(original, f"shop_item:{entry.id}")
    await handle_shop_item(cb, bot)
    detail_art = cb.message.photo
    assert detail_art is not None
    cb.data = "shop_refresh"
    await handle_shop_refresh(cb, bot)
    assert cb.message is original and bot.live() == [original]
    assert not original.deleted and original.photo != detail_art
    assert [op[0] for op in bot.operations] == ["edit_media", "edit_media"]


@pytest.mark.asyncio
async def test_long_shop_card_preserves_photo_and_offers_full_description(db_session):
    from sib.services.shop import ShopService

    character = await _shop_character(db_session, "sib_sabz")
    character.lore_fa = "توضیح طولانی " * 100
    await db_session.commit()
    entries = await ShopService.list_entries(db_session, user_id=7)
    entry_id = next(e.id for e, c, owned in entries if c.id == character.id)
    await db_session.commit()

    bot = RecordingBot()
    original = _photo_message(bot)
    cb = _callback(original, f"shop_item:{entry_id}")
    await handle_shop_item(cb, bot)
    assert cb.message is original and not original.deleted
    assert cb.message.photo is not None and _units(cb.message.caption) <= CAPTION_LIMIT
    assert [op[0] for op in bot.operations] == ["edit_media"]

    cb.data = next(b.callback_data for b in _buttons(cb.message) if "متن کامل" in b.text)
    await handle_shop_item_full(cb, bot)
    assert cb.message.kind == "text" and character.lore_fa in cb.message.text
    assert len(bot.live()) == 1 and original.deleted
    assert any(
        c.startswith(f"shop_buy:{entry_id}:") for c in {b.callback_data for b in _buttons(cb.message)}
    )
    assert f"shop_item:{entry_id}" in {b.callback_data for b in _buttons(cb.message)}


@pytest.mark.asyncio
async def test_media_edit_updates_real_aiogram_callback_and_banner_cache(monkeypatch):
    """aiogram callback messages are frozen: the edit result has the NEW file_id."""
    from aiogram.types import Chat, Message, PhotoSize, User
    from sib.services import banner

    monkeypatch.setattr(banner, "_file_id_cache", {})
    old = Message(
        message_id=41, date=1720000, chat=Chat(id=7, type="private"),
        photo=[PhotoSize(file_id="OLD", file_unique_id="old", width=1, height=1)],
    )
    updated = Message(
        message_id=41, date=1720000, chat=old.chat, caption="new screen",
        photo=[PhotoSize(file_id="NEW", file_unique_id="new", width=1, height=1)],
    )
    cb = CallbackQuery(
        id="cb", from_user=User(id=7, is_bot=False, first_name="کاربر"),
        chat_instance="instance", message=old, data="apple_shop",
    )
    bot = MagicMock()
    bot.edit_message_media = AsyncMock(return_value=updated)
    monkeypatch.setattr(banner, "banner_photo", lambda key: "TARGET-ART")

    assert await banner.render_banner(cb, bot, "shop", "new screen")
    assert cb.message is updated  # same ID, but the actual edited photo
    assert banner._file_id_cache["shop"] == "NEW", "must not cache OLD art under the new banner"
    assert not bot.send_photo.called and not bot.delete_message.called


@pytest.mark.asyncio
async def test_failed_photo_send_does_not_replace_text_with_another_text():
    """If a media upload fails, its text fallback should reuse a text panel."""
    from sib.services.telegram_ui import safe_edit_callback_message

    class NoPhotoBot(RecordingBot):
        async def send_photo(self, chat_id, photo, **kwargs):
            self.operations.append(("send_photo_failed", chat_id, None))
            raise TelegramBadRequest(None, "wrong file identifier")

    bot = NoPhotoBot()
    original = _text_message(bot)
    cb = _callback(original, "wiki_char:1")
    await safe_edit_callback_message(cb, bot, "fallback", photo="BAD-ART")
    assert cb.message is original and bot.live() == [original]
    assert original.text == "fallback" and not original.deleted
    assert [op[0] for op in bot.operations] == ["send_photo_failed", "edit_text"]


@pytest.mark.asyncio
async def test_photo_send_retries_without_markdown_before_dropping_media():
    """Invalid Markdown in a shortened caption is not a reason to send TEXT."""
    from sib.services.telegram_ui import safe_edit_callback_message

    class BadMarkdownBot(RecordingBot):
        async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, **kwargs):
            if parse_mode == "Markdown":
                self.operations.append(("send_photo_markdown", chat_id, None))
                raise TelegramBadRequest(None, "can't parse entities")
            return await super().send_photo(chat_id, photo, caption=caption,
                                            parse_mode=parse_mode, **kwargs)

    bot = BadMarkdownBot()
    old = _text_message(bot)
    cb = _callback(old, "wiki_char:1")
    await safe_edit_callback_message(cb, bot, "x" * 1200, photo="ART")
    assert old.deleted and cb.message.kind == "photo"
    assert [op[0] for op in bot.operations] == ["send_photo_markdown", "send_photo", "delete"]


@pytest.mark.asyncio
async def test_unknown_type_real_change_replaces_and_retains_the_new_reference():
    """For a genuine mismatch the pair-only renderer still replaces once."""
    from sib.services.telegram_ui import safe_edit_screen

    bot = RecordingBot()
    original = _text_message(bot)
    new_id = await safe_edit_screen(bot, 1, original.message_id, "photo", photo="NEW")
    assert new_id != original.message_id
    assert original.deleted and bot.live()[0].photo == "NEW"
    assert [op[0] for op in bot.operations] == ["edit_media", "send_photo", "delete"]

    # Returning to a text-only screen is a second genuine type change.
    final_id = await safe_edit_screen(bot, 1, new_id, "text")
    assert final_id != new_id and bot.live()[0].kind == "text"
    assert len(bot.live()) == 1


@pytest.mark.asyncio
async def test_long_caption_on_text_to_photo_replacement_still_sends_photo():
    """A real type change must not drop its intended media on a long caption."""
    from sib.services.telegram_ui import safe_edit_callback_message

    bot = RecordingBot()
    old = _text_message(bot)
    cb = _callback(old, "wiki_char:1")
    await safe_edit_callback_message(cb, bot, "🤖" * 600, photo="CHAR-ART")
    assert old.deleted and len(bot.live()) == 1
    assert cb.message is bot.live()[0] and cb.message.kind == "photo"
    assert cb.message.photo == "CHAR-ART"
    assert _units(cb.message.caption) <= CAPTION_LIMIT
    assert [op[0] for op in bot.operations] == ["send_photo", "delete"]


@pytest.mark.asyncio
async def test_unknown_type_edit_failure_is_not_assumed_to_be_a_type_change():
    """A transient edit failure must not send/delete an otherwise live panel."""
    bot = RecordingBot()
    msg = _photo_message(bot)
    cb = _callback(msg, "unrelated")
    # Known PHOTO keeps the live panel on edit failure. The pair-only
    # renderer must do the same when it cannot prove a type change.
    bot.edit_message_media = AsyncMock(side_effect=TelegramBadRequest(None, "message can't be edited"))
    from sib.services.telegram_ui import safe_edit_screen

    result = await safe_edit_screen(bot, 1, msg.message_id, "screen", photo="NEW")
    assert result is None
    assert bot.live() == [msg] and not msg.deleted
    assert not any(op[0].startswith("send") or op[0] == "delete" for op in bot.operations)
