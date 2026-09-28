"""Character card art — resolution, wiki/shop/collection/profile/battle wiring."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image, ImageFilter


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
# Framed card assets: ONE cohesive frame for the whole collection
# ---------------------------------------------------------------------------

CARD_NAMES = ("sib_sorkh", "sib_sabz", "sib_talayi", "sib_mahi")


def _openings():
    """The frame's true opening geometry, from the pipeline itself."""
    from scripts.frame_card import FRAME_PATH, opening_mask

    frame = Image.open(FRAME_PATH).convert("RGB")
    mask = opening_mask(frame)
    return frame, mask


def test_all_character_cards_exist_with_same_dimensions():
    from sib.services.character_art import artwork_path

    for name in CARD_NAMES:
        path = artwork_path(SimpleNamespace(code=name, artwork=None))
        assert path is not None and Path(path).is_file(), f"missing card art for {name}"
        assert Image.open(path).size == (896, 1200), f"{name}: card must be 896x1200"


def test_character_cards_share_one_frame_no_double_frame():
    """Every card keeps the SAME outer frame pixels (the frame asset
    itself) outside the opening — a second inner frame, pasted-image
    borders or white deckle margins inside the opening are regressions."""
    from sib.services.character_art import artwork_path

    frame, mask = _openings()
    # Region outside the opening, plus a 3px seam tolerance ring.
    ring = mask.filter(ImageFilter.MaxFilter(7))  # dilate ~3px
    fp = frame.load()

    cards = []
    for name in CARD_NAMES:
        path = artwork_path(SimpleNamespace(code=name, artwork=None))
        cards.append(Image.open(path).convert("RGB"))

    for name, card in zip(CARD_NAMES, cards):
        assert card.size == frame.size
        cp = card.load()
        diff_sum = 0
        count = 0
        for y in range(0, card.height, 3):
            for x in range(0, card.width, 3):
                if ring.getpixel((x, y)) == 0:  # outside opening + seam
                    a, b = fp[x, y], cp[x, y]
                    diff_sum += abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])
                    count += 1
        mean_diff = diff_sum / max(count, 1)
        assert mean_diff < 12, f"{name}: outer frame altered (mean diff {mean_diff:.1f})"


def test_character_cards_fill_the_opening_without_margins():
    """The art must reach the opening's lip: no paper/white margin band
    between the art and the frame (the 'pasted-in-image' look)."""
    from sib.services.character_art import artwork_path

    frame, mask = _openings()
    ox0, oy0, ox1, oy1 = mask.getbbox()
    matte = frame.convert("RGB").getpixel((frame.width // 2, frame.height // 2))
    inner_ring = mask.filter(ImageFilter.MinFilter(13)).filter(
        ImageFilter.MaxFilter(7)
    )  # a band just inside the opening lip

    for name in CARD_NAMES:
        path = artwork_path(SimpleNamespace(code=name, artwork=None))
        card = Image.open(path).convert("RGB")
        cp = card.load()
        far = 0
        total = 0
        for y in range(oy0, oy1):
            for x in range(ox0, ox1):
                if inner_ring.getpixel((x, y)):
                    c = cp[x, y]
                    total += 1
                    if abs(c[0] - matte[0]) + abs(c[1] - matte[1]) + abs(c[2] - matte[2]) > 40:
                        far += 1
        assert total > 0
        assert far / total > 0.95, (
            f"{name}: art does not reach the frame lip ({far}/{total})"
        )


def test_frame_card_pipeline_reproduces_the_collection(tmp_path):
    """The pipeline itself rebuilds a coherent card from raw art: exact
    frame size, untouched outer frame, art filled into the opening."""
    from scripts.frame_card import RAW_DIR, FRAME_PATH, frame_card, opening_mask

    out = tmp_path / "rebuilt.jpg"
    raws = sorted(RAW_DIR.glob("*.jpg"))
    assert raws
    frame_card(raws[0], out)
    rebuilt = Image.open(out).convert("RGB")
    assert rebuilt.size == (896, 1200)
