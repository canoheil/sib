"""Official card text: exact storage, shared screens and safe scoped sync."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from sib.core.text import escape_markdown
from sib.database.connection import CharacterSeedError, seed_characters
from sib.database.models import Character, PlayerCard, ReleaseStatus
from sib.handlers.cards import _card_view
from sib.handlers.shop import _cards_view, _shop_item_view
from sib.handlers.wiki import _wiki_index_lines
from sib.services.apple_wallet import AppleWalletService
from sib.services.card_upgrade import CardUpgradeService
from sib.services.characters import CharacterService, render_character_card
from sib.services.player_cards import PlayerCardService
from sib.services.shop import ShopService
from sib.services.wiki import CollectionService, WikiService
from sib.tests.test_card_upgrade import _fund
from sib.tests.test_parties import create_test_user

SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "characters_seed.json"
RED_CODE = "sib_sorkh"
RED_NAME = "سیب سرخ"
RED_AFFILIATIONS = "امپراتوری👑 - تیغ های تاج🗡"
RED_STORY = "از میان جنگجویان امپراتوری، تنها افراد محدودی شایسته ایستادن در کنار زاریوش‌اند؛ و او یکی از همان افراد است. تیغه‌اش از پادشاهش سرپیچی نمیکند، اما هیچکس نمیداند پیش از آنکه سوگند وفاداری بخورد، چه چیزی را پشت سر گذاشته است."
RED_WIKI = (
    f"*{RED_NAME}*\n\n*وابستگی‌ها*:{RED_AFFILIATIONS}\n\n"
    f"💪 قدرت: 22\n⚡ سرعت: 20\n🧠 هوش: 18\n\n*داستان*:{RED_STORY}"
)
PRICE_LINE = "💰 قیمت: 300 سیب"


def _entries():
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


def _replace_seed(monkeypatch, entries):
    read_text = Path.read_text
    text = json.dumps(entries, ensure_ascii=False)

    def fake_read(path, *args, **kwargs):
        if path == SEED_PATH:
            return text
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read)


async def _seeded(session):
    await seed_characters(session)
    await session.commit()
    return {
        character.code: character
        for character in (await session.scalars(select(Character))).all()
    }


def _buttons(keyboard):
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def test_red_apple_seed_is_the_exact_official_message():
    entry = next(entry for entry in _entries() if entry["code"] == RED_CODE)
    assert entry["name_fa"] == RED_NAME
    assert entry["affiliations_fa"] == RED_AFFILIATIONS
    assert entry["lore_fa"] == RED_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (22, 20, 18)
    # In particular, do not "correct" these spellings or the story's name.
    assert "زاریوش‌اند" in entry["lore_fa"]
    assert "نمیکند" in entry["lore_fa"] and "هیچکس نمیداند" in entry["lore_fa"]


async def test_red_apple_wiki_and_shop_share_exact_content(db_session):
    characters = await _seeded(db_session)
    red = characters[RED_CODE]
    assert red.name_fa == RED_NAME
    assert red.affiliations_fa == RED_AFFILIATIONS
    assert red.lore_fa == RED_STORY
    wiki = await WikiService.wiki_page(db_session, red.id)
    assert wiki == RED_WIKI
    assert "قیمت" not in wiki
    assert render_character_card(red) == RED_WIKI + "\n\n" + PRICE_LINE
    assert "نقاط قوت" not in wiki and "🎖 نقش" not in wiki and "📜" not in wiki

    user = await create_test_user(db_session, "خریدار لور رسمی")
    await db_session.commit()
    entries = await ShopService.list_entries(db_session, user_id=user.id)
    entry = next(entry for entry, character, _ in entries if character.code == RED_CODE)
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    card_body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert card_body == wiki + "\n\n" + PRICE_LINE
    assert card_body.count(PRICE_LINE) == 1
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


async def test_bold_labels_and_card_names_apply_to_all_cards(db_session):
    characters = await _seeded(db_session)
    assert len(characters) == 16
    for character in characters.values():
        wiki = await WikiService.wiki_page(db_session, character.id)
        assert wiki.startswith(f"*{escape_markdown(character.name_fa)}*\n")
        assert "*وابستگی‌ها*:" in wiki
        assert "*داستان*:" in wiki
        assert PRICE_LINE not in wiki
        assert f"*{escape_markdown(character.name_fa)}*" in WikiService.wiki_index_entry(character, 1)
    index = "\n".join(_wiki_index_lines(list(characters.values())))
    for character in characters.values():
        assert f"*{escape_markdown(character.name_fa)}*" in index

    user = await create_test_user(db_session, "قالب مشترک")
    await db_session.commit()
    from sib.handlers.shop import SHOP_CARDS_PAGE_SIZE

    entries = await ShopService.list_entries(db_session, user_id=user.id)
    page_count = (len(entries) + SHOP_CARDS_PAGE_SIZE - 1) // SHOP_CARDS_PAGE_SIZE
    await db_session.commit()  # the listing renderer uses its own session
    listing_pages = []
    for page in range(page_count):
        page_text, _keyboard = await _cards_view(user.id, page=page)
        listing_pages.append(page_text)
    listing = "\n".join(listing_pages)
    for character in characters.values():
        assert f"*{escape_markdown(character.name_fa)}*" in listing


async def test_official_sync_preserves_other_cards_and_player_state(db_session):
    characters = await _seeded(db_session)
    red = characters[RED_CODE]
    # A separate unmarked operator definition remains outside official sync;
    # Igris now has supplied official text and is no longer an unmarked fixture.
    other = await CharacterService.create(
        db_session, code="unmarked_preservation_probe", name_fa="کارت آزمایشی بدون لور رسمی",
        power=20, speed=20, intelligence=20,
    )
    user = await create_test_user(db_session, "نگهداری وضعیت")
    owned = await PlayerCardService.acquire(db_session, user.id, red.id)
    owned.level = 7
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    # Emulate an existing installation before the official content shipped.
    red.name_fa = "نام قبلی"
    red.lore_fa = "داستان قبلی تولیدشده"
    red.affiliations_fa = None
    red.artwork = "custom-artwork"
    red.shop_enabled = False
    red.is_active = False
    red.release_status = ReleaseStatus.RETIRED
    red.price = 77  # historical column must not change the 300-Apple rule
    other.name_fa = "نام ویرایش‌شدهٔ کارت دیگر"
    other.lore_fa = "داستان اختصاصی کارت دیگر"
    await db_session.commit()
    await db_session.refresh(red)
    await db_session.refresh(other)
    await db_session.refresh(owned)
    preserve_fields = (
        "artwork", "faction_id", "faction_role_fa", "passive_key", "passive_data",
        "hidden_modifiers", "strengths", "weaknesses", "release_status", "released_at",
        "is_active", "shop_enabled", "price", "rarity", "balance_version",
    )
    red_before = {key: getattr(red, key) for key in preserve_fields}
    other_before = {
        column.name: getattr(other, column.name) for column in Character.__table__.columns
    }
    card_before = {
        column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns
    }
    histories_before = len(await CharacterService.get_versions(db_session, red.id))

    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(red)
    await db_session.refresh(other)
    await db_session.refresh(owned)
    assert red.name_fa == RED_NAME and red.lore_fa == RED_STORY
    assert red.affiliations_fa == RED_AFFILIATIONS
    assert {key: getattr(red, key) for key in preserve_fields} == red_before
    assert {key: getattr(other, key) for key in other_before} == other_before
    assert {key: getattr(owned, key) for key in card_before} == card_before
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, red.id)) == histories_before

    # Once synced, another startup does not rewrite even the timestamp.
    timestamp = red.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(red)
    assert red.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, red.id)) == histories_before


async def test_official_stat_sync_uses_normal_history_once(db_session):
    red = (await _seeded(db_session))[RED_CODE]
    await CharacterService.update(db_session, red.id, power=23, speed=19)
    await db_session.commit()
    previous_version = red.balance_version
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    assert red.stats_dict() == {"power": 22, "speed": 20, "intelligence": 18}
    assert red.balance_version == previous_version + 1
    versions = await CharacterService.get_versions(db_session, red.id)
    assert json.loads(versions[0].snapshot)["power"] == 22
    count = len(versions)
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    assert len(await CharacterService.get_versions(db_session, red.id)) == count


async def test_official_purchase_main_card_upgrade_and_back_are_preserved(db_session):
    red = (await _seeded(db_session))[RED_CODE]
    user = await create_test_user(db_session, "بازی با کارت رسمی")
    await _fund(db_session, user, apples=1000, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == red.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialred01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialred01")
    await db_session.commit()
    assert first.price == 300 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700
    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{RED_NAME}*" in detail
    assert f"*وابستگی‌ها*:{RED_AFFILIATIONS}" in detail
    assert RED_STORY not in detail and "*داستان*:" not in detail
    assert photo is not None
    assert f"card_main_set:{card.id}:0" in _buttons(keyboard)
    assert f"card_upgrade:{card.id}" in _buttons(keyboard)
    assert "apple_cards" in _buttons(keyboard)

    await PlayerCardService.set_favorite(db_session, user.id, card.id, expected_main_card_id=0)
    await db_session.commit()
    result = await CardUpgradeService.upgrade(db_session, user.id, card.id, expected_level=1)
    await db_session.commit()
    assert (result.from_level, result.to_level) == (1, 2)
    assert await AppleWalletService.get_balance(db_session, user.id) == 600
    detail, main_id = await CollectionService.render_card_detail_with_main_card(db_session, user.id, card.id)
    assert main_id == card.id
    assert "💪 قدرت: `32`" in detail and "⚡ سرعت: `30`" in detail and "🧠 هوش: `28`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail
    assert RED_STORY not in detail


async def test_storage_keeps_whitespace_punctuation_emoji_and_markup(db_session):
    name = "نام_رسمی* [کارت]"
    affiliations = "  دسته_الف👑 - تیغ*تاج🗡  \nگروه دوم"
    story = "  داستان_عیناً* با `نشانه` و [پرانتز] و \\ خط 😄\n\nجمله نمیکند!  \n"
    card = await CharacterService.create(
        db_session, code="verbatim_test", name_fa=name, affiliations_fa=affiliations,
        lore_fa=story, power=22, speed=20, intelligence=18,
        release_status=ReleaseStatus.RELEASED,
    )
    await db_session.commit()
    await db_session.refresh(card)
    assert card.name_fa == name and card.affiliations_fa == affiliations and card.lore_fa == story
    page = await WikiService.wiki_page(db_session, card.id)
    assert page.startswith(f"*{escape_markdown(name)}*\n")
    assert f"*وابستگی‌ها*:{escape_markdown(affiliations)}" in page
    assert page.split("*داستان*:", 1)[1] == escape_markdown(story)
    assert render_character_card(card) == page + "\n\n" + PRICE_LINE


@pytest.mark.parametrize("invalid", [
    {"affiliations_fa": None},
    {"power": "22"},
    {"power": 23},
])
async def test_invalid_official_content_rolls_back_the_sync(db_session, monkeypatch, invalid):
    red = (await _seeded(db_session))[RED_CODE]
    red.lore_fa = "نسخه قبلی برای بررسی rollback"
    await db_session.commit()
    red_id = red.id
    previous_story = red.lore_fa
    entries = _entries()
    bad = dict(next(entry for entry in entries if entry["code"] == RED_CODE))
    bad["code"] = "broken_official"
    bad.update(invalid)
    entries.append(bad)
    _replace_seed(monkeypatch, entries)
    with pytest.raises(CharacterSeedError):
        await seed_characters(db_session)
    await db_session.rollback()
    restored = await db_session.get(Character, red_id)
    assert restored.lore_fa == previous_story
    assert await CharacterService.get_by_code(db_session, "broken_official") is None
