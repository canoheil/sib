"""Moon Lily's exact official lore and consistent game surfaces."""

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

MOON_LILY_CODE = "moon_lily"
MOON_LILY_NAME = "نیلوفر ماه"
MOON_LILY_AFFILIATIONS = "فرزندان ماه🌙"
MOON_LILY_STORY = "نیلوفر ماه، خواهر کوچک‌تر ملکهٔ ماهی؛ گلی ظریف در میان سایه‌های قلمرو ماه. درحالی‌که همه از اسرار ملکه سخن می‌گویند، کمتر کسی می‌داند نیلوفر چه جایگاهی در زندگی او دارد؛ خواهری عزیز، نقطه‌ضعفی پنهان یا رازی که حتی نزدیک‌ترین پیروان ملکه نیز از آن بی‌خبرند."
MOON_LILY_WIKI = (
    f"*{MOON_LILY_NAME}*\n\n*وابستگی‌ها*:{MOON_LILY_AFFILIATIONS}\n\n"
    f"💪 قدرت: 8\n⚡ سرعت: 12\n🧠 هوش: 40\n\n*داستان*:{MOON_LILY_STORY}"
)


def test_moon_lily_seed_preserves_all_official_fields_verbatim():
    entry = next(entry for entry in _entries() if entry["code"] == MOON_LILY_CODE)
    assert entry["name_fa"] == MOON_LILY_NAME
    assert entry["affiliations_fa"] == MOON_LILY_AFFILIATIONS
    assert entry["lore_fa"] == MOON_LILY_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (8, 12, 40)
    assert entry["faction"] == "moon_descendants" and entry["sync_faction"] is True
    assert "کوچک‌تر ملکهٔ ماهی" in entry["lore_fa"]
    assert "درحالی‌که" in entry["lore_fa"]
    assert "نقطه‌ضعفی پنهان" in entry["lore_fa"]
    assert entry["lore_fa"].endswith("از آن بی‌خبرند.")
    for field in ("faction_role_fa", "passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses"):
        assert field not in entry


async def test_moon_lily_wiki_and_shop_render_the_complete_official_text(db_session):
    characters = await _seeded(db_session, shop_code="moon_lily")
    lily = characters[MOON_LILY_CODE]
    assert lily.name_fa == MOON_LILY_NAME
    assert lily.affiliations_fa == MOON_LILY_AFFILIATIONS
    assert lily.lore_fa == MOON_LILY_STORY
    assert lily.stats_dict() == {"power": 8, "speed": 12, "intelligence": 40}
    assert lily.faction_role_fa is None and lily.passive_key is None
    assert lily.hidden_modifiers is None and not lily.strengths_list and not lily.weaknesses_list
    faction = await FactionService.get_for_character(db_session, lily)
    assert faction is not None and faction.code == "moon_descendants"

    wiki = await WikiService.wiki_page(db_session, lily.id)
    assert wiki == MOON_LILY_WIKI
    assert wiki.split("*داستان*:", 1)[1] == MOON_LILY_STORY
    assert "قیمت" not in wiki and faction.lore_fa not in wiki
    assert "نقش:" not in wiki and "توانایی منفعل" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(lily, faction=faction) == wiki + "\n\n" + PRICE_LINE

    index = "\n".join(_wiki_index_lines([lily], {faction.id: faction}))
    assert "فرزندان ماه" in index and "🌙" in index
    assert f"*{MOON_LILY_NAME}*" in index and "بدون دسته" not in index
    ordered = await WikiService.list_wiki(db_session)
    ordered_ids = [character.id for character in ordered]
    assert ordered_ids.index(characters["sib_mahi"].id) < ordered_ids.index(characters["mmung"].id)
    assert ordered_ids.index(characters["mmung"].id) < ordered_ids.index(characters["moon_knight"].id)
    assert ordered_ids.index(characters["moon_knight"].id) < ordered_ids.index(lily.id)

    user = await create_test_user(db_session, "خریدار نیلوفر ماه رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == lily.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    card_body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert card_body == wiki + "\n\n" + PRICE_LINE
    assert card_body.endswith(PRICE_LINE) and card_body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in lily.lore_fa and photo_caption(text) == text
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


@pytest.mark.parametrize("previous_faction", [None, "empire"])
async def test_moon_lily_sync_preserves_other_cards_and_player_state(db_session, monkeypatch, previous_faction):
    characters = await _seeded(db_session)
    lily = characters[MOON_LILY_CODE]
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    empire = await FactionService.get_by_code(db_session, "empire")
    user = await create_test_user(db_session, "حفظ وضعیت نیلوفر ماه")
    owned = await PlayerCardService.acquire(db_session, user.id, lily.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    lily.affiliations_fa = None
    lily.lore_fa = ""
    lily.faction_id = empire.id if previous_faction else None
    lily.faction_role_fa = "نقش قبلی مدیریت‌شده"
    lily.artwork = "custom-moon-lily-artwork"
    lily.release_status = ReleaseStatus.RETIRED
    lily.shop_enabled = False
    lily.is_active = False
    lily.price = 77
    await db_session.commit()
    await db_session.refresh(lily)
    await db_session.refresh(owned)

    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "faction_id", "updated_at"}
    ]
    lily_before = {key: getattr(lily, key) for key in preserve_keys}
    other_cards_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != MOON_LILY_CODE
    }
    player_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    history_count = len(await CharacterService.get_versions(db_session, lily.id))
    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(lily)
    await db_session.refresh(owned)

    expected_fields = {"lore_fa", "affiliations_fa"}
    if previous_faction:
        expected_fields.add("faction_id")
    assert writes == [(lily.id, expected_fields)]
    assert lily.affiliations_fa == MOON_LILY_AFFILIATIONS
    assert lily.lore_fa == MOON_LILY_STORY and lily.faction_id == moon.id
    assert {key: getattr(lily, key) for key in preserve_keys} == lily_before
    for code, before in other_cards_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in before} == before
    assert {key: getattr(owned, key) for key in player_before} == player_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, lily.id)) == history_count

    timestamp = lily.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(lily)
    assert writes == [(lily.id, expected_fields)] and lily.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, lily.id)) == history_count


async def test_moon_lily_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    lily = (await _seeded(db_session, shop_code="moon_lily"))[MOON_LILY_CODE]
    user = await create_test_user(db_session, "بازی با نیلوفر ماه رسمی")
    await _fund(db_session, user, apples=2700, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == lily.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officiallily001")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officiallily001")
    await db_session.commit()
    assert first.price == 2000 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700

    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{MOON_LILY_NAME}*" in detail
    assert f"*وابستگی‌ها*:{MOON_LILY_AFFILIATIONS}" in detail
    assert "💪 قدرت: `8`" in detail and "⚡ سرعت: `12`" in detail and "🧠 هوش: `40`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert MOON_LILY_STORY not in detail and "*داستان*:" not in detail
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
    assert "💪 قدرت: `18`" in detail and "⚡ سرعت: `22`" in detail and "🧠 هوش: `50`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail and MOON_LILY_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
