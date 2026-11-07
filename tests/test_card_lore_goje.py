"""Goje's exact official text, shared surfaces and scoped replacement."""

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
from sib.tests.test_card_lore import EPIC_PRICE_LINE as PRICE_LINE, _buttons, _entries, _seeded
from sib.tests.test_card_upgrade import _fund
from sib.tests.test_parties import create_test_user

GOJE_CODE = "goje"
GOJE_NAME = "گوجه"
GOJE_AFFILIATIONS = "امپراتوری👑"
GOJE_STORY = "در دربار امپراتوری، نام گوجه با قدرتی گره خورده که در هیچ فرمانی ثبت نشده است. او نه تاجی به سر دارد و نه ادعایی برای تخت؛ بااین‌حال، گاهی یک کلمه از زبان او می‌تواند سرنوشت کسانی را تغییر دهد که خود را قدرتمندترین افراد امپراتوری می‌دانند."
GOJE_WIKI = (
    f"*{GOJE_NAME}*\n\n*وابستگی‌ها*:{GOJE_AFFILIATIONS}\n\n"
    f"💪 قدرت: 20\n⚡ سرعت: 20\n🧠 هوش: 20\n\n*داستان*:{GOJE_STORY}"
)


def test_goje_seed_is_verbatim_without_invented_role_or_mechanics():
    entry = next(entry for entry in _entries() if entry["code"] == GOJE_CODE)
    assert entry["name_fa"] == GOJE_NAME
    assert entry["affiliations_fa"] == GOJE_AFFILIATIONS
    assert entry["lore_fa"] == GOJE_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (20, 20, 20)
    assert entry["faction"] == "empire" and entry["sync_faction"] is True
    assert "بااین‌حال،" in entry["lore_fa"]
    assert "می‌تواند" in entry["lore_fa"] and "می‌دانند." in entry["lore_fa"]
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses"):
        assert field not in entry


async def test_goje_wiki_group_shop_and_full_caption_are_consistent(db_session):
    characters = await _seeded(db_session, shop_code="goje")
    goje = characters[GOJE_CODE]
    assert goje.name_fa == GOJE_NAME and goje.affiliations_fa == GOJE_AFFILIATIONS
    assert goje.lore_fa == GOJE_STORY
    assert goje.stats_dict() == {"power": 20, "speed": 20, "intelligence": 20}
    faction = await FactionService.get_for_character(db_session, goje)
    assert faction is not None and faction.code == "empire"
    assert goje.faction_role_fa is None and goje.passive_key is None
    assert goje.hidden_modifiers is None and not goje.strengths_list and not goje.weaknesses_list
    wiki = await WikiService.wiki_page(db_session, goje.id)
    assert wiki == GOJE_WIKI
    assert wiki.split("*داستان*:", 1)[1] == GOJE_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(goje, faction=faction) == wiki + "\n\n" + PRICE_LINE
    index = "\n".join(_wiki_index_lines([goje], {faction.id: faction}))
    assert "امپراتوری" in index and "👑" in index and f"*{GOJE_NAME}*" in index
    assert "بدون دسته" not in index
    ids = [character.id for character in await WikiService.list_wiki(db_session)]
    assert ids.index(goje.id) < ids.index(characters["sib_sabz"].id)

    user = await create_test_user(db_session, "خریدار گوجه رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == goje.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert body == wiki + "\n\n" + PRICE_LINE
    assert body.endswith(PRICE_LINE) and body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in goje.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("old_faction", [None, "moon_descendants"])
async def test_goje_sync_preserves_other_cards_and_player_state(db_session, monkeypatch, old_faction):
    characters = await _seeded(db_session)
    goje = characters[GOJE_CODE]
    empire = await FactionService.get_by_code(db_session, "empire")
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    user = await create_test_user(db_session, "حفظ وضعیت گوجه")
    owned = await PlayerCardService.acquire(db_session, user.id, goje.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")
    goje.affiliations_fa = None
    goje.lore_fa = ""
    goje.faction_id = moon.id if old_faction else None
    goje.faction_role_fa = "نقش قبلی مدیریت‌شده"
    goje.artwork = "custom-goje-artwork"
    goje.release_status = ReleaseStatus.RETIRED
    goje.shop_enabled = False
    goje.is_active = False
    goje.price = 77
    await db_session.commit()
    await db_session.refresh(goje)
    await db_session.refresh(owned)
    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    before = {key: getattr(goje, key) for key in preserve_keys}
    others_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != GOJE_CODE
    }
    card_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    histories_before = len(await CharacterService.get_versions(db_session, goje.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(goje)
    await db_session.refresh(owned)
    expected_keys = {"lore_fa", "affiliations_fa"}
    if old_faction:
        expected_keys.add("faction_id")
    assert writes == [(goje.id, expected_keys)]
    assert goje.faction_id == empire.id
    assert goje.affiliations_fa == GOJE_AFFILIATIONS and goje.lore_fa == GOJE_STORY
    assert {key: getattr(goje, key) for key in preserve_keys} == before
    for code, previous in others_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in previous} == previous
    assert {key: getattr(owned, key) for key in card_before} == card_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, goje.id)) == histories_before
    timestamp = goje.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(goje)
    assert writes == [(goje.id, expected_keys)] and goje.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, goje.id)) == histories_before


async def test_goje_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    goje = (await _seeded(db_session, shop_code="goje"))[GOJE_CODE]
    user = await create_test_user(db_session, "بازی با گوجه رسمی")
    await _fund(db_session, user, apples=6700, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == goje.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialgoje01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialgoje01")
    await db_session.commit()
    assert first.price == 6000 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700
    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{GOJE_NAME}*" in detail and f"*وابستگی‌ها*:{GOJE_AFFILIATIONS}" in detail
    for label in ("💪 قدرت", "⚡ سرعت", "🧠 هوش"):
        assert f"{label}: `20`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert GOJE_STORY not in detail and "*داستان*:" not in detail
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
    for label in ("💪 قدرت", "⚡ سرعت", "🧠 هوش"):
        assert f"{label}: `30`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail and GOJE_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
