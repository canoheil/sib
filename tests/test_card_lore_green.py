"""Green Apple's official text and its shared game surfaces, verbatim."""

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

GREEN_CODE = "sib_sabz"
GREEN_NAME = "سیب سبز"
GREEN_AFFILIATIONS = "فرزانگان🔮"
GREEN_STORY = "برادر پادشاه فرزانگان و استاد سیب طلایی؛ پیرمردی که پیش آز اغاز هر نبرد پایان آن را در ذهنش رقم میزند. او به شاگردش آموخت چگونه پیروز شود، اما هنوز معلوم نیست مهم ترین درسی که او پنهان کرده، چیست."
GREEN_WIKI = (
    f"*{GREEN_NAME}*\n\n*وابستگی‌ها*:{GREEN_AFFILIATIONS}\n\n"
    f"💪 قدرت: 16\n⚡ سرعت: 18\n🧠 هوش: 26\n\n*داستان*:{GREEN_STORY}"
)


def test_green_apple_seed_keeps_the_exact_official_message():
    entry = next(entry for entry in _entries() if entry["code"] == GREEN_CODE)
    assert entry["name_fa"] == GREEN_NAME
    assert entry["affiliations_fa"] == GREEN_AFFILIATIONS
    assert entry["lore_fa"] == GREEN_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (16, 18, 26)
    assert "پیش آز اغاز" in entry["lore_fa"]
    assert "رقم میزند" in entry["lore_fa"] and "مهم ترین" in entry["lore_fa"]


async def test_green_apple_wiki_and_shop_have_the_same_unabridged_text(db_session):
    green = (await _seeded(db_session))[GREEN_CODE]
    assert green.name_fa == GREEN_NAME
    assert green.affiliations_fa == GREEN_AFFILIATIONS
    assert green.lore_fa == GREEN_STORY
    assert green.stats_dict() == {"power": 16, "speed": 18, "intelligence": 26}
    wiki = await WikiService.wiki_page(db_session, green.id)
    assert wiki == GREEN_WIKI
    assert wiki.split("*داستان*:", 1)[1] == GREEN_STORY
    assert "قیمت" not in wiki
    assert "🎖 نقش" not in wiki and "📜" not in wiki and "نقاط قوت" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(green) == wiki + "\n\n" + PRICE_LINE

    user = await create_test_user(db_session, "خریدار سیب سبز رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == green.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert body == wiki + "\n\n" + PRICE_LINE
    assert body.endswith(PRICE_LINE) and body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in green.lore_fa
    assert photo_caption(text) == text  # This card fits: no story shortening.
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


async def test_green_apple_replacement_preserves_every_other_card_and_player_state(db_session):
    characters = await _seeded(db_session)
    green = characters[GREEN_CODE]
    user = await create_test_user(db_session, "حفظ وضعیت سیب سبز")
    owned = await PlayerCardService.acquire(db_session, user.id, green.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    # Existing-installation data, not another narrative source.
    green.affiliations_fa = None
    green.lore_fa = "استاد صبر و استراتژی؛ حریفش را با آرامش، قدم‌به‌قدم به دام می‌اندازد."
    green.artwork = "custom-green-artwork"
    green.release_status = ReleaseStatus.RETIRED
    green.shop_enabled = False
    green.is_active = False
    green.price = 120
    await db_session.commit()
    await db_session.refresh(green)
    await db_session.refresh(owned)
    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "updated_at"}
    ]
    green_before = {key: getattr(green, key) for key in preserve_keys}
    others_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != GREEN_CODE
    }
    owned_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    versions_before = len(await CharacterService.get_versions(db_session, green.id))

    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(green)
    await db_session.refresh(owned)
    assert green.affiliations_fa == GREEN_AFFILIATIONS and green.lore_fa == GREEN_STORY
    assert {key: getattr(green, key) for key in preserve_keys} == green_before
    for code, previous in others_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in previous} == previous
    assert {key: getattr(owned, key) for key in owned_before} == owned_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, green.id)) == versions_before

    timestamp = green.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(green)
    assert green.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, green.id)) == versions_before


async def test_green_apple_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    green = (await _seeded(db_session))[GREEN_CODE]
    user = await create_test_user(db_session, "بازی با سیب سبز رسمی")
    await _fund(db_session, user, apples=1000, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == green.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialgreen01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialgreen01")
    await db_session.commit()
    assert first.price == 300 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700
    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{GREEN_NAME}*" in detail
    assert f"*وابستگی‌ها*:{GREEN_AFFILIATIONS}" in detail
    assert "💪 قدرت: `16`" in detail and "⚡ سرعت: `18`" in detail and "🧠 هوش: `26`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert GREEN_STORY not in detail and "*داستان*:" not in detail
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
    assert "💪 قدرت: `26`" in detail and "⚡ سرعت: `28`" in detail and "🧠 هوش: `36`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail
    assert GREEN_STORY not in detail
