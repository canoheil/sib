"""Card factions: data-driven lore metadata (task requirement #6).

Pins the agreed contract:
- three canon factions exist as DATA (Empire, Sages, Moon Descendants) and
  the four seeded characters belong to the right ones;
- the system is extensible: a fourth faction is data only (no code change);
- factions are lore-only: no stats, no balance-version impact, no gameplay
  coupling;
- the migration/backfill is non-destructive (never overwrites, only fills
  NULLs; the deprecated rarity column is preserved);
- the world name is NOT invented ("Avarion" is not canon).
"""

from pathlib import Path

import pytest
from sqlalchemy import select

from sib.database.connection import seed_characters
from sib.database.models import (
    Character,
    CharacterBalanceVersion,
    Faction,
    PlayerCard,
    ReleaseStatus,
)
from sib.services.characters import CharacterService, render_character_card
from sib.services.factions import FactionService
from sib.services.wiki import WikiService
from sib.tests.test_parties import create_test_user

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "sib_sorkh": "empire",
    "sib_sabz": "sages",
    "sib_talayi": "sages",
    "sib_mahi": "moon_descendants",
}


async def _seeded_characters(db_session):
    await seed_characters(db_session)
    await db_session.commit()
    return list(
        (
            await db_session.scalars(
                select(Character).where(Character.code.in_(EXPECTED.keys()))
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# Canon data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_creates_the_three_canon_factions(db_session):
    await seed_characters(db_session)
    await db_session.commit()

    factions = {f.code: f for f in await FactionService.list_active(db_session)}
    assert set(factions) == {"empire", "sages", "moon_descendants"}

    empire = factions["empire"]
    sages = factions["sages"]
    moon = factions["moon_descendants"]
    assert empire.name_fa and empire.lore_fa
    assert sages.name_fa and sages.lore_fa
    assert moon.name_fa and moon.lore_fa
    # Moonfall Valley is the Moon Descendants' territory.
    assert "Moonfall Valley" in (moon.territory_fa or "")
    # Lore only: no mechanics attached to a faction row.
    assert not hasattr(empire, "bonus")
    assert not hasattr(empire, "power")


@pytest.mark.asyncio
async def test_seeded_characters_get_their_faction_and_role(db_session):
    characters = await _seeded_characters(db_session)
    by_code = {c.code: c for c in characters}
    assert set(by_code) == set(EXPECTED)

    for code, faction_code in EXPECTED.items():
        character = by_code[code]
        faction = await FactionService.get_for_character(db_session, character)
        assert faction is not None and faction.code == faction_code
        assert character.faction_role_fa  # a role inside the faction

    # The canonical roles from the lore.
    assert "جنگجوی امپراتوری" in by_code["sib_sorkh"].faction_role_fa
    assert "فرزانگان" in by_code["sib_talayi"].faction_role_fa
    assert "ملکه ماه" in by_code["sib_mahi"].faction_role_fa
    # The Golden Apple is the Sages' princess and the Green Apple her mentor.
    assert "دختر پادشاه" in by_code["sib_talayi"].faction_role_fa
    assert "آموزگار" in by_code["sib_sabz"].faction_role_fa


@pytest.mark.asyncio
async def test_backfill_only_fills_nulls_and_never_overwrites(db_session):
    """Existing installations keep their data; the backfill is additive."""
    await _seeded_characters(db_session)
    character = await db_session.scalar(
        select(Character).where(Character.code == "sib_sorkh")
    )

    # Simulate a legacy install that skipped faction metadata ...
    character.faction_id = None
    character.faction_role_fa = None
    await db_session.commit()

    linked, roles = await FactionService.backfill_characters_from_data(db_session)
    await db_session.commit()
    await db_session.refresh(character)
    assert linked >= 1 and roles >= 1
    assert character.faction_id is not None

    # ... and a hand-edited faction must survive a re-run.
    moon = await FactionService.get_by_code(db_session, "moon_descendants")
    character.faction_id = moon.id
    character.faction_role_fa = "نقش دستی"
    await db_session.commit()

    await FactionService.backfill_characters_from_data(db_session)
    await db_session.commit()
    await db_session.refresh(character)
    assert character.faction_id == moon.id
    assert character.faction_role_fa == "نقش دستی"


# ---------------------------------------------------------------------------
# Extensibility (no code change for Faction 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fourth_faction_is_data_only(db_session):
    await seed_characters(db_session)
    await db_session.commit()

    fourth = Faction(
        code="sky_wardens",
        name_fa="نگهبانان آسمان",
        symbol="🪽",
        display_order=4,
        lore_fa=" lore ",
    )
    db_session.add(fourth)
    await db_session.flush()

    character = await CharacterService.create(
        db_session,
        code="sky_hero",
        name_fa="قهرمان آسمان",
        power=20,
        speed=20,
        intelligence=20,
        faction_id=fourth.id,
        faction_role_fa="پیام‌آور نگهبانان",
    )
    await CharacterService.set_release_status(
        db_session, character.id, ReleaseStatus.RELEASED
    )
    await db_session.commit()

    # The generic rendering picks it up with no code change ...
    card = render_character_card(character, faction=fourth)
    assert "نگهبانان آسمان" in card
    assert "پیام‌آور نگهبانان" in card
    # ... and so does the wiki grouping (faction list is data-driven).
    listed = {f.code for f in await FactionService.list_active(db_session)}
    assert "sky_wardens" in listed
    assert await WikiService.wiki_page(db_session, character.id) is not None


@pytest.mark.asyncio
async def test_unaffiliated_characters_render_without_a_faction(db_session):
    character = await CharacterService.create(
        db_session, code="lone", name_fa="تنها", power=20, speed=20, intelligence=20
    )
    await db_session.commit()
    card = render_character_card(character)
    assert "تنها" in card
    assert "نقش" not in card


# ---------------------------------------------------------------------------
# Lore only: no gameplay coupling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_faction_does_not_touch_stats_or_balance(db_session):
    """Two identical characters differ ONLY by faction."""
    await seed_characters(db_session)
    await db_session.commit()
    empire = await FactionService.get_by_code(db_session, "empire")
    moon = await FactionService.get_by_code(db_session, "moon_descendants")

    a = await CharacterService.create(
        db_session, code="twin_a", name_fa="دوقلوی الف",
        power=22, speed=20, intelligence=18, faction_id=empire.id,
    )
    b = await CharacterService.create(
        db_session, code="twin_b", name_fa="دوقلوی ب",
        power=22, speed=20, intelligence=18, faction_id=moon.id,
    )
    await db_session.commit()

    # Faction is not part of the balance snapshot, and the two characters
    # are mechanically identical (codes differ by design).
    assert "faction" not in a.snapshot()
    snap_a = {k: v for k, v in a.snapshot().items() if k != "code"}
    snap_b = {k: v for k, v in b.snapshot().items() if k != "code"}
    assert snap_a == snap_b
    assert a.stats_dict() == b.stats_dict()

    # A card's gameplay values are identical too (no bonuses).
    user = await create_test_user(db_session, "دوگانه")
    from sib.services.player_cards import PlayerCardService

    card_a = await PlayerCardService.acquire(db_session, user.id, a.id)
    await db_session.commit()
    from sib.services.card_progression import card_attributes

    stats = card_attributes(card_a, a)
    assert stats.power == a.power == 22
    assert stats.speed == b.speed == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("official", [True, False])
async def test_owned_card_detail_shows_faction_lore(db_session, official):
    """Official affiliations win; unmarked cards retain legacy role display."""
    await seed_characters(db_session)
    await db_session.commit()
    user = await create_test_user(db_session, "ویکی‌کارت")
    character = await db_session.scalar(
        select(Character).where(Character.code == "sib_mahi")
    )
    from sib.services.player_cards import PlayerCardService
    from sib.services.wiki import CollectionService

    if not official:
        # Emulate the old installation in this isolated fixture only.
        character.affiliations_fa = None
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()

    detail = await CollectionService.render_card_detail(db_session, user.id, card.id)
    assert character.faction_role_fa == "ملکه ماه، رهبر فرزندان ماه"
    if official:
        assert character.affiliations_fa == "فرزندان ماه🌙"
        assert f"*وابستگی‌ها*:{character.affiliations_fa}" in detail
        assert "نقش:" not in detail  # Do not append the old, unsupplied role.
    else:
        assert "ملکه ماه" in detail  # Preserve the original legacy coverage.
    assert "فرزندان ماه" in detail
    assert character.lore_fa not in detail  # This interface has no story.
    # Stats stay untouched by faction metadata.
    assert f"💪 قدرت: `{character.power}`" in detail
    assert f"⚡ سرعت: `{character.speed}`" in detail
    assert f"🧠 هوش: `{character.intelligence}`" in detail


def test_gameplay_modules_never_read_faction_metadata():
    """Factions are lore: gameplay code must not even mention them."""
    gameplay_files = [
        "services/apple_battle.py",
        "services/match_result.py",
        "services/trading.py",
        "services/garden.py",
        "services/items.py",
        "matchmaking/matcher.py",
        "matchmaking/acceptance.py",
    ]
    for relative in gameplay_files:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        code_lines = [
            line
            for line in source.splitlines()
            if not line.strip().startswith("#")
        ]
        assert "faction" not in "\n".join(code_lines).lower(), (
            f"{relative} must not branch on faction metadata"
        )


def test_world_name_is_not_invented():
    """'Avarion' is not canon: no data/code/art file may claim it."""
    targets = list(REPO_ROOT.glob("data/*.json")) + [
        REPO_ROOT / "services" / "factions.py",
        REPO_ROOT / "data" / "factions_seed.json",
    ]
    for path in targets:
        if path.exists():
            assert "avarion" not in path.read_text(encoding="utf-8").lower()
