"""Sage King's exact official text and consistent game surfaces."""

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

SAGE_KING_CODE = "sage_king"
SAGE_KING_NAME = "پادشاه فرزانه"
SAGE_KING_AFFILIATIONS = "فرزانگان🔮"
SAGE_KING_STORY = "پادشاه فرزانگان، فرمانروای سرزمین خرد و پدر سیب طلایی؛ پادشاهی که قدرتش نه‌تنها در دانسته‌های بی‌شمارش، بلکه در چیزهایی است که هرگز بر زبان نمی‌آورد. بسیاری او را خردمندترین موجود امپراتوری می‌دانند، اما هیچ‌کس نمی‌داند آیا او آینده را پیش‌بینی می‌کند، یا خودش آن را می‌سازد."
SAGE_KING_WIKI = (
    f"*{SAGE_KING_NAME}*\n\n*وابستگی‌ها*:{SAGE_KING_AFFILIATIONS}\n\n"
    f"💪 قدرت: 20\n⚡ سرعت: 12\n🧠 هوش: 28\n\n*داستان*:{SAGE_KING_STORY}"
)


def test_sage_king_seed_preserves_all_official_fields_verbatim():
    entry = next(entry for entry in _entries() if entry["code"] == SAGE_KING_CODE)
    assert entry["name_fa"] == SAGE_KING_NAME
    assert entry["affiliations_fa"] == SAGE_KING_AFFILIATIONS
    assert entry["lore_fa"] == SAGE_KING_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (20, 12, 28)
    assert entry["faction"] == "sages" and entry["sync_faction"] is True
    assert "نه‌تنها" in entry["lore_fa"]
    assert "دانسته‌های بی‌شمارش" in entry["lore_fa"]
    assert "هیچ‌کس نمی‌داند" in entry["lore_fa"]
    assert entry["lore_fa"].endswith("یا خودش آن را می‌سازد.")
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses"):
        assert field not in entry


async def test_sage_king_wiki_and_shop_render_full_official_text(db_session):
    characters = await _seeded(db_session, shop_code="sage_king")
    king = characters[SAGE_KING_CODE]
    assert king.name_fa == SAGE_KING_NAME
    assert king.affiliations_fa == SAGE_KING_AFFILIATIONS
    assert king.lore_fa == SAGE_KING_STORY
    assert king.stats_dict() == {"power": 20, "speed": 12, "intelligence": 28}
    assert king.faction_role_fa is None and king.passive_key is None
    assert king.hidden_modifiers is None and not king.strengths_list and not king.weaknesses_list
    faction = await FactionService.get_for_character(db_session, king)
    assert faction is not None and faction.code == "sages"

    wiki = await WikiService.wiki_page(db_session, king.id)
    assert wiki == SAGE_KING_WIKI
    assert wiki.split("*داستان*:", 1)[1] == SAGE_KING_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(king, faction=faction) == wiki + "\n\n" + PRICE_LINE

    index = "\n".join(_wiki_index_lines([king], {faction.id: faction}))
    assert "فرزانگان" in index and "🔮" in index
    assert f"*{SAGE_KING_NAME}*" in index and "بدون دسته" not in index
    ordered = await WikiService.list_wiki(db_session)
    ordered_ids = [character.id for character in ordered]
    assert ordered_ids.index(characters["green_knight"].id) < ordered_ids.index(king.id)
    assert ordered_ids.index(king.id) < ordered_ids.index(characters["sib_mahi"].id)

    user = await create_test_user(db_session, "خریدار پادشاه فرزانه رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == king.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    card_body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert card_body == wiki + "\n\n" + PRICE_LINE
    assert card_body.endswith(PRICE_LINE) and card_body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in king.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("previous_faction", [None, "moon_descendants"])
async def test_sage_king_sync_preserves_other_cards_and_player_state(db_session, monkeypatch, previous_faction):
    characters = await _seeded(db_session)
    king = characters[SAGE_KING_CODE]
    sages = await FactionService.get_by_code(db_session, "sages")
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    user = await create_test_user(db_session, "حفظ وضعیت پادشاه فرزانه")
    owned = await PlayerCardService.acquire(db_session, user.id, king.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    king.affiliations_fa = None
    king.lore_fa = ""
    king.faction_id = moon.id if previous_faction else None
    king.faction_role_fa = "نقش قبلی مدیریت‌شده"
    king.artwork = "custom-sage-king-artwork"
    king.release_status = ReleaseStatus.RETIRED
    king.shop_enabled = False
    king.is_active = False
    king.price = 77
    await db_session.commit()
    await db_session.refresh(king)
    await db_session.refresh(owned)

    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    king_before = {key: getattr(king, key) for key in preserve_keys}
    other_cards_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != SAGE_KING_CODE
    }
    player_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    history_count = len(await CharacterService.get_versions(db_session, king.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(king)
    await db_session.refresh(owned)

    expected_fields = {"lore_fa", "affiliations_fa"}
    if previous_faction:
        expected_fields.add("faction_id")
    assert writes == [(king.id, expected_fields)]
    assert king.affiliations_fa == SAGE_KING_AFFILIATIONS
    assert king.lore_fa == SAGE_KING_STORY and king.faction_id == sages.id
    assert {key: getattr(king, key) for key in preserve_keys} == king_before
    for code, before in other_cards_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in before} == before
    assert {key: getattr(owned, key) for key in player_before} == player_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, king.id)) == history_count

    timestamp = king.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(king)
    assert writes == [(king.id, expected_fields)] and king.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, king.id)) == history_count


async def test_sage_king_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    king = (await _seeded(db_session, shop_code="sage_king"))[SAGE_KING_CODE]
    user = await create_test_user(db_session, "بازی با پادشاه فرزانه رسمی")
    await _fund(db_session, user, apples=1000, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == king.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialsageking1")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialsageking1")
    await db_session.commit()
    assert first.price == 300 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700

    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{SAGE_KING_NAME}*" in detail
    assert f"*وابستگی‌ها*:{SAGE_KING_AFFILIATIONS}" in detail
    assert "💪 قدرت: `20`" in detail and "⚡ سرعت: `12`" in detail and "🧠 هوش: `28`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert SAGE_KING_STORY not in detail and "*داستان*:" not in detail
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
    assert "💪 قدرت: `30`" in detail and "⚡ سرعت: `22`" in detail and "🧠 هوش: `38`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail and SAGE_KING_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
