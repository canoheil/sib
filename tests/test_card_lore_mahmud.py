"""Mahmoud's exact official text and shared surfaces, without guessed lore.

The sixteenth card («محمود»): supplied affiliations, stats and story only.
Like the other supplied cards it ships as pure data - no role, no passive and
no strengths/weaknesses may be inferred from the story, and its art is the
bundled original ``assets/characters/mahmud.jpg`` (resolved by code).
"""

import pytest

from sib.database.connection import seed_characters
from sib.database.models import Character, PlayerCard, ReleaseStatus
from sib.handlers.cards import _card_view
from sib.handlers.shop import _shop_item_view
from sib.handlers.wiki import _wiki_index_lines
from sib.services.apple_wallet import AppleWalletService
from sib.services.card_upgrade import CardUpgradeService
from sib.services.characters import CharacterService, render_character_card
from sib.services.factions import FactionService
from sib.services.player_cards import PlayerCardService
from sib.services.shop import ShopService
from sib.services.telegram_ui import photo_caption
from sib.services.wiki import CollectionService, WikiService
from sib.tests.test_card_lore import RARE_PRICE_LINE as PRICE_LINE, _buttons, _entries, _seeded
from sib.tests.test_card_upgrade import _fund
from sib.tests.test_parties import create_test_user

MAHMUD_CODE = "mahmud"
MAHMUD_NAME = "محمود"
# Supplied affiliations. The half-space (ZWNJ) that used to sit inside
# «تیغ‌های» was replaced by ONE normal space so the line renders
# like the other empire cards; the author's spacing around the emoji is
# kept exactly as delivered. Nothing else of the string was touched.
MAHMUD_AFFILIATIONS = "امپراتوری 👑 - تیغ های تاج 🗡"
MAHMUD_STORY = "محمود را بسیاری به‌خاطر ظاهر عجیب و نگاه اخموش دست‌کم می‌گیرند؛ غافل از اینکه پشت آن چهرهٔ بامزه، ذهنی تیز و اراده‌ای سرسخت پنهان شده است. او ثابت کرده که حتی غیرمنتظره‌ترین جنگجویان هم می‌توانند در میان نخبگان امپراتوری جای بگیرند."
MAHMUD_WIKI = (
    f"*{MAHMUD_NAME}*\n\n*وابستگی‌ها*:{MAHMUD_AFFILIATIONS}\n\n"
    f"💪 قدرت: 18\n⚡ سرعت: 17\n🧠 هوش: 25\n\n*داستان*:{MAHMUD_STORY}"
)


def test_mahmud_seed_preserves_every_supplied_character_exactly():
    entry = next(entry for entry in _entries() if entry["code"] == MAHMUD_CODE)
    assert entry["name_fa"] == MAHMUD_NAME
    assert entry["name_en"] == "Mahmoud"
    assert entry["affiliations_fa"] == MAHMUD_AFFILIATIONS
    assert entry["lore_fa"] == MAHMUD_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (18, 17, 25)
    assert entry["faction"] == "empire" and entry["sync_faction"] is True
    assert entry["release_status"] == "RELEASED" and entry["shop_enabled"] is True
    assert entry["artwork"] is None  # bundled art resolves by code

    # Submitted spellings and text carriers survive storage untouched.
    assert "به‌خاطر" in entry["lore_fa"] and "دست‌کم می‌گیرند" in entry["lore_fa"]
    assert "اراده‌ای سرسخت" in entry["lore_fa"]
    assert "غیرمنتظره‌ترین جنگجویان" in entry["lore_fa"]
    assert "چهرهٔ بامزه" in entry["lore_fa"]  # hamza above (U+0654), as supplied
    assert "\u200c" in entry["lore_fa"] and "\u0654" in entry["lore_fa"]
    # The «Crown Blades» subgroup stays affiliation TEXT (never a faction
    # keyword). The half-space inside its name became one normal space -
    # that single code point is the whole visual fix.
    assert entry["affiliations_fa"] == MAHMUD_AFFILIATIONS
    assert entry["affiliations_fa"].endswith("🗡")
    assert "\u200c" not in entry["affiliations_fa"]  # no half-space left in this line
    assert "تیغ های تاج" in entry["affiliations_fa"]
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers",
                  "strengths", "weaknesses"):
        assert field not in entry, f"no gameplay or role must be invented for {field}"


async def test_mahmud_wiki_and_shop_show_full_story_and_separate_price(db_session):
    characters = await _seeded(db_session, shop_code="mahmud")
    mahmud = characters[MAHMUD_CODE]
    assert mahmud.name_fa == MAHMUD_NAME and mahmud.name_en == "Mahmoud"
    assert mahmud.affiliations_fa == MAHMUD_AFFILIATIONS
    assert mahmud.lore_fa == MAHMUD_STORY
    assert mahmud.stats_dict() == {"power": 18, "speed": 17, "intelligence": 25}
    assert mahmud.total_base_stats == 60
    assert mahmud.faction_role_fa is None and mahmud.passive_key is None
    assert mahmud.hidden_modifiers is None
    assert not mahmud.strengths_list and not mahmud.weaknesses_list
    faction = await FactionService.get_for_character(db_session, mahmud)
    assert faction is not None and faction.code == "empire"

    wiki = await WikiService.wiki_page(db_session, mahmud.id)
    assert wiki == MAHMUD_WIKI
    assert wiki.split("*داستان*:", 1)[1] == MAHMUD_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(mahmud, faction=faction) == wiki + "\n\n" + PRICE_LINE

    index = "\n".join(_wiki_index_lines([mahmud], {faction.id: faction}))
    assert "امپراتوری" in index and "👑" in index
    assert f"*{MAHMUD_NAME}*" in index and "بدون دسته" not in index
    # The new card joins its faction group in stable insertion order.
    ordered_ids = [character.id for character in await WikiService.list_wiki(db_session)]
    assert ordered_ids.index(characters["sib_sorkh"].id) < ordered_ids.index(mahmud.id)
    assert ordered_ids.index(characters["zarush"].id) < ordered_ids.index(mahmud.id)

    user = await create_test_user(db_session, "خریدار محمود رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == mahmud.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    card_body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert card_body == wiki + "\n\n" + PRICE_LINE
    assert card_body.endswith(PRICE_LINE) and card_body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in mahmud.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("old_faction", [None, "moon_descendants"])
async def test_mahmud_sync_preserves_other_cards_and_player_state(
    db_session, monkeypatch, old_faction
):
    characters = await _seeded(db_session)
    mahmud = characters[MAHMUD_CODE]
    empire = await FactionService.get_by_code(db_session, "empire")
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    user = await create_test_user(db_session, "حفظ وضعیت محمود")
    owned = await PlayerCardService.acquire(db_session, user.id, mahmud.id)
    owned.level = 6
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    # Simulate an old row/operator override while the official seed ships.
    mahmud.affiliations_fa = None
    mahmud.lore_fa = ""
    mahmud.faction_id = moon.id if old_faction else None
    mahmud.faction_role_fa = "نقش قبلی مدیریت‌شده"
    mahmud.artwork = "custom-mahmud-artwork"
    mahmud.release_status = ReleaseStatus.RETIRED
    mahmud.shop_enabled = False
    mahmud.is_active = False
    mahmud.price = 77
    characters["sib_sorkh"].faction_id = moon.id  # No opt-in on that entry.
    await db_session.commit()
    await db_session.refresh(mahmud)
    await db_session.refresh(owned)
    await db_session.refresh(characters["sib_sorkh"])
    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    mahmud_before = {key: getattr(mahmud, key) for key in preserve_keys}
    others_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != MAHMUD_CODE
    }
    player_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    histories_before = len(await CharacterService.get_versions(db_session, mahmud.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(mahmud)
    await db_session.refresh(owned)

    expected_fields = {"lore_fa", "affiliations_fa"}
    if old_faction:
        expected_fields.add("faction_id")
    assert writes == [(mahmud.id, expected_fields)]
    assert mahmud.affiliations_fa == MAHMUD_AFFILIATIONS and mahmud.lore_fa == MAHMUD_STORY
    assert mahmud.faction_id == empire.id
    assert {key: getattr(mahmud, key) for key in preserve_keys} == mahmud_before
    for code, before in others_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in before} == before
    assert {key: getattr(owned, key) for key in player_before} == player_before
    assert owned.level == 6 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, mahmud.id)) == histories_before

    # A repeated sync is a true no-op, including the timestamp.
    timestamp = mahmud.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(mahmud)
    assert writes == [(mahmud.id, expected_fields)]
    assert mahmud.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, mahmud.id)) == histories_before


async def test_mahmud_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    from pathlib import Path

    from aiogram.types import FSInputFile

    mahmud = (await _seeded(db_session, shop_code="mahmud"))[MAHMUD_CODE]
    user = await create_test_user(db_session, "بازی با محمود رسمی")
    await _fund(db_session, user, apples=2700, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == mahmud.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialmahmud01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialmahmud01")
    await db_session.commit()
    assert first.price == 2000 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700

    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{MAHMUD_NAME}*" in detail
    assert f"*وابستگی‌ها*:{MAHMUD_AFFILIATIONS}" in detail
    assert "💪 قدرت: `18`" in detail
    assert "⚡ سرعت: `17`" in detail
    assert "🧠 هوش: `25`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert MAHMUD_STORY not in detail and "*داستان*:" not in detail
    # The owned-card screen sends the card's own bundled original artwork.
    assert isinstance(photo, FSInputFile)
    assert Path(photo.path) == Path(__file__).resolve().parents[1] / "assets" / "characters" / "mahmud.jpg"
    assert f"card_main_set:{card.id}:0" in _buttons(keyboard)
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)

    await PlayerCardService.set_favorite(db_session, user.id, card.id, expected_main_card_id=0)
    await db_session.commit()
    result = await CardUpgradeService.upgrade(db_session, user.id, card.id, expected_level=1)
    await db_session.commit()
    assert (result.from_level, result.to_level) == (1, 2)
    assert await AppleWalletService.get_balance(db_session, user.id) == 600
    detail, main_card_id = await CollectionService.render_card_detail_with_main_card(
        db_session, user.id, card.id
    )
    assert main_card_id == card.id
    assert "💪 قدرت: `28`" in detail
    assert "⚡ سرعت: `27`" in detail
    assert "🧠 هوش: `35`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail
    assert MAHMUD_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
