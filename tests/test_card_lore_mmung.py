"""Mmung's exact official text and shared surfaces, without guessed lore."""

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

MMUNG_CODE = "mmung"
MMUNG_NAME = "ممونگ"
MMUNG_AFFILIATIONS = "فرزندان ماه🌙 - پنجه‌های ماه🐾"
MMUNG_STORY = "ممونگ، گرگ مادهٔ فرزندان ماه؛ جنگجویی با زیبایی فریبنده و غریزه‌ای به وحشی‌گری درندگان. سرعتش به دشمن فرصت واکنش نمی‌دهد و نیزه‌اش پیش از آنکه زوزه‌اش در میدان نبرد بپیچد، جان میگیرد. کسانی که او را دست‌کم می‌گیرند، معمولاً فرصت پشیمانی پیدا نمی‌کنند."
MMUNG_WIKI = (
    f"*{MMUNG_NAME}*\n\n*وابستگی‌ها*:{MMUNG_AFFILIATIONS}\n\n"
    f"💪 قدرت: 14\n⚡ سرعت: 36\n🧠 هوش: 10\n\n*داستان*:{MMUNG_STORY}"
)


def test_mmung_seed_preserves_every_supplied_character_exactly():
    entry = next(entry for entry in _entries() if entry["code"] == MMUNG_CODE)
    assert entry["name_fa"] == MMUNG_NAME
    assert entry["affiliations_fa"] == MMUNG_AFFILIATIONS
    assert entry["lore_fa"] == MMUNG_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (14, 36, 10)
    assert entry["faction"] == "moon_descendants" and entry["sync_faction"] is True
    assert "گرگ مادهٔ فرزندان ماه" in entry["lore_fa"]
    assert "غریزه‌ای به وحشی‌گری" in entry["lore_fa"]
    assert "جان میگیرد." in entry["lore_fa"]  # Preserve the submitted spelling.
    assert "دست‌کم می‌گیرند" in entry["lore_fa"]
    # The Moon Paws phrase is stored as affiliation text, not inferred metadata.
    assert "پنجه‌های ماه🐾" in entry["affiliations_fa"]
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses"):
        assert field not in entry


async def test_mmung_wiki_and_shop_show_full_story_and_separate_price(db_session):
    characters = await _seeded(db_session, shop_code="mmung")
    mmung = characters[MMUNG_CODE]
    assert mmung.name_fa == MMUNG_NAME
    assert mmung.affiliations_fa == MMUNG_AFFILIATIONS
    assert mmung.lore_fa == MMUNG_STORY
    assert mmung.stats_dict() == {"power": 14, "speed": 36, "intelligence": 10}
    assert mmung.faction_role_fa is None and mmung.passive_key is None
    assert mmung.hidden_modifiers is None and not mmung.strengths_list and not mmung.weaknesses_list
    faction = await FactionService.get_for_character(db_session, mmung)
    assert faction is not None and faction.code == "moon_descendants"

    wiki = await WikiService.wiki_page(db_session, mmung.id)
    assert wiki == MMUNG_WIKI
    assert wiki.split("*داستان*:", 1)[1] == MMUNG_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(mmung, faction=faction) == wiki + "\n\n" + PRICE_LINE

    index = "\n".join(_wiki_index_lines([mmung], {faction.id: faction}))
    assert "فرزندان ماه" in index and "🌙" in index
    assert f"*{MMUNG_NAME}*" in index and "بدون دسته" not in index
    ordered = await WikiService.list_wiki(db_session)
    ordered_ids = [character.id for character in ordered]
    assert ordered_ids.index(characters["sib_mahi"].id) < ordered_ids.index(mmung.id)

    user = await create_test_user(db_session, "خریدار ممونگ رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == mmung.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    card_body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert card_body == wiki + "\n\n" + PRICE_LINE
    assert card_body.endswith(PRICE_LINE) and card_body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in mmung.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("old_faction", [None, "empire"])
async def test_mmung_sync_preserves_other_cards_and_player_state(db_session, monkeypatch, old_faction):
    characters = await _seeded(db_session)
    mmung = characters[MMUNG_CODE]
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    empire = await FactionService.get_by_code(db_session, "empire")
    user = await create_test_user(db_session, "حفظ وضعیت ممونگ")
    owned = await PlayerCardService.acquire(db_session, user.id, mmung.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    # Simulate an old row/operator override while retaining the official seed.
    mmung.affiliations_fa = None
    mmung.lore_fa = ""
    mmung.faction_id = empire.id if old_faction else None
    mmung.faction_role_fa = "نقش قبلی مدیریت‌شده"
    mmung.artwork = "custom-mmung-artwork"
    mmung.release_status = ReleaseStatus.RETIRED
    mmung.shop_enabled = False
    mmung.is_active = False
    mmung.price = 77
    await db_session.commit()
    await db_session.refresh(mmung)
    await db_session.refresh(owned)
    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    mmung_before = {key: getattr(mmung, key) for key in preserve_keys}
    others_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != MMUNG_CODE
    }
    player_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    histories_before = len(await CharacterService.get_versions(db_session, mmung.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(mmung)
    await db_session.refresh(owned)

    expected_fields = {"lore_fa", "affiliations_fa"}
    if old_faction:
        expected_fields.add("faction_id")
    assert writes == [(mmung.id, expected_fields)]
    assert mmung.affiliations_fa == MMUNG_AFFILIATIONS and mmung.lore_fa == MMUNG_STORY
    assert mmung.faction_id == moon.id
    assert {key: getattr(mmung, key) for key in preserve_keys} == mmung_before
    for code, before in others_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in before} == before
    assert {key: getattr(owned, key) for key in player_before} == player_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, mmung.id)) == histories_before

    # A repeated sync is a true no-op, including the timestamp.
    timestamp = mmung.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(mmung)
    assert writes == [(mmung.id, expected_fields)]
    assert mmung.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, mmung.id)) == histories_before


async def test_mmung_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    mmung = (await _seeded(db_session, shop_code="mmung"))[MMUNG_CODE]
    user = await create_test_user(db_session, "بازی با ممونگ رسمی")
    await _fund(db_session, user, apples=6700, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == mmung.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialmmung01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialmmung01")
    await db_session.commit()
    assert first.price == 6000 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700

    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{MMUNG_NAME}*" in detail
    assert f"*وابستگی‌ها*:{MMUNG_AFFILIATIONS}" in detail
    assert "💪 قدرت: `14`" in detail
    assert "⚡ سرعت: `36`" in detail
    assert "🧠 هوش: `10`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert MMUNG_STORY not in detail and "*داستان*:" not in detail
    assert photo is not None
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
    assert "💪 قدرت: `24`" in detail
    assert "⚡ سرعت: `46`" in detail
    assert "🧠 هوش: `20`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail
    assert MMUNG_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
