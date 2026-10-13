"""Moon Apple's official text, shared screens and unchanged player state."""

from sib.database.connection import seed_characters
from sib.database.models import Character, PlayerCard, ReleaseStatus
from sib.handlers.cards import _card_view
from sib.handlers.shop import _shop_item_view
from sib.services.apple_wallet import AppleWalletService
from sib.services.card_upgrade import CardUpgradeService
from sib.services.characters import CharacterService, render_character_card
from sib.services.player_cards import PlayerCardService
from sib.services.shop import ShopService
from sib.services.telegram_ui import photo_caption
from sib.services.wiki import CollectionService, WikiService
from sib.tests.test_card_lore import PRICE_LINE, _buttons, _entries, _seeded
from sib.tests.test_card_upgrade import _fund
from sib.tests.test_parties import create_test_user

MOON_CODE = "sib_mahi"
MOON_NAME = "سیب ماهی"
MOON_AFFILIATIONS = "فرزندان ماه🌙"
MOON_STORY = "ملکه فرزندان ماه، کسی که در سایهٔ ماه، فرمانروایی می‌کند؛ ملکه‌ای که حتی نزدیک‌ترین پیروانش نیز از نیت واقعی او آگاه نیستند اما بدون هیچ چون و چرایی فرمان او را اجرا میکنند و تا پای جان به او وفادارند."
MOON_WIKI = (
    f"*{MOON_NAME}*\n\n*وابستگی‌ها*:{MOON_AFFILIATIONS}\n\n"
    f"💪 قدرت: 14\n⚡ سرعت: 16\n🧠 هوش: 30\n\n*داستان*:{MOON_STORY}"
)


def test_moon_apple_seed_keeps_the_exact_official_message():
    entry = next(entry for entry in _entries() if entry["code"] == MOON_CODE)
    assert entry["name_fa"] == MOON_NAME
    assert entry["affiliations_fa"] == MOON_AFFILIATIONS
    assert entry["lore_fa"] == MOON_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (14, 16, 30)
    # Preserve the original combining hamza and the uncorrected spelling.
    assert "سایه\u0654 ماه" in entry["lore_fa"]
    assert "اجرا میکنند" in entry["lore_fa"]
    assert "اجرا می‌کنند" not in entry["lore_fa"]
    assert "بدون هیچ چون و چرایی" in entry["lore_fa"]


async def test_moon_apple_wiki_and_shop_have_identical_full_text(db_session):
    moon = (await _seeded(db_session))[MOON_CODE]
    assert moon.name_fa == MOON_NAME
    assert moon.affiliations_fa == MOON_AFFILIATIONS
    assert moon.lore_fa == MOON_STORY
    assert moon.stats_dict() == {"power": 14, "speed": 16, "intelligence": 30}
    wiki = await WikiService.wiki_page(db_session, moon.id)
    assert wiki == MOON_WIKI
    assert wiki.split("*داستان*:", 1)[1] == MOON_STORY
    assert "قیمت" not in wiki
    assert "🎖 نقش" not in wiki and "📜" not in wiki and "نقاط قوت" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(moon) == wiki + "\n\n" + PRICE_LINE

    user = await create_test_user(db_session, "خریدار سیب ماهی رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == moon.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert body == wiki + "\n\n" + PRICE_LINE
    assert body.endswith(PRICE_LINE) and body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in moon.lore_fa
    assert photo_caption(text) == text  # The complete text fits its caption.
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


async def test_moon_replacement_preserves_other_cards_and_player_state(db_session, monkeypatch):
    characters = await _seeded(db_session)
    moon = characters[MOON_CODE]
    user = await create_test_user(db_session, "حفظ وضعیت سیب ماهی")
    owned = await PlayerCardService.acquire(db_session, user.id, moon.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    # Old-installation test data, never a substitute for the official text.
    moon.affiliations_fa = None
    moon.lore_fa = "در سکوت شب زاده شد؛ هوش سردش هر نقشه‌ای را پیش از اجرا خنثی می‌کند."
    moon.artwork = "custom-moon-artwork"
    moon.release_status = ReleaseStatus.RETIRED
    moon.shop_enabled = False
    moon.is_active = False
    moon.price = 250
    await db_session.commit()
    await db_session.refresh(moon)
    await db_session.refresh(owned)
    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "updated_at"}
    ]
    moon_before = {key: getattr(moon, key) for key in preserve_keys}
    others_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != MOON_CODE
    }
    owned_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    versions_before = len(await CharacterService.get_versions(db_session, moon.id))

    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(moon)
    await db_session.refresh(owned)
    assert writes == [(moon.id, {"lore_fa", "affiliations_fa"})]
    assert moon.affiliations_fa == MOON_AFFILIATIONS and moon.lore_fa == MOON_STORY
    assert {key: getattr(moon, key) for key in preserve_keys} == moon_before
    for code, previous in others_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in previous} == previous
    assert {key: getattr(owned, key) for key in owned_before} == owned_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, moon.id)) == versions_before

    timestamp = moon.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(moon)
    assert writes == [(moon.id, {"lore_fa", "affiliations_fa"})]
    assert moon.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, moon.id)) == versions_before


async def test_moon_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    moon = (await _seeded(db_session))[MOON_CODE]
    # Historical per-card/per-entry prices must not override the 300 rule.
    moon.price = 250
    user = await create_test_user(db_session, "بازی با سیب ماهی رسمی")
    await _fund(db_session, user, apples=1000, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == moon.id
    )
    entry.price_override = 5
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialmoon01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialmoon01")
    await db_session.commit()
    assert first.price == 300 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700
    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{MOON_NAME}*" in detail
    assert f"*وابستگی‌ها*:{MOON_AFFILIATIONS}" in detail
    assert "💪 قدرت: `14`" in detail and "⚡ سرعت: `16`" in detail and "🧠 هوش: `30`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert MOON_STORY not in detail and "*داستان*:" not in detail
    assert photo is not None
    assert f"card_main_set:{card.id}:0" in _buttons(keyboard)
    assert f"card_upgrade:{card.id}" in _buttons(keyboard)
    assert "apple_cards" in _buttons(keyboard)

    await PlayerCardService.set_favorite(db_session, user.id, card.id, expected_main_card_id=0)
    await db_session.commit()
    result = await CardUpgradeService.upgrade(db_session, user.id, card.id, expected_level=1)
    await db_session.commit()
    assert (result.from_level, result.to_level) == (1, 2)
    assert await AppleWalletService.get_balance(db_session, user.id) == 600
    detail, main_id = await CollectionService.render_card_detail_with_main_card(db_session, user.id, card.id)
    assert main_id == card.id
    assert "💪 قدرت: `24`" in detail and "⚡ سرعت: `26`" in detail and "🧠 هوش: `40`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail
    assert MOON_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
