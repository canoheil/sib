"""Character card art — resolution, wiki/shop/collection/profile/battle wiring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


async def _shop_character(db_session, code: str, price: int = 100):
    """A released, shop-enabled character whose `code` maps to framed art."""
    from sib.database.models import ReleaseStatus
    from sib.services.characters import CharacterService

    character = await CharacterService.create(
        db_session,
        code=code,
        name_fa=f"قهرمان {code}",
        lore_fa="توضیح آزمایشی شخصیت برای صفحه ویکی.",
        power=22,
        speed=20,
        intelligence=18,
        price=price,
        passive_key="test_passive",
    )
    await CharacterService.set_release_status(db_session, character.id, ReleaseStatus.RELEASED)
    character.shop_enabled = True
    character.is_active = True
    await db_session.commit()
    return character


def test_framed_card_assets_exist():
    """All released characters ship framed card art + the canonical frame."""
    from pathlib import Path

    from sib.services.character_art import ASSETS_DIR

    root = Path(__file__).resolve().parent.parent
    frame = root / "assets" / "frames" / "card_frame_gold.jpg"
    assert frame.exists(), "canonical golden frame asset is missing"
    for code in ("sib_sorkh", "sib_sabz", "sib_talayi", "sib_mahi"):
        assert (ASSETS_DIR / f"{code}.jpg").exists(), f"missing framed card for {code}"
    # Framing is deterministic + reusable: the script must ship with the repo.
    assert (root / "scripts" / "frame_card.py").exists()


@pytest.mark.asyncio
async def test_artwork_path_and_fallback(db_session):
    from sib.services.character_art import (
        artwork_path,
        photo_arg,
        resolve_character_photo,
    )

    character = await _shop_character(db_session, "sib_sorkh")
    assert artwork_path(character) is not None
    assert artwork_path(SimpleNamespace(code="no_such_card", artwork=None)) is None
    assert artwork_path(SimpleNamespace(code=None, artwork=None)) is None

    file_id, path = await resolve_character_photo(db_session, character)
    assert file_id is None and path is not None
    assert photo_arg(file_id, path) is not None
    file_id, path = await resolve_character_photo(
        db_session, SimpleNamespace(code="no_such_card", artwork=None)
    )
    assert file_id is None and path is None
    assert photo_arg(file_id, path) is None


@pytest.mark.asyncio
async def test_wiki_char_page_shows_art(db_session):
    from aiogram.types import CallbackQuery

    from sib.handlers.wiki import handle_wiki_char

    character = await _shop_character(db_session, "sib_sabz")

    cb = MagicMock(spec=CallbackQuery)
    cb.data = f"wiki_char:{character.id}"
    cb.from_user = MagicMock(id=4242, is_bot=False, first_name="ویکی")
    cb.message = MagicMock()
    cb.message.chat = MagicMock(id=1, type="private")
    cb.message.message_id = 10
    cb.message.content_type = "text"
    cb.answer = AsyncMock()

    bot = MagicMock()
    bot.edit_message_media = AsyncMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()

    await handle_wiki_char(cb, bot)

    # Photo message swap is attempted first (art exists for seeded cards).
    assert bot.edit_message_media.await_count == 1
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.caption  # description travels as caption


@pytest.mark.asyncio
async def test_shop_item_detail_full_screen(db_session):
    from sib.services.shop import ShopService

    character = await _shop_character(db_session, "sib_talayi", price=300)
    async with __import__("sib.database.connection", fromlist=["get_session"]).get_session() as session:
        entries = await ShopService.list_entries(session, user_id=989898)
        await session.commit()
    assert entries, "shop rotation should offer entries"
    entry, listed_character, owned = next(
        t for t in entries if t[1].id == character.id
    )

    from sib.handlers.shop import _shop_item_view

    text, kb, art_photo = await _shop_item_view(989898, entry.id)
    assert character.name_fa in text
    assert "قیمت" in text
    assert "توضیح آزمایشی" in text  # wiki description travels to the detail screen
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"shop_buy:{entry.id}" in callbacks  # confirm — purchase handler untouched
    assert "shop_refresh" in callbacks  # back to shop
    assert art_photo is not None  # framed art resolved

    # Unknown entry degrades safely.
    text, kb, art_photo = await _shop_item_view(989898, 99999999)
    assert "نیست" in text and art_photo is None


@pytest.mark.asyncio
async def test_shop_list_buttons_open_detail_not_purchase(db_session):
    """List → detail screen; purchase only happens through confirm."""
    from sib.handlers.shop import _shop_view

    await _shop_character(db_session, "sib_mahi")
    text, kb = await _shop_view(777777)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(c.startswith("shop_item:") for c in callbacks)
    assert not any(c.startswith("shop_buy:") for c in callbacks)
    assert "فروشگاه" in text


@pytest.mark.asyncio
async def test_safe_edit_screen_caption_first_then_text():
    from sib.services.telegram_ui import safe_edit_screen

    bot = MagicMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()

    ok = await safe_edit_screen(bot, 1, 2, "متن", None)
    assert ok and bot.edit_message_caption.await_count == 1
    assert bot.edit_message_text.await_count == 0

    bot2 = MagicMock()
    bot2.edit_message_caption = AsyncMock(side_effect=Exception("no caption"))
    bot2.edit_message_text = AsyncMock()
    ok = await safe_edit_screen(bot2, 1, 2, "متن", None)
    assert ok and bot2.edit_message_text.await_count == 1


@pytest.mark.asyncio
async def test_profile_shows_favorite_card_art(db_session):
    from aiogram.types import Message

    from sib.services.player_cards import PlayerCardService
    from sib.tests.test_parties import create_test_user

    character = await _shop_character(db_session, "sib_sorkh")
    user = await create_test_user(db_session, "آرت‌باز")
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await PlayerCardService.set_favorite(db_session, user.id, card.id, True)
    await db_session.commit()
    uid = user.id

    msg = MagicMock(spec=Message)
    msg.from_user = MagicMock(id=uid, is_bot=False, first_name="آرت‌باز", full_name="آرت‌باز")
    msg.chat = MagicMock(id=uid, type="private")
    msg.reply = AsyncMock(return_value=MagicMock(message_id=1))
    msg.reply_photo = AsyncMock(return_value=MagicMock(message_id=2))

    bot = MagicMock()
    from sib.handlers.apple_battle import cmd_profile

    await cmd_profile(msg, bot)

    assert msg.reply_photo.await_count == 1
    caption = msg.reply_photo.await_args.kwargs["caption"]
    assert "کارت اصلی" in caption
