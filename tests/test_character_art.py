"""Character card art — resolution, wiki/shop/collection/profile/battle wiring."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from sib.core.text import escape_markdown


async def _shop_character(db_session, code: str):
    """A released, shop-enabled character whose `code` maps to framed art.

    Cards have no per-card price: the unified shop price applies."""
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
        passive_key="test_passive",
    )
    await CharacterService.set_release_status(db_session, character.id, ReleaseStatus.RELEASED)
    character.shop_enabled = True
    character.is_active = True
    await db_session.commit()
    return character


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
    """A text message showing a photo screen is REPLACED by a photo message.

    Telegram cannot convert text→photo in place: the old message is removed
    and the art + description are sent as the new screen (the description
    travels as the caption). No stale/missing art, no orphan messages.
    """
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
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=77, photo=None))
    bot.delete_message = AsyncMock()

    await handle_wiki_char(cb, bot)

    # The photo screen is sent as a NEW photo message ...
    assert bot.send_photo.await_count == 1
    media = bot.send_photo.await_args.args[1] if len(bot.send_photo.await_args.args) > 1 else bot.send_photo.await_args.kwargs["photo"]
    caption = bot.send_photo.await_args.kwargs.get("caption")
    assert caption  # description travels as caption
    # ... and the old text message is deleted (no orphans, no stale media).
    bot.delete_message.assert_awaited_once()
    assert bot.delete_message.await_args.args[1] == 10 or bot.delete_message.await_args.kwargs.get("message_id") == 10
    # The live reference now points at the new message.
    assert cb.message.message_id == 77


@pytest.mark.asyncio
async def test_shop_item_detail_full_screen(db_session):
    from sib.services.shop import ShopService

    character = await _shop_character(db_session, "sib_talayi")
    async with __import__("sib.database.connection", fromlist=["get_session"]).get_session() as session:
        entries = await ShopService.list_entries(session, user_id=989898)
        await session.commit()
    assert entries, "shop rotation should offer entries"
    entry, listed_character, owned = next(
        t for t in entries if t[1].id == character.id
    )

    from sib.handlers.shop import _shop_item_view

    text, kb, art_photo = await _shop_item_view(989898, entry.id)
    assert escape_markdown(character.name_fa) in text
    assert "قیمت" in text
    assert "توضیح آزمایشی" in text  # wiki description travels to the detail screen
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert any(c.startswith(f"shop_buy:{entry.id}:") for c in callbacks)  # confirm (nonce-bearing)
    assert "shop_cards:0" in callbacks  # back to the Cards section
    assert art_photo is not None  # framed art resolved

    # Unknown entry degrades safely.
    text, kb, art_photo = await _shop_item_view(989898, 99999999)
    assert "نیست" in text and art_photo is None


@pytest.mark.asyncio
async def test_shop_list_buttons_open_detail_not_purchase(db_session):
    """Root → sections; section list → detail; purchase only via confirm."""
    from sib.handlers.shop import _cards_view, _shop_view

    await _shop_character(db_session, "sib_mahi")
    text, kb = await _shop_view(777777)
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    # Root offers the three sections, never a direct purchase.
    assert "shop_cards" in callbacks
    assert "shop_goods" in callbacks
    assert "shop_sell" in callbacks
    assert not any(c.startswith("shop_buy:") for c in callbacks)
    assert "فروشگاه" in text

    # The Cards section lists items that open a DETAIL screen.
    _cards_text, cards_kb = await _cards_view(777777)
    card_callbacks = [
        b.callback_data for row in cards_kb.inline_keyboard for b in row
    ]
    assert any(c.startswith("shop_item:") for c in card_callbacks)
    assert not any(c.startswith("shop_buy:") for c in card_callbacks)


@pytest.mark.asyncio
async def test_safe_edit_screen_transitions_leave_no_stale_media():
    """safe_edit_screen renders the target screen's OWN media — never keeps
    a previous screen's photo.

    The old caption-first behavior edited the caption of a photo message
    and silently KEPT its art on text screens (the "photos follow the
    user" bug). The transition contract now is:

    - text→text / photo→photo: edited in place (stable message id);
    - text↔photo: replaced (new message + old deleted) and the returned
      message id moves to the new message.
    """
    from sib.services.telegram_ui import safe_edit_screen

    # TEXT → TEXT: plain text edit, id stays.
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    new_id = await safe_edit_screen(bot, 1, 2, "متن", None, current_is_photo=False)
    assert new_id == 2
    assert bot.edit_message_text.await_count == 1
    assert not getattr(bot, "send_message").called and not getattr(bot, "send_photo").called

    # PHOTO → TEXT: the photo must NOT survive — replace with a text message.
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=9))
    bot.delete_message = AsyncMock()
    new_id = await safe_edit_screen(bot, 1, 2, "متن", None, current_is_photo=True)
    assert new_id == 9  # reference moved to the replacement
    assert bot.send_message.await_count == 1
    assert bot.delete_message.await_count == 1

    # PHOTO → PHOTO: media swap in place, id stays.
    bot = MagicMock()
    bot.edit_message_media = AsyncMock()
    new_id = await safe_edit_screen(bot, 1, 2, "متن", photo="photo-1", current_is_photo=True)
    assert new_id == 2
    assert bot.edit_message_media.await_count == 1

    # TEXT → PHOTO: replace with a photo message.
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=11))
    bot.delete_message = AsyncMock()
    new_id = await safe_edit_screen(bot, 1, 2, "متن", photo="photo-2", current_is_photo=False)
    assert new_id == 11
    assert bot.send_photo.await_count == 1
    assert bot.delete_message.await_count == 1


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


# ---------------------------------------------------------------------------
# Delivered card art is the card — no frame, no crop, no re-encode
# ---------------------------------------------------------------------------
# The images in ``assets/characters/`` ARE the cards: the artist bakes the
# frame, the name plate and the crown into the picture. The game only resolves
# the path and hands it to Telegram, so these tests pin the *delivery* contract
# (a 4:5 portrait that Telegram accepts), never a processing step. The old
# golden-frame pipeline (``scripts/frame_card.py``) belonged to an early
# version of the game and must not touch these files.

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "characters_seed.json"

#: Every single card in the collection is drawn at 4:5 (the artists' house
#: format); 0.8 is the target and the tolerance only absorbs rounding.
CARD_RATIO = 4 / 5
CARD_RATIO_TOLERANCE = 0.006
#: Telegram's own limit for a bot photo; anything bigger cannot be sent.
MAX_CARD_BYTES = 10 * 1024 * 1024
#: Below this the card looks soft on a phone screen.
MIN_CARD_SIDE = 800


def _seed_codes() -> list[str]:
    import json

    with open(SEED_FILE, encoding="utf-8") as handle:
        return [row["code"] for row in json.load(handle)]


def test_every_seeded_card_ships_art():
    """A card without art would render as a text-only wiki/profile screen."""
    from sib.services.character_art import artwork_path

    codes = _seed_codes()
    assert codes, "the roster seed is empty"
    missing = [
        code
        for code in codes
        if artwork_path(SimpleNamespace(code=code, artwork=None)) is None
    ]
    assert not missing, f"missing card art: {missing}"


def test_card_art_matches_the_house_format():
    """4:5 portrait, sharp enough, small enough — exactly as delivered."""
    from sib.services.character_art import artwork_path

    problems = []
    for code in _seed_codes():
        path = artwork_path(SimpleNamespace(code=code, artwork=None))
        assert path is not None and Path(path).is_file(), code
        size = Path(path).stat().st_size
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
        if image_format not in ("JPEG", "PNG"):
            problems.append(f"{code}: {image_format} is not a Telegram photo")
        ratio = width / height
        if abs(ratio - CARD_RATIO) > CARD_RATIO_TOLERANCE:
            problems.append(f"{code}: ratio {ratio:.4f} is not 4:5")
        if min(width, height) < MIN_CARD_SIDE:
            problems.append(f"{code}: {width}x{height} is too small")
        if size > MAX_CARD_BYTES:
            problems.append(f"{code}: {size / 1024:.0f}KB exceeds Telegram's limit")
    assert not problems, "card art delivery problems: " + "; ".join(problems)


def test_artwork_resolution_returns_the_delivered_file_untouched():
    """``artwork_path`` is a filesystem lookup, never a transform step."""
    from sib.services.character_art import artwork_path

    for code in ("hero", "sib_sorkh", "igris"):
        resolved = artwork_path(SimpleNamespace(code=code, artwork=None))
        assert Path(resolved).name == f"{code}.jpg"
        assert Path(resolved).parent.name == "characters"


def test_seed_never_overrides_artwork_with_a_processed_asset():
    """``artwork`` stays null so resolution is always ``{code}.jpg``."""
    import json

    with open(SEED_FILE, encoding="utf-8") as handle:
        rows = json.load(handle)
    overridden = [row["code"] for row in rows if row.get("artwork") is not None]
    assert not overridden, f"cards may not override their art: {overridden}"


def test_no_product_code_composites_a_frame_over_the_art():
    """The golden-frame pipeline is retired: no runtime code may use it."""
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for directory in ("services", "handlers", "keyboards", "games"):
        for path in (root / directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "card_frame" in text or "frame_card" in text:
                offenders.append(str(path.relative_to(root)))
    assert not offenders, f"retired frame pipeline still referenced: {offenders}"
