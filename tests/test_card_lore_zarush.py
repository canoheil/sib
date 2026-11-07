"""Zarush's exact official submission and shared game presentations."""

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

ZARUSH_CODE = "zarush"
ZARUSH_NAME = "زاریوش"
ZARUSH_AFFILIATIONS = "امپراتوری👑"
ZARUSH_STORY = "زاریوش، پادشاه جنگجوی امپراتوری و مقتدرترین شمشیرزن سرزمین‌هایش؛ فرمانروایی که قدرت و کاریزمایش حتی سرسخت‌ترین جنگجویان را به اطاعت وامی‌دارد. تیغ‌های تاج مستقیماً تحت فرمان اویند و کسانی چون ایگریس و سیب قرمز، بی‌چون‌وچرا اراده‌اش را اجرا می‌کنند. زاریوش برای فرمانروایی به تاجش تکیه ندارد؛ این تاج است که به نام او معنا پیدا می‌کند."
ZARUSH_WIKI = (
    f"*{ZARUSH_NAME}*\n\n*وابستگی‌ها*:{ZARUSH_AFFILIATIONS}\n\n"
    f"💪 قدرت: 22\n⚡ سرعت: 21\n🧠 هوش: 17\n\n*داستان*:{ZARUSH_STORY}"
)


def test_zarush_seed_preserves_the_supplied_spelling_and_full_text():
    entry = next(entry for entry in _entries() if entry["code"] == ZARUSH_CODE)
    assert entry["name_fa"] == ZARUSH_NAME
    assert entry["affiliations_fa"] == ZARUSH_AFFILIATIONS
    assert entry["lore_fa"] == ZARUSH_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (22, 21, 17)
    assert entry["faction"] == "empire" and entry["sync_faction"] is True
    assert "مقتدرترین شمشیرزن سرزمین‌هایش" in entry["lore_fa"]
    assert "بی‌چون‌وچرا اراده‌اش را اجرا می‌کنند" in entry["lore_fa"]
    assert entry["lore_fa"].endswith("این تاج است که به نام او معنا پیدا می‌کند.")
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses"):
        assert field not in entry


async def test_zarush_wiki_and_shop_show_the_complete_official_text(db_session):
    characters = await _seeded(db_session, shop_code="zarush")
    zarush = characters[ZARUSH_CODE]
    assert zarush.name_fa == ZARUSH_NAME
    assert zarush.affiliations_fa == ZARUSH_AFFILIATIONS
    assert zarush.lore_fa == ZARUSH_STORY
    assert zarush.stats_dict() == {"power": 22, "speed": 21, "intelligence": 17}
    assert zarush.faction_role_fa is None and zarush.passive_key is None
    assert zarush.hidden_modifiers is None and not zarush.strengths_list and not zarush.weaknesses_list
    faction = await FactionService.get_for_character(db_session, zarush)
    assert faction is not None and faction.code == "empire"

    wiki = await WikiService.wiki_page(db_session, zarush.id)
    assert wiki == ZARUSH_WIKI
    assert wiki.split("*داستان*:", 1)[1] == ZARUSH_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(zarush, faction=faction) == wiki + "\n\n" + PRICE_LINE

    index = "\n".join(_wiki_index_lines([zarush], {faction.id: faction}))
    assert "امپراتوری" in index and "👑" in index
    assert f"*{ZARUSH_NAME}*" in index and "بدون دسته" not in index
    ordered = await WikiService.list_wiki(db_session)
    ordered_ids = [character.id for character in ordered]
    assert ordered_ids.index(characters["igris"].id) < ordered_ids.index(zarush.id)
    assert ordered_ids.index(zarush.id) < ordered_ids.index(characters["sib_sabz"].id)

    user = await create_test_user(db_session, "خریدار زاریوش رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == zarush.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    card_body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert card_body == wiki + "\n\n" + PRICE_LINE
    assert card_body.endswith(PRICE_LINE) and card_body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in zarush.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("previous_faction", [None, "sages"])
async def test_zarush_sync_preserves_other_cards_and_player_state(db_session, monkeypatch, previous_faction):
    characters = await _seeded(db_session)
    zarush = characters[ZARUSH_CODE]
    empire = await FactionService.get_by_code(db_session, "empire")
    sages = await FactionService.get_by_code(db_session, "sages")
    user = await create_test_user(db_session, "حفظ وضعیت زاریوش")
    owned = await PlayerCardService.acquire(db_session, user.id, zarush.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    zarush.name_fa = "زاروش"
    zarush.affiliations_fa = None
    zarush.lore_fa = ""
    zarush.faction_id = sages.id if previous_faction else None
    zarush.faction_role_fa = "نقش قبلی مدیریت‌شده"
    zarush.artwork = "custom-zarush-artwork"
    zarush.release_status = ReleaseStatus.RETIRED
    zarush.shop_enabled = False
    zarush.is_active = False
    zarush.price = 77
    await db_session.commit()
    await db_session.refresh(zarush)
    await db_session.refresh(owned)

    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"name_fa", "lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    zarush_before = {key: getattr(zarush, key) for key in preserve_keys}
    other_cards_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != ZARUSH_CODE
    }
    player_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    history_count = len(await CharacterService.get_versions(db_session, zarush.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(zarush)
    await db_session.refresh(owned)

    expected_fields = {"name_fa", "lore_fa", "affiliations_fa"}
    if previous_faction:
        expected_fields.add("faction_id")
    assert writes == [(zarush.id, expected_fields)]
    assert zarush.name_fa == ZARUSH_NAME
    assert zarush.affiliations_fa == ZARUSH_AFFILIATIONS
    assert zarush.lore_fa == ZARUSH_STORY and zarush.faction_id == empire.id
    assert {key: getattr(zarush, key) for key in preserve_keys} == zarush_before
    for code, before in other_cards_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in before} == before
    assert {key: getattr(owned, key) for key in player_before} == player_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, zarush.id)) == history_count

    timestamp = zarush.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(zarush)
    assert writes == [(zarush.id, expected_fields)] and zarush.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, zarush.id)) == history_count


async def test_zarush_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    zarush = (await _seeded(db_session, shop_code="zarush"))[ZARUSH_CODE]
    user = await create_test_user(db_session, "بازی با زاریوش رسمی")
    await _fund(db_session, user, apples=6700, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == zarush.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialzarush01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialzarush01")
    await db_session.commit()
    assert first.price == 6000 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700

    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{ZARUSH_NAME}*" in detail
    assert f"*وابستگی‌ها*:{ZARUSH_AFFILIATIONS}" in detail
    assert "💪 قدرت: `22`" in detail and "⚡ سرعت: `21`" in detail and "🧠 هوش: `17`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert ZARUSH_STORY not in detail and "*داستان*:" not in detail
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
    assert "💪 قدرت: `32`" in detail and "⚡ سرعت: `31`" in detail and "🧠 هوش: `27`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail and ZARUSH_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
