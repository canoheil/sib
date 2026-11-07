"""Treyni's exact official text and shared card presentations."""

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

TREYNI_CODE = "treyni"
TREYNI_NAME = "ترینی"
TREYNI_AFFILIATIONS = "فرزانگان🔮"
TREYNI_STORY = "ترینی، دروید نگهبان جنگل‌های کهن؛ موجودی با زیبایی فریبنده که اراده‌اش با ریشه‌های جنگل درهم تنیده است. او از حیات محافظت می‌کند، اما برایش تفاوتی میان دشمن و قربانی وجود ندارد؛ هرکس تعادل طبیعت را برهم بزند، بهای آن را خواهد پرداخت. چراکه طبیعت برای زیبا بودن، نیازی به مهربان بودن ندارد."
TREYNI_WIKI = (
    f"*{TREYNI_NAME}*\n\n*وابستگی‌ها*:{TREYNI_AFFILIATIONS}\n\n"
    f"💪 قدرت: 12\n⚡ سرعت: 24\n🧠 هوش: 24\n\n*داستان*:{TREYNI_STORY}"
)


def test_treyni_seed_preserves_the_exact_official_message():
    entry = next(entry for entry in _entries() if entry["code"] == TREYNI_CODE)
    assert entry["name_fa"] == TREYNI_NAME
    assert entry["affiliations_fa"] == TREYNI_AFFILIATIONS
    assert entry["lore_fa"] == TREYNI_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (12, 24, 24)
    assert entry["faction"] == "sages" and entry["sync_faction"] is True
    assert "دروید نگهبان جنگل‌های کهن" in entry["lore_fa"]
    assert "تفاوتی میان دشمن و قربانی وجود ندارد" in entry["lore_fa"]
    assert entry["lore_fa"].endswith("نیازی به مهربان بودن ندارد.")
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses"):
        assert field not in entry


async def test_treyni_wiki_and_shop_show_full_story_with_separate_price(db_session):
    characters = await _seeded(db_session, shop_code="treyni")
    treyni = characters[TREYNI_CODE]
    assert treyni.name_fa == TREYNI_NAME
    assert treyni.affiliations_fa == TREYNI_AFFILIATIONS
    assert treyni.lore_fa == TREYNI_STORY
    assert treyni.stats_dict() == {"power": 12, "speed": 24, "intelligence": 24}
    assert treyni.faction_role_fa is None and treyni.passive_key is None
    assert treyni.hidden_modifiers is None and not treyni.strengths_list and not treyni.weaknesses_list
    faction = await FactionService.get_for_character(db_session, treyni)
    assert faction is not None and faction.code == "sages"

    wiki = await WikiService.wiki_page(db_session, treyni.id)
    assert wiki == TREYNI_WIKI
    assert wiki.split("*داستان*:", 1)[1] == TREYNI_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(treyni, faction=faction) == wiki + "\n\n" + PRICE_LINE

    index = "\n".join(_wiki_index_lines([treyni], {faction.id: faction}))
    assert "فرزانگان" in index and "🔮" in index
    assert f"*{TREYNI_NAME}*" in index and "بدون دسته" not in index
    ordered = await WikiService.list_wiki(db_session)
    ordered_ids = [character.id for character in ordered]
    assert ordered_ids.index(characters["sage_king"].id) < ordered_ids.index(treyni.id)
    assert ordered_ids.index(treyni.id) < ordered_ids.index(characters["sib_mahi"].id)

    user = await create_test_user(db_session, "خریدار ترینی رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == treyni.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    card_body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert card_body == wiki + "\n\n" + PRICE_LINE
    assert card_body.endswith(PRICE_LINE) and card_body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in treyni.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("previous_faction", [None, "moon_descendants"])
async def test_treyni_sync_preserves_other_cards_and_player_state(db_session, monkeypatch, previous_faction):
    characters = await _seeded(db_session)
    treyni = characters[TREYNI_CODE]
    sages = await FactionService.get_by_code(db_session, "sages")
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    user = await create_test_user(db_session, "حفظ وضعیت ترینی")
    owned = await PlayerCardService.acquire(db_session, user.id, treyni.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    treyni.affiliations_fa = None
    treyni.lore_fa = ""
    treyni.faction_id = moon.id if previous_faction else None
    treyni.faction_role_fa = "نقش قبلی مدیریت‌شده"
    treyni.artwork = "custom-treyni-artwork"
    treyni.release_status = ReleaseStatus.RETIRED
    treyni.shop_enabled = False
    treyni.is_active = False
    treyni.price = 77
    await db_session.commit()
    await db_session.refresh(treyni)
    await db_session.refresh(owned)

    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    treyni_before = {key: getattr(treyni, key) for key in preserve_keys}
    other_cards_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != TREYNI_CODE
    }
    player_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    history_count = len(await CharacterService.get_versions(db_session, treyni.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(treyni)
    await db_session.refresh(owned)

    expected_fields = {"lore_fa", "affiliations_fa"}
    if previous_faction:
        expected_fields.add("faction_id")
    assert writes == [(treyni.id, expected_fields)]
    assert treyni.affiliations_fa == TREYNI_AFFILIATIONS
    assert treyni.lore_fa == TREYNI_STORY and treyni.faction_id == sages.id
    assert {key: getattr(treyni, key) for key in preserve_keys} == treyni_before
    for code, before in other_cards_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in before} == before
    assert {key: getattr(owned, key) for key in player_before} == player_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, treyni.id)) == history_count

    timestamp = treyni.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(treyni)
    assert writes == [(treyni.id, expected_fields)] and treyni.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, treyni.id)) == history_count


async def test_treyni_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    treyni = (await _seeded(db_session, shop_code="treyni"))[TREYNI_CODE]
    user = await create_test_user(db_session, "بازی با ترینی رسمی")
    await _fund(db_session, user, apples=2700, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == treyni.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialtreyni01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialtreyni01")
    await db_session.commit()
    assert first.price == 2000 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700

    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{TREYNI_NAME}*" in detail
    assert f"*وابستگی‌ها*:{TREYNI_AFFILIATIONS}" in detail
    assert "💪 قدرت: `12`" in detail and "⚡ سرعت: `24`" in detail and "🧠 هوش: `24`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert TREYNI_STORY not in detail and "*داستان*:" not in detail
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
    assert "💪 قدرت: `22`" in detail and "⚡ سرعت: `34`" in detail and "🧠 هوش: `34`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail and TREYNI_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
