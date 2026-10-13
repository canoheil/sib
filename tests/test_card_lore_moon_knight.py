"""Moon Knight's exact text, raw sub-affiliations and preserved play state."""

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
from sib.tests.test_card_lore import PRICE_LINE, _buttons, _entries, _seeded
from sib.tests.test_card_upgrade import _fund
from sib.tests.test_parties import create_test_user

MOON_KNIGHT_CODE = "moon_knight"
MOON_KNIGHT_NAME = "شوالیه ماه"
MOON_KNIGHT_AFFILIATIONS = "فرزندان ماه🌙 - پنجه‌های ماه🐾"
MOON_KNIGHT_STORY = "شوالیهٔ ماه، یکی از نخبگان پنجه‌های ماه؛ گروهی که ملکهٔ ماهی قدرتمندترین خدمتگزارانش را از میان آن‌ها برمی‌گزیند. شمشیر نقرین او سلاحی مهیب و سرکش است، اما برخلاف دیگران، شوالیه نه از قدرت آن هراس دارد و نه در برابرش سر خم می‌کند. او تیغه‌ای را مهار کرده که شاید هیچ‌کس دیگری شایستهٔ در دست گرفتنش نباشد."
MOON_KNIGHT_WIKI = (
    f"*{MOON_KNIGHT_NAME}*\n\n*وابستگی‌ها*:{MOON_KNIGHT_AFFILIATIONS}\n\n"
    f"💪 قدرت: 30\n⚡ سرعت: 20\n🧠 هوش: 10\n\n*داستان*:{MOON_KNIGHT_STORY}"
)


def test_moon_knight_seed_preserves_all_official_fields_verbatim():
    entry = next(entry for entry in _entries() if entry["code"] == MOON_KNIGHT_CODE)
    assert entry["name_fa"] == MOON_KNIGHT_NAME
    assert entry["affiliations_fa"] == MOON_KNIGHT_AFFILIATIONS
    assert entry["lore_fa"] == MOON_KNIGHT_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (30, 20, 10)
    assert entry["faction"] == "moon_descendants" and entry["sync_faction"] is True
    assert "شوالیهٔ ماه" in entry["lore_fa"] and "ملکهٔ ماهی" in entry["lore_fa"]
    assert "شمشیر نقرین" in entry["lore_fa"]
    assert "تیغه‌ای را مهار کرده" in entry["lore_fa"]
    assert "هیچ‌کس دیگری شایستهٔ در دست گرفتنش نباشد." in entry["lore_fa"]
    # The subgroup is saved verbatim as affiliation text, not parsed into a role.
    assert "پنجه‌های ماه🐾" in entry["affiliations_fa"]
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses"):
        assert field not in entry


async def test_moon_knight_wiki_and_shop_render_the_complete_official_text(db_session):
    characters = await _seeded(db_session)
    knight = characters[MOON_KNIGHT_CODE]
    assert knight.name_fa == MOON_KNIGHT_NAME
    assert knight.affiliations_fa == MOON_KNIGHT_AFFILIATIONS
    assert knight.lore_fa == MOON_KNIGHT_STORY
    assert knight.stats_dict() == {"power": 30, "speed": 20, "intelligence": 10}
    assert knight.faction_role_fa is None and knight.passive_key is None
    assert knight.hidden_modifiers is None and not knight.strengths_list and not knight.weaknesses_list
    faction = await FactionService.get_for_character(db_session, knight)
    assert faction is not None and faction.code == "moon_descendants"

    wiki = await WikiService.wiki_page(db_session, knight.id)
    assert wiki == MOON_KNIGHT_WIKI
    assert wiki.split("*داستان*:", 1)[1] == MOON_KNIGHT_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(knight, faction=faction) == wiki + "\n\n" + PRICE_LINE

    index = "\n".join(_wiki_index_lines([knight], {faction.id: faction}))
    assert "فرزندان ماه" in index and "🌙" in index
    assert f"*{MOON_KNIGHT_NAME}*" in index and "بدون دسته" not in index
    ordered = await WikiService.list_wiki(db_session)
    ordered_ids = [character.id for character in ordered]
    assert ordered_ids.index(characters["sib_mahi"].id) < ordered_ids.index(characters["mmung"].id)
    assert ordered_ids.index(characters["mmung"].id) < ordered_ids.index(knight.id)

    user = await create_test_user(db_session, "خریدار شوالیه ماه رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == knight.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    card_body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert card_body == wiki + "\n\n" + PRICE_LINE
    assert card_body.endswith(PRICE_LINE) and card_body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in knight.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("previous_faction", [None, "empire"])
async def test_moon_knight_sync_preserves_other_cards_and_player_state(db_session, monkeypatch, previous_faction):
    characters = await _seeded(db_session)
    knight = characters[MOON_KNIGHT_CODE]
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    empire = await FactionService.get_by_code(db_session, "empire")
    user = await create_test_user(db_session, "حفظ وضعیت شوالیه ماه")
    owned = await PlayerCardService.acquire(db_session, user.id, knight.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    # An old row may have a missing or manually overridden faction link.
    knight.affiliations_fa = None
    knight.lore_fa = ""
    knight.faction_id = empire.id if previous_faction else None
    knight.faction_role_fa = "نقش قبلی مدیریت‌شده"
    knight.artwork = "custom-moon-knight-artwork"
    knight.release_status = ReleaseStatus.RETIRED
    knight.shop_enabled = False
    knight.is_active = False
    knight.price = 77
    await db_session.commit()
    await db_session.refresh(knight)
    await db_session.refresh(owned)

    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    knight_before = {key: getattr(knight, key) for key in preserve_keys}
    other_cards_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != MOON_KNIGHT_CODE
    }
    player_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    history_count = len(await CharacterService.get_versions(db_session, knight.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(knight)
    await db_session.refresh(owned)

    expected_fields = {"lore_fa", "affiliations_fa"}
    if previous_faction:
        expected_fields.add("faction_id")
    assert writes == [(knight.id, expected_fields)]
    assert knight.affiliations_fa == MOON_KNIGHT_AFFILIATIONS
    assert knight.lore_fa == MOON_KNIGHT_STORY and knight.faction_id == moon.id
    assert {key: getattr(knight, key) for key in preserve_keys} == knight_before
    for code, before in other_cards_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in before} == before
    assert {key: getattr(owned, key) for key in player_before} == player_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, knight.id)) == history_count

    timestamp = knight.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(knight)
    assert writes == [(knight.id, expected_fields)] and knight.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, knight.id)) == history_count


async def test_moon_knight_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    knight = (await _seeded(db_session))[MOON_KNIGHT_CODE]
    user = await create_test_user(db_session, "بازی با شوالیه ماه رسمی")
    await _fund(db_session, user, apples=1000, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == knight.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialmoonkn01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialmoonkn01")
    await db_session.commit()
    assert first.price == 300 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700

    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{MOON_KNIGHT_NAME}*" in detail
    assert f"*وابستگی‌ها*:{MOON_KNIGHT_AFFILIATIONS}" in detail
    assert "💪 قدرت: `30`" in detail and "⚡ سرعت: `20`" in detail and "🧠 هوش: `10`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert MOON_KNIGHT_STORY not in detail and "*داستان*:" not in detail
    assert photo is not None
    assert f"card_main_set:{card.id}:0" in _buttons(keyboard)
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)

    await PlayerCardService.set_favorite(db_session, user.id, card.id, expected_main_card_id=0)
    await db_session.commit()
    result = await CardUpgradeService.upgrade(db_session, user.id, card.id, expected_level=1)
    await db_session.commit()
    assert (result.from_level, result.to_level) == (1, 2)
    assert await AppleWalletService.get_balance(db_session, user.id) == 600
    detail, main_id = await CollectionService.render_card_detail_with_main_card(db_session, user.id, card.id)
    assert main_id == card.id
    assert "💪 قدرت: `40`" in detail and "⚡ سرعت: `30`" in detail and "🧠 هوش: `20`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail and MOON_KNIGHT_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
