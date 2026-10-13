"""Igris's exact official text, shared surfaces and scoped replacement."""

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

IGRIS_CODE = "igris"
IGRIS_NAME = "ایگریس"
IGRIS_AFFILIATIONS = "امپراتوری👑 - تیغ های تاج🗡"
IGRIS_STORY = "در میان تیغ‌های تاج، هیچ‌کس به اندازهٔ ایگریس هراس‌انگیز نیست؛ جنگجویی که حتی ظاهرش برای شکستن ارادهٔ دشمنان کافی است. او برای امپراتوری می‌جنگد، اما کسانی که از نزدیک با او روبه‌رو شده‌اند، می‌دانند چیزی در وجودش هست که حتی وحشتناک‌ترین دشمنان زاریوش را به سکوت وامی‌دارد."
IGRIS_WIKI = (
    f"*{IGRIS_NAME}*\n\n*وابستگی‌ها*:{IGRIS_AFFILIATIONS}\n\n"
    f"💪 قدرت: 32\n⚡ سرعت: 17\n🧠 هوش: 11\n\n*داستان*:{IGRIS_STORY}"
)


def test_igris_seed_is_verbatim_without_invented_role_or_mechanics():
    entry = next(entry for entry in _entries() if entry["code"] == IGRIS_CODE)
    assert entry["name_fa"] == IGRIS_NAME
    assert entry["affiliations_fa"] == IGRIS_AFFILIATIONS
    assert entry["lore_fa"] == IGRIS_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (32, 17, 11)
    assert entry["faction"] == "empire" and entry["sync_faction"] is True
    assert "اندازه\u0654 ایگریس" in entry["lore_fa"]
    assert "اراده\u0654 دشمنان" in entry["lore_fa"]
    assert "روبه‌رو شده‌اند" in entry["lore_fa"]
    assert "زاریوش" in entry["lore_fa"] and entry["lore_fa"].endswith("وامی‌دارد.")
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses"):
        assert field not in entry


async def test_igris_wiki_group_shop_and_full_caption_are_consistent(db_session):
    characters = await _seeded(db_session)
    igris = characters[IGRIS_CODE]
    assert igris.name_fa == IGRIS_NAME and igris.affiliations_fa == IGRIS_AFFILIATIONS
    assert igris.lore_fa == IGRIS_STORY
    assert igris.stats_dict() == {"power": 32, "speed": 17, "intelligence": 11}
    faction = await FactionService.get_for_character(db_session, igris)
    assert faction is not None and faction.code == "empire"
    assert igris.faction_role_fa is None and igris.passive_key is None
    assert igris.hidden_modifiers is None and not igris.strengths_list and not igris.weaknesses_list
    wiki = await WikiService.wiki_page(db_session, igris.id)
    assert wiki == IGRIS_WIKI
    assert wiki.split("*داستان*:", 1)[1] == IGRIS_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(igris, faction=faction) == wiki + "\n\n" + PRICE_LINE
    index = "\n".join(_wiki_index_lines([igris], {faction.id: faction}))
    assert "امپراتوری" in index and "👑" in index and f"*{IGRIS_NAME}*" in index
    assert "بدون دسته" not in index
    ids = [character.id for character in await WikiService.list_wiki(db_session)]
    assert ids.index(igris.id) < ids.index(characters["sib_sabz"].id)

    user = await create_test_user(db_session, "خریدار ایگریس رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == igris.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert body == wiki + "\n\n" + PRICE_LINE
    assert body.endswith(PRICE_LINE) and body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in igris.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("old_faction", [None, "moon_descendants"])
async def test_igris_sync_preserves_other_cards_and_player_state(db_session, monkeypatch, old_faction):
    characters = await _seeded(db_session)
    igris = characters[IGRIS_CODE]
    empire = await FactionService.get_by_code(db_session, "empire")
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    user = await create_test_user(db_session, "حفظ وضعیت ایگریس")
    owned = await PlayerCardService.acquire(db_session, user.id, igris.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")
    igris.affiliations_fa = None
    igris.lore_fa = ""
    igris.faction_id = moon.id if old_faction else None
    igris.faction_role_fa = "نقش قبلی مدیریت‌شده"
    igris.artwork = "custom-igris-artwork"
    igris.release_status = ReleaseStatus.RETIRED
    igris.shop_enabled = False
    igris.is_active = False
    igris.price = 77
    await db_session.commit()
    await db_session.refresh(igris)
    await db_session.refresh(owned)
    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    before = {key: getattr(igris, key) for key in preserve_keys}
    others_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != IGRIS_CODE
    }
    card_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    histories_before = len(await CharacterService.get_versions(db_session, igris.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(igris)
    await db_session.refresh(owned)
    expected_keys = {"lore_fa", "affiliations_fa"}
    if old_faction:
        expected_keys.add("faction_id")
    assert writes == [(igris.id, expected_keys)]
    assert igris.faction_id == empire.id
    assert igris.affiliations_fa == IGRIS_AFFILIATIONS and igris.lore_fa == IGRIS_STORY
    assert {key: getattr(igris, key) for key in preserve_keys} == before
    for code, previous in others_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in previous} == previous
    assert {key: getattr(owned, key) for key in card_before} == card_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, igris.id)) == histories_before
    timestamp = igris.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(igris)
    assert writes == [(igris.id, expected_keys)] and igris.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, igris.id)) == histories_before


async def test_igris_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    igris = (await _seeded(db_session))[IGRIS_CODE]
    user = await create_test_user(db_session, "بازی با ایگریس رسمی")
    await _fund(db_session, user, apples=1000, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == igris.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialigris01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialigris01")
    await db_session.commit()
    assert first.price == 300 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700
    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{IGRIS_NAME}*" in detail and f"*وابستگی‌ها*:{IGRIS_AFFILIATIONS}" in detail
    for label, value in (("💪 قدرت", 32), ("⚡ سرعت", 17), ("🧠 هوش", 11)):
        assert f"{label}: `{value}`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert IGRIS_STORY not in detail and "*داستان*:" not in detail
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
    for label, value in (("💪 قدرت", 42), ("⚡ سرعت", 27), ("🧠 هوش", 21)):
        assert f"{label}: `{value}`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail and IGRIS_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
