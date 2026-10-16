"""Tifun's exact official text and shared surfaces, without guessed lore.

The seventeenth card («طیفان»): supplied affiliations, stats and story only.
Like the other supplied cards it ships as pure data - no role, no passive and
no strengths/weaknesses may be inferred from the story, and its art is the
bundled original ``assets/characters/tifun.jpg`` (resolved by code).
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
from sib.tests.test_card_lore import PRICE_LINE, _buttons, _entries, _seeded
from sib.tests.test_card_upgrade import _fund
from sib.tests.test_parties import create_test_user

TIFUN_CODE = "tifun"
TIFUN_NAME = "طیفان"
# Supplied affiliations, verbatim: the sages' group name and its symbol,
# written exactly like the rest of the sages roster (no space between them).
TIFUN_AFFILIATIONS = "فرزانگان🔮"
TIFUN_STORY = "نگهبان فانوس دریایی و فرمانروای طوفان‌ها؛ پیرمردی که باد و رعد را به فرمان خود درمی‌آورد. می‌گویند نور فانوسش رازی باستانی را پنهان کرده که با خاموشی آن، خشم دریا آزاد خواهد شد."
TIFUN_WIKI = (
    f"*{TIFUN_NAME}*\n\n*وابستگی‌ها*:{TIFUN_AFFILIATIONS}\n\n"
    f"💪 قدرت: 15\n⚡ سرعت: 15\n🧠 هوش: 30\n\n*داستان*:{TIFUN_STORY}"
)


def test_tifun_seed_preserves_every_supplied_character_exactly():
    entry = next(entry for entry in _entries() if entry["code"] == TIFUN_CODE)
    assert entry["name_fa"] == TIFUN_NAME
    assert entry["name_en"] == "Tifun"
    assert entry["affiliations_fa"] == TIFUN_AFFILIATIONS
    assert entry["lore_fa"] == TIFUN_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (15, 15, 30)
    assert entry["faction"] == "sages" and entry["sync_faction"] is True
    assert entry["release_status"] == "RELEASED" and entry["shop_enabled"] is True
    assert entry["artwork"] is None  # bundled art resolves by code

    # Submitted spellings and text carriers survive storage untouched.
    assert "طوفان‌ها" in entry["lore_fa"] and "درمی‌آورد" in entry["lore_fa"]
    assert "می‌گویند" in entry["lore_fa"] and "فانوس دریایی" in entry["lore_fa"]
    assert "فرمانروای طوفان‌ها" in entry["lore_fa"]
    assert "خشم دریا آزاد خواهد شد" in entry["lore_fa"]
    assert "\u200c" in entry["lore_fa"]  # the supplied half-spaces are kept
    # The sages' affiliation is the same string as the rest of the faction,
    # emoji included; it is free text, never a role or a new faction.
    assert entry["affiliations_fa"] == "فرزانگان 🔮".replace(" ", "")
    assert entry["affiliations_fa"].endswith("🔮")
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers",
                  "strengths", "weaknesses"):
        assert field not in entry, f"no gameplay or role must be invented for {field}"


async def test_tifun_wiki_and_shop_show_full_story_and_separate_price(db_session):
    characters = await _seeded(db_session)
    tifun = characters[TIFUN_CODE]
    assert tifun.name_fa == TIFUN_NAME and tifun.name_en == "Tifun"
    assert tifun.affiliations_fa == TIFUN_AFFILIATIONS
    assert tifun.lore_fa == TIFUN_STORY
    assert tifun.stats_dict() == {"power": 15, "speed": 15, "intelligence": 30}
    assert tifun.total_base_stats == 60
    assert tifun.faction_role_fa is None and tifun.passive_key is None
    assert tifun.hidden_modifiers is None
    assert not tifun.strengths_list and not tifun.weaknesses_list
    faction = await FactionService.get_for_character(db_session, tifun)
    assert faction is not None and faction.code == "sages"

    wiki = await WikiService.wiki_page(db_session, tifun.id)
    assert wiki == TIFUN_WIKI
    assert wiki.split("*داستان*:", 1)[1] == TIFUN_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(tifun, faction=faction) == wiki + "\n\n" + PRICE_LINE

    index = "\n".join(_wiki_index_lines([tifun], {faction.id: faction}))
    assert "فرزانگان" in index and "🔮" in index
    assert f"*{TIFUN_NAME}*" in index and "بدون دسته" not in index
    # The new card joins the sages group in stable insertion order.
    ordered_ids = [character.id for character in await WikiService.list_wiki(db_session)]
    assert ordered_ids.index(characters["sib_sabz"].id) < ordered_ids.index(tifun.id)
    assert ordered_ids.index(characters["treyni"].id) < ordered_ids.index(tifun.id)

    user = await create_test_user(db_session, "خریدار طیفان رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == tifun.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    card_body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert card_body == wiki + "\n\n" + PRICE_LINE
    assert card_body.endswith(PRICE_LINE) and card_body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in tifun.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("old_faction", [None, "moon_descendants"])
async def test_tifun_sync_preserves_other_cards_and_player_state(
    db_session, monkeypatch, old_faction
):
    characters = await _seeded(db_session)
    tifun = characters[TIFUN_CODE]
    sages = await FactionService.get_by_code(db_session, "sages")
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    user = await create_test_user(db_session, "حفظ وضعیت طیفان")
    owned = await PlayerCardService.acquire(db_session, user.id, tifun.id)
    owned.level = 3
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    # Simulate an old row/operator override while the official seed ships.
    tifun.affiliations_fa = None
    tifun.lore_fa = ""
    tifun.faction_id = moon.id if old_faction else None
    tifun.faction_role_fa = "نقش قبلی مدیریت‌شده"
    tifun.artwork = "custom-tifun-artwork"
    tifun.release_status = ReleaseStatus.RETIRED
    tifun.shop_enabled = False
    tifun.is_active = False
    tifun.price = 77
    characters["sib_sorkh"].faction_id = moon.id  # No opt-in on that entry.
    await db_session.commit()
    await db_session.refresh(tifun)
    await db_session.refresh(owned)
    await db_session.refresh(characters["sib_sorkh"])
    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    tifun_before = {key: getattr(tifun, key) for key in preserve_keys}
    others_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != TIFUN_CODE
    }
    player_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    histories_before = len(await CharacterService.get_versions(db_session, tifun.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(tifun)
    await db_session.refresh(owned)

    expected_fields = {"lore_fa", "affiliations_fa"}
    if old_faction:
        expected_fields.add("faction_id")
    assert writes == [(tifun.id, expected_fields)]
    assert tifun.affiliations_fa == TIFUN_AFFILIATIONS and tifun.lore_fa == TIFUN_STORY
    assert tifun.faction_id == sages.id
    assert {key: getattr(tifun, key) for key in preserve_keys} == tifun_before
    for code, before in others_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in before} == before
    assert {key: getattr(owned, key) for key in player_before} == player_before
    assert owned.level == 3 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, tifun.id)) == histories_before

    # A repeated sync is a true no-op, including the timestamp.
    timestamp = tifun.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(tifun)
    assert writes == [(tifun.id, expected_fields)]
    assert tifun.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, tifun.id)) == histories_before


async def test_tifun_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    from pathlib import Path

    from aiogram.types import FSInputFile

    tifun = (await _seeded(db_session))[TIFUN_CODE]
    user = await create_test_user(db_session, "بازی با طیفان رسمی")
    await _fund(db_session, user, apples=1000, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == tifun.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialtifun001")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialtifun001")
    await db_session.commit()
    assert first.price == 300 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700

    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{TIFUN_NAME}*" in detail
    assert f"*وابستگی‌ها*:{TIFUN_AFFILIATIONS}" in detail
    assert "💪 قدرت: `15`" in detail
    assert "⚡ سرعت: `15`" in detail
    assert "🧠 هوش: `30`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert TIFUN_STORY not in detail and "*داستان*:" not in detail
    # The owned-card screen sends the card's own bundled original artwork.
    assert isinstance(photo, FSInputFile)
    assert Path(photo.path) == Path(__file__).resolve().parents[1] / "assets" / "characters" / "tifun.jpg"
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
    assert "💪 قدرت: `25`" in detail
    assert "⚡ سرعت: `25`" in detail
    assert "🧠 هوش: `40`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail
    assert TIFUN_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
