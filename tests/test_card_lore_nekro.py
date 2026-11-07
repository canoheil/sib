"""Nekro's supplied content, artwork and availability."""

from pathlib import Path

from sib.services.card_progression import attributes_at_level, base_attributes
from sib.services.character_art import artwork_path
from sib.services.characters import render_character_card
from sib.services.factions import FactionService
from sib.services.shop import ShopService
from sib.services.wiki import WikiService
from sib.tests.test_card_lore import RARE_PRICE_LINE as PRICE_LINE, _entries, _seeded

NEKRO_AFFILIATIONS = "فرزندان ماه🌙 - پنجه‌های ماه🐾"
NEKRO_STORY = "تا پیش از ورود این موجود که حضورش در دنیای اواریا تابو محسوب می‌شود، امبر تنها کسی بود که برای مقابله با سیبومورتیس از جادوی سیاه استفاده کرده بود؛ اما ملکهٔ ماه که در برابر نیروی اهریمن هالووینی مستأصل شده بود، نکرومنسر را از میان خرابه‌های قبرستانی قدیمی احضار کرد و با وجود بی‌اعتمادی و تردید دربارهٔ نیت و مقاصد این موجود، او را به عضویت پنجه‌های ماه درآورد."


def test_nekro_seed_exact_content():
    entry = next(item for item in _entries() if item["code"] == "nekro")
    assert entry["name_fa"] == "نکرو" and entry["name_en"] == "Nekro"
    assert entry["affiliations_fa"] == NEKRO_AFFILIATIONS
    assert entry["lore_fa"] == NEKRO_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (20, 12, 28)
    assert entry["faction"] == "moon_descendants" and entry["sync_faction"] is True
    assert entry["release_status"] == "RELEASED" and entry["shop_enabled"] is True
    for field in ("passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses", "faction_role_fa"):
        assert field not in entry


async def test_nekro_seed_wiki_shop_and_progression(db_session):
    characters = await _seeded(db_session, shop_code="nekro")
    nekro = characters["nekro"]
    assert nekro.affiliations_fa == NEKRO_AFFILIATIONS and nekro.lore_fa == NEKRO_STORY
    assert nekro.passive_key is None and nekro.hidden_modifiers is None
    assert (await FactionService.get_for_character(db_session, nekro)).code == "moon_descendants"
    assert artwork_path(nekro) == Path(__file__).resolve().parents[1] / "assets/characters/nekro.jpg"
    assert attributes_at_level(base_attributes(nekro), 10).as_dict() == {
        "power": 110, "speed": 102, "intelligence": 118,
    }
    wiki = await WikiService.wiki_page(db_session, nekro.id)
    assert NEKRO_AFFILIATIONS in wiki and NEKRO_STORY in wiki
    assert render_character_card(nekro) == wiki + "\n\n" + PRICE_LINE
    assert "nekro" in {character.code for _, character, _ in await ShopService.list_entries(db_session)}
    assert (await _seeded(db_session, shop_code="nekro"))["nekro"].id == nekro.id
