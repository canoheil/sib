"""Filo's supplied lore, opt-in faction grouping and intact game state."""

import pytest

from sib.database.connection import CharacterSeedError, seed_characters
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
from sib.tests.test_card_lore import PRICE_LINE, _buttons, _entries, _replace_seed, _seeded
from sib.tests.test_card_upgrade import _fund
from sib.tests.test_parties import create_test_user

FILO_CODE = "filo"
FILO_NAME = "فیلو"
FILO_AFFILIATIONS = "امپراتوری👑"
FILO_STORY = "در میدان‌های نبرد امپراتوری، فیلو بارها جنگجویانی را از آستانهٔ مرگ بازگردانده است. می‌گویند دستانش می‌توانند هر زخمی را درمان کنند؛ اما هیچ‌کس نمی‌داند برای بازگرداندن هر جان، چه بهایی می‌پردازد."
FILO_WIKI = (
    f"*{FILO_NAME}*\n\n*وابستگی‌ها*:{FILO_AFFILIATIONS}\n\n"
    f"💪 قدرت: 7\n⚡ سرعت: 13\n🧠 هوش: 40\n\n*داستان*:{FILO_STORY}"
)


def test_filo_seed_is_exact_and_invents_no_role_or_mechanics():
    entry = next(entry for entry in _entries() if entry["code"] == FILO_CODE)
    assert entry["name_fa"] == FILO_NAME
    assert entry["affiliations_fa"] == FILO_AFFILIATIONS
    assert entry["lore_fa"] == FILO_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (7, 13, 40)
    assert entry["faction"] == "empire" and entry["sync_faction"] is True
    assert "آستانه\u0654 مرگ" in entry["lore_fa"]
    assert "هیچ‌کس نمی‌داند" in entry["lore_fa"]
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses"):
        assert field not in entry


async def test_filo_wiki_grouping_shop_and_full_text_are_consistent(db_session):
    characters = await _seeded(db_session)
    filo = characters[FILO_CODE]
    assert filo.name_fa == FILO_NAME and filo.affiliations_fa == FILO_AFFILIATIONS
    assert filo.lore_fa == FILO_STORY
    assert filo.stats_dict() == {"power": 7, "speed": 13, "intelligence": 40}
    faction = await FactionService.get_for_character(db_session, filo)
    assert faction is not None and faction.code == "empire"
    assert filo.faction_role_fa is None and filo.passive_key is None
    assert filo.hidden_modifiers is None
    assert not filo.strengths_list and not filo.weaknesses_list
    wiki = await WikiService.wiki_page(db_session, filo.id)
    assert wiki == FILO_WIKI
    assert wiki.split("*داستان*:", 1)[1] == FILO_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(filo, faction=faction) == wiki + "\n\n" + PRICE_LINE
    index = "\n".join(_wiki_index_lines([filo], {faction.id: faction}))
    assert "امپراتوری" in index and "👑" in index and f"*{FILO_NAME}*" in index
    assert "بدون دسته" not in index
    ordered = await WikiService.list_wiki(db_session)
    ids = [character.id for character in ordered]
    assert ids.index(filo.id) < ids.index(characters["sib_sabz"].id)

    user = await create_test_user(db_session, "خریدار فیلو رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == filo.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert body == wiki + "\n\n" + PRICE_LINE
    assert body.endswith(PRICE_LINE) and body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in filo.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("old_faction", [None, "moon_descendants"])
async def test_filo_sync_only_updates_its_official_fields_and_group(db_session, monkeypatch, old_faction):
    characters = await _seeded(db_session)
    filo = characters[FILO_CODE]
    empire = await FactionService.get_by_code(db_session, "empire")
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    user = await create_test_user(db_session, "حفظ وضعیت فیلو")
    owned = await PlayerCardService.acquire(db_session, user.id, filo.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    # Existing-installation test state, including operator edits to another card.
    filo.affiliations_fa = None
    filo.lore_fa = ""
    filo.faction_id = moon.id if old_faction else None
    filo.faction_role_fa = "نقش قبلی مدیریت‌شده"
    filo.artwork = "custom-filo-artwork"
    filo.release_status = ReleaseStatus.RETIRED
    filo.shop_enabled = False
    filo.is_active = False
    filo.price = 77
    characters["sib_sorkh"].faction_id = moon.id  # No opt-in on that entry.
    await db_session.commit()
    await db_session.refresh(filo)
    await db_session.refresh(owned)
    await db_session.refresh(characters["sib_sorkh"])
    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    before = {key: getattr(filo, key) for key in preserve_keys}
    others_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != FILO_CODE
    }
    card_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    histories_before = len(await CharacterService.get_versions(db_session, filo.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(filo)
    await db_session.refresh(owned)
    expected_keys = {"lore_fa", "affiliations_fa"}
    if old_faction:
        expected_keys.add("faction_id")
    assert writes == [(filo.id, expected_keys)]
    assert filo.faction_id == empire.id
    assert filo.affiliations_fa == FILO_AFFILIATIONS and filo.lore_fa == FILO_STORY
    assert {key: getattr(filo, key) for key in preserve_keys} == before
    for code, previous in others_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in previous} == previous
    assert {key: getattr(owned, key) for key in card_before} == card_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, filo.id)) == histories_before

    timestamp = filo.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(filo)
    assert writes == [(filo.id, expected_keys)] and filo.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, filo.id)) == histories_before


async def test_filo_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    filo = (await _seeded(db_session))[FILO_CODE]
    user = await create_test_user(db_session, "بازی با فیلو رسمی")
    await _fund(db_session, user, apples=1000, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == filo.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialfilo01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialfilo01")
    await db_session.commit()
    assert first.price == 300 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700
    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{FILO_NAME}*" in detail and f"*وابستگی‌ها*:{FILO_AFFILIATIONS}" in detail
    assert "💪 قدرت: `7`" in detail and "⚡ سرعت: `13`" in detail and "🧠 هوش: `40`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert FILO_STORY not in detail and "*داستان*:" not in detail
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
    assert "💪 قدرت: `17`" in detail and "⚡ سرعت: `23`" in detail and "🧠 هوش: `50`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail and FILO_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)


@pytest.mark.parametrize("change,omit", [
    ({"faction": "unknown_test_faction"}, ()),
    ({"sync_faction": "true"}, ()),
    ({"faction": 42}, ()),
    ({}, ("faction",)),
    ({}, ("affiliations_fa",)),
])
async def test_invalid_opt_in_rolls_back_all_content_and_group_writes(db_session, monkeypatch, change, omit):
    filo = (await _seeded(db_session))[FILO_CODE]
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    filo.lore_fa = ""
    filo.affiliations_fa = None
    filo.faction_id = moon.id
    await db_session.commit()
    filo_id, old_faction_id = filo.id, moon.id
    payload = _entries()
    bad = dict(next(entry for entry in payload if entry["code"] == FILO_CODE))
    bad["code"] = "invalid_filo_faction"
    bad.update(change)
    for key in omit:
        del bad[key]
    payload.append(bad)
    _replace_seed(monkeypatch, payload)
    with pytest.raises(CharacterSeedError):
        await seed_characters(db_session)
    await db_session.rollback()
    restored = await db_session.get(Character, filo_id)
    assert restored.lore_fa == "" and restored.affiliations_fa is None
    assert restored.faction_id == old_faction_id
    assert await CharacterService.get_by_code(db_session, "invalid_filo_faction") is None
