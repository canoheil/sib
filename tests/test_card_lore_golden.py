"""Golden Apple's official text and shared card interfaces, verbatim."""

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

GOLDEN_CODE = "sib_talayi"
GOLDEN_NAME = "سیب طلایی"
GOLDEN_AFFILIATIONS = "فرزانگان🔮"
GOLDEN_STORY = "دختر پادشاه فرزانگان و شاگرد استاد پیر، سیب سبز؛ وارث خردی که نسل‌ها برای به‌دست‌آوردنش تلاش کرده‌اند. اما برخلاف آنچه دیگران می‌پندارند، بزرگ‌ترین راز او دانسته‌هایش نیست، بلکه چیزی است که هنوز تصمیم نگرفته به کسی بیاموزد."
GOLDEN_WIKI = (
    f"*{GOLDEN_NAME}*\n\n*وابستگی‌ها*:{GOLDEN_AFFILIATIONS}\n\n"
    f"💪 قدرت: 20\n⚡ سرعت: 24\n🧠 هوش: 16\n\n*داستان*:{GOLDEN_STORY}"
)


def test_golden_apple_seed_keeps_the_exact_official_message():
    entry = next(entry for entry in _entries() if entry["code"] == GOLDEN_CODE)
    assert entry["name_fa"] == GOLDEN_NAME
    assert entry["affiliations_fa"] == GOLDEN_AFFILIATIONS
    assert entry["lore_fa"] == GOLDEN_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (20, 24, 16)
    # Preserve the teacher's name and the original zero-width non-joiners.
    assert "استاد پیر، سیب سبز؛" in entry["lore_fa"]
    assert "نسل‌ها برای به‌دست‌آوردنش" in entry["lore_fa"]
    assert "بزرگ‌ترین راز او دانسته‌هایش" in entry["lore_fa"]


async def test_golden_apple_wiki_and_shop_have_identical_full_text(db_session):
    golden = (await _seeded(db_session, shop_code="sib_talayi"))[GOLDEN_CODE]
    assert golden.name_fa == GOLDEN_NAME
    assert golden.affiliations_fa == GOLDEN_AFFILIATIONS
    assert golden.lore_fa == GOLDEN_STORY
    assert golden.stats_dict() == {"power": 20, "speed": 24, "intelligence": 16}
    wiki = await WikiService.wiki_page(db_session, golden.id)
    assert wiki == GOLDEN_WIKI
    assert wiki.split("*داستان*:", 1)[1] == GOLDEN_STORY
    assert "قیمت" not in wiki
    assert "🎖 نقش" not in wiki and "📜" not in wiki and "نقاط قوت" not in wiki
    assert photo_caption(wiki) == wiki
    assert render_character_card(golden) == wiki + "\n\n" + PRICE_LINE

    user = await create_test_user(db_session, "خریدار سیب طلایی رسمی")
    await db_session.commit()
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session, user_id=user.id)
        if character.id == golden.id
    )
    await db_session.commit()
    text, keyboard, photo = await _shop_item_view(user.id, entry.id)
    body, wallet = text.split("\n\n💰 سیب‌های شما:", 1)
    assert body == wiki + "\n\n" + PRICE_LINE
    assert body.endswith(PRICE_LINE) and body.count(PRICE_LINE) == 1
    assert PRICE_LINE not in golden.lore_fa
    assert photo_caption(text) == text  # This card fits without truncation.
    assert wallet and photo is not None
    assert any(callback.startswith(f"shop_buy:{entry.id}:") for callback in _buttons(keyboard))
    assert "shop_cards:0" in _buttons(keyboard)


async def test_golden_replacement_preserves_other_cards_and_player_state(db_session, monkeypatch):
    characters = await _seeded(db_session)
    golden = characters[GOLDEN_CODE]
    user = await create_test_user(db_session, "حفظ وضعیت سیب طلایی")
    owned = await PlayerCardService.acquire(db_session, user.id, golden.id)
    owned.level = 4
    await db_session.flush()
    await PlayerCardService.set_favorite(db_session, user.id, owned.id, expected_main_card_id=0)
    await AppleWalletService.earn(db_session, user.id, 900, reason="test_grant")

    # Emulate the old installation; this is test data, not official lore.
    golden.affiliations_fa = None
    golden.lore_fa = "افسانه‌ی باغ‌ها؛ می‌گویند هر کس او را شکست دهد، یک روز خوش‌شانسی را از آن خود کرده است."
    golden.artwork = "custom-golden-artwork"
    golden.release_status = ReleaseStatus.RETIRED
    golden.shop_enabled = False
    golden.is_active = False
    await db_session.commit()
    await db_session.refresh(golden)
    await db_session.refresh(owned)
    preserve_keys = [
        column.name for column in Character.__table__.columns
        if column.name not in {"lore_fa", "affiliations_fa", "updated_at"}
    ]
    golden_before = {key: getattr(golden, key) for key in preserve_keys}
    others_before = {
        code: {column.name: getattr(character, column.name) for column in Character.__table__.columns}
        for code, character in characters.items() if code != GOLDEN_CODE
    }
    owned_before = {column.name: getattr(owned, column.name) for column in PlayerCard.__table__.columns}
    versions_before = len(await CharacterService.get_versions(db_session, golden.id))

    original_update = CharacterService.update
    writes = []

    async def record_update(session, character_id, **fields):
        writes.append((character_id, set(fields) - {"note"}))
        return await original_update(session, character_id, **fields)

    monkeypatch.setattr(CharacterService, "update", staticmethod(record_update))
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(golden)
    await db_session.refresh(owned)
    assert writes == [(golden.id, {"lore_fa", "affiliations_fa"})]
    assert golden.affiliations_fa == GOLDEN_AFFILIATIONS and golden.lore_fa == GOLDEN_STORY
    assert {key: getattr(golden, key) for key in preserve_keys} == golden_before
    for code, previous in others_before.items():
        await db_session.refresh(characters[code])
        assert {key: getattr(characters[code], key) for key in previous} == previous
    assert {key: getattr(owned, key) for key in owned_before} == owned_before
    assert owned.level == 4 and owned.is_favorite
    assert await AppleWalletService.get_balance(db_session, user.id) == 900
    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == owned.id
    assert len(await CharacterService.get_versions(db_session, golden.id)) == versions_before

    timestamp = golden.updated_at
    assert await seed_characters(db_session) == 0
    await db_session.commit()
    await db_session.refresh(golden)
    assert writes == [(golden.id, {"lore_fa", "affiliations_fa"})]
    assert golden.updated_at == timestamp
    assert len(await CharacterService.get_versions(db_session, golden.id)) == versions_before


async def test_golden_purchase_main_selection_upgrade_and_back_are_preserved(db_session):
    golden = (await _seeded(db_session, shop_code="sib_talayi"))[GOLDEN_CODE]
    user = await create_test_user(db_session, "بازی با سیب طلایی رسمی")
    await _fund(db_session, user, apples=1000, gold=3)
    entry = next(
        entry for entry, character, _ in await ShopService.list_entries(db_session)
        if character.id == golden.id
    )
    await db_session.commit()
    first = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialgold01")
    await db_session.commit()
    replay = await ShopService.purchase(db_session, user.id, entry.id, nonce="officialgold01")
    await db_session.commit()
    assert first.price == 300 and not first.replayed and replay.replayed
    assert await AppleWalletService.get_balance(db_session, user.id) == 700
    card = first.card
    detail, keyboard, photo = await _card_view(user.id, card.id)
    assert f"*{GOLDEN_NAME}*" in detail
    assert f"*وابستگی‌ها*:{GOLDEN_AFFILIATIONS}" in detail
    assert "💪 قدرت: `20`" in detail and "⚡ سرعت: `24`" in detail and "🧠 هوش: `16`" in detail
    assert "مجموع ویژگی‌ها: `60`" in detail
    assert GOLDEN_STORY not in detail and "*داستان*:" not in detail
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
    assert "💪 قدرت: `30`" in detail and "⚡ سرعت: `34`" in detail and "🧠 هوش: `26`" in detail
    assert "مجموع ویژگی‌ها: `90`" in detail
    assert GOLDEN_STORY not in detail
    _detail, keyboard, _photo = await _card_view(user.id, card.id)
    assert not any(callback.startswith("card_main_set:") for callback in _buttons(keyboard))
    assert f"card_upgrade:{card.id}" in _buttons(keyboard) and "apple_cards" in _buttons(keyboard)
