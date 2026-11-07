"""Dr. Daxibe's official content, art, availability and progression."""

from pathlib import Path

from sib.database.connection import seed_characters
from sib.services.card_progression import attributes_at_level, base_attributes
from sib.services.character_art import artwork_path, resolve_character_photo
from sib.services.characters import render_character_card
from sib.services.factions import FactionService
from sib.services.shop import ShopService
from sib.services.wiki import WikiService
from sib.tests.test_card_lore import RARE_PRICE_LINE as PRICE_LINE, _entries, _seeded

DAXIBE_AFFILIATIONS = "فرزانگان🔮"
DAXIBE_STORY = "دکتر داکسیب، کیمیاگر مرموز فرزانگان، سال‌ها از عمر خود را صرف مطالعه بیماری‌ها و ساخت داروهای کمیاب کرده است. او باور دارد هر بیماری رازی در دل طبیعت پنهان کرده و هیچ درمانی بدون شناخت عمیق آن ممکن نیست."


def test_daxibe_seed_exact_content():
    entry = next(item for item in _entries() if item["code"] == "dr_daxibe")
    assert entry["name_fa"] == "دکتر داکسیب" and entry["name_en"] == "Dr. Daxibe"
    assert entry["affiliations_fa"] == DAXIBE_AFFILIATIONS
    assert entry["lore_fa"] == DAXIBE_STORY
    assert (entry["power"], entry["speed"], entry["intelligence"]) == (18, 16, 26)
    assert entry["faction"] == "sages" and entry["sync_faction"] is True
    assert entry["release_status"] == "RELEASED" and entry["shop_enabled"] is True
    for field in ("passive_key", "passive_data", "hidden_modifiers", "strengths", "weaknesses", "faction_role_fa"):
        assert field not in entry


async def test_daxibe_seed_wiki_shop_and_progression(db_session):
    characters = await _seeded(db_session, shop_code="dr_daxibe")
    daxibe = characters["dr_daxibe"]
    assert daxibe.affiliations_fa == DAXIBE_AFFILIATIONS and daxibe.lore_fa == DAXIBE_STORY
    assert daxibe.passive_key is None and daxibe.hidden_modifiers is None
    assert (await FactionService.get_for_character(db_session, daxibe)).code == "sages"
    path = Path(__file__).resolve().parents[1] / "assets/characters/dr_daxibe.jpg"
    assert artwork_path(daxibe) == path
    assert await resolve_character_photo(db_session, daxibe) == (None, path)
    assert attributes_at_level(base_attributes(daxibe), 10).as_dict() == {
        "power": 108, "speed": 106, "intelligence": 116,
    }
    wiki = await WikiService.wiki_page(db_session, daxibe.id)
    assert DAXIBE_AFFILIATIONS in wiki and DAXIBE_STORY in wiki
    assert render_character_card(daxibe) == wiki + "\n\n" + PRICE_LINE
    assert "dr_daxibe" in {character.code for _, character, _ in await ShopService.list_entries(db_session)}
    assert await seed_characters(db_session) == 0
