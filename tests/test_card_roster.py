"""The delivered new cards («Filo» … «Zarush», plus «Mahmoud» and «Tifun») — roster, art,
stats, budget, shop.

The suite began as the 11-card delivery and is append-only like the roster
it pins: the later cards («محمود» / `mahmud`, «طیفان» / `tifun`) joined the
same tables, so counts and pagination below cover 18 characters
(4 audited + 13 supplied + 1 league trophy).

Covers the whole delivery end to end:

* registration: unique codes, exact image mapping, seed idempotency;
* the fixed Level-1 budget (60) and its non-configurability, the untouched
  four audited cards, and Level-10 totals through the real progression
  helper (no formula change);
* every game surface: shop rotation + list/detail screens, wiki index and
  character page, owned-card collection/detail;
* shop economics: exactly 300 Apples per card, deducted once, with the
  existing ownership/idempotency/balance protections intact;
* seed safety: re-running creates nothing and never touches player data.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from sib.database.models import (
    SHOP_CARD_PRICE_APPLES,
    AppleTransaction,
    Character,
    PlayerCard,
    ReleaseStatus,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.card_progression import (
    ATTRIBUTE_STEP,
    CARD_BASE_ATTRIBUTE_BUDGET,
    MAX_CARD_LEVEL,
    attributes_at_level,
    base_attributes,
    card_attributes,
)
from sib.services.characters import CharacterService, CharacterValidationError
from sib.services.factions import FactionService
from sib.services.shop import ShopError, ShopService
from sib.services.wiki import CollectionService, WikiService
from sib.tests.test_card_lore_filo import FILO_AFFILIATIONS, FILO_STORY
from sib.tests.test_card_lore_goje import GOJE_AFFILIATIONS, GOJE_STORY
from sib.tests.test_card_lore_green_knight import KNIGHT_AFFILIATIONS, KNIGHT_STORY
from sib.tests.test_card_lore_igris import IGRIS_AFFILIATIONS, IGRIS_STORY
from sib.tests.test_card_lore_mahmud import MAHMUD_AFFILIATIONS, MAHMUD_STORY
from sib.tests.test_card_lore_mmung import MMUNG_AFFILIATIONS, MMUNG_STORY
from sib.tests.test_card_lore_moon_knight import MOON_KNIGHT_AFFILIATIONS, MOON_KNIGHT_STORY
from sib.tests.test_card_lore_moon_lily import MOON_LILY_AFFILIATIONS, MOON_LILY_STORY
from sib.tests.test_card_lore_sage_king import SAGE_KING_AFFILIATIONS, SAGE_KING_STORY
from sib.tests.test_card_lore_tifun import TIFUN_AFFILIATIONS, TIFUN_STORY
from sib.tests.test_card_lore_treyni import TREYNI_AFFILIATIONS, TREYNI_STORY
from sib.tests.test_card_lore_wizard import WIZARD_AFFILIATIONS, WIZARD_STORY
from sib.tests.test_card_lore_zarush import ZARUSH_AFFILIATIONS, ZARUSH_STORY
from sib.tests.test_parties import create_test_user

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_PATH = REPO_ROOT / "data" / "characters_seed.json"

# The authoritative roster table: code == artwork filename stem, which is
# how ``services.character_art.artwork_path`` resolves bundled art. Names and
# stats are pinned; official text appears only for cards explicitly submitted
# so far. No role or gameplay ability is inferred from any story.
NEW_CARDS: dict[str, dict] = {
    "filo": {"name_en": "Filo", "power": 7, "speed": 13, "intelligence": 40},
    "goje": {"name_en": "Goje", "power": 20, "speed": 20, "intelligence": 20},
    "green_knight": {"name_en": "Green Knight", "power": 27, "speed": 19, "intelligence": 14},
    "igris": {"name_en": "Igris", "power": 32, "speed": 17, "intelligence": 11},
    "mahmud": {"name_en": "Mahmoud", "power": 18, "speed": 17, "intelligence": 25},
    "mmung": {"name_en": "Mmung", "power": 14, "speed": 36, "intelligence": 10},
    "moon_knight": {"name_en": "Moon Knight", "power": 30, "speed": 20, "intelligence": 10},
    "moon_lily": {"name_en": "Moon Lily", "power": 8, "speed": 12, "intelligence": 40},
    "sage_king": {"name_en": "Sage King", "power": 20, "speed": 12, "intelligence": 28},
    "tifun": {"name_en": "Tifun", "power": 15, "speed": 15, "intelligence": 30},
    "treyni": {"name_en": "Treyni", "power": 12, "speed": 24, "intelligence": 24},
    "wizard": {"name_en": "Wizard", "power": 24, "speed": 10, "intelligence": 26},
    "zarush": {"name_en": "Zarush", "power": 22, "speed": 21, "intelligence": 17},
}
NEW_CODES = tuple(NEW_CARDS)

# Only explicitly received official text is allowed on this later roster.
# It is keyed by card: every entry below is verbatim supplied content.
OFFICIAL_NEW_TEXT = {
    "filo": (FILO_AFFILIATIONS, FILO_STORY, "empire"),
    "goje": (GOJE_AFFILIATIONS, GOJE_STORY, "empire"),
    "green_knight": (KNIGHT_AFFILIATIONS, KNIGHT_STORY, "sages"),
    "igris": (IGRIS_AFFILIATIONS, IGRIS_STORY, "empire"),
    "mahmud": (MAHMUD_AFFILIATIONS, MAHMUD_STORY, "empire"),
    "mmung": (MMUNG_AFFILIATIONS, MMUNG_STORY, "moon_descendants"),
    "moon_knight": (MOON_KNIGHT_AFFILIATIONS, MOON_KNIGHT_STORY, "moon_descendants"),
    "moon_lily": (MOON_LILY_AFFILIATIONS, MOON_LILY_STORY, "moon_descendants"),
    "sage_king": (SAGE_KING_AFFILIATIONS, SAGE_KING_STORY, "sages"),
    "tifun": (TIFUN_AFFILIATIONS, TIFUN_STORY, "sages"),
    "treyni": (TREYNI_AFFILIATIONS, TREYNI_STORY, "sages"),
    "wizard": (WIZARD_AFFILIATIONS, WIZARD_STORY, "moon_descendants"),
    "zarush": (ZARUSH_AFFILIATIONS, ZARUSH_STORY, "empire"),
}

# Event-exclusive cards (`shop_enabled=False`): full roster & wiki members,
# deliberately excluded from the 300-Apple weekly shop rotation.
# ``hero`` is the league champion trophy: it is granted by the weekly podium
# (services/group_league.py), never sold, drawn or handed out by the starter
# pack, so it can never appear in a rotation.
EXCLUSIVE_CARDS: dict[str, dict] = {
    "amber": {"name_en": "Amber", "power": 15, "speed": 20, "intelligence": 25},
    "hero": {"name_en": "Hero", "power": 16, "speed": 34, "intelligence": 10},
}

# The four pre-existing cards: pinned so the new delivery cannot have
# silently rebalanced them.
AUDITED_CARDS = {
    "sib_sorkh": (22, 20, 18),
    "sib_sabz": (16, 18, 26),
    "sib_talayi": (20, 24, 16),
    "sib_mahi": (14, 16, 30),
}


def _seed_entries() -> list[dict]:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


async def _seeded(session) -> dict[str, Character]:
    from sib.database.connection import seed_characters

    await seed_characters(session)
    await session.commit()
    rows = (await session.scalars(select(Character))).all()
    return {character.code: character for character in rows}


async def _buyer(session, apples: int = 1_000) -> int:
    user = await create_test_user(session, "خریدار کارت‌های تازه")
    await AppleWalletService.earn(session, user.id, apples, reason="test_grant")
    await session.commit()
    return user.id


# ---------------------------------------------------------------------------
# 1. Registration, identifiers, seed data
# ---------------------------------------------------------------------------


def test_all_new_cards_are_declared_with_unique_codes():
    entries = _seed_entries()
    codes = [entry["code"] for entry in entries]
    assert len(codes) == len(set(codes)), "character codes must be unique"
    assert set(NEW_CODES) <= set(codes)
    # Stable, filesystem-safe identifiers (they are also the art filenames).
    for code in NEW_CODES:
        assert code.isascii() and code.lower() == code
        assert code.replace("_", "").isalnum()


def test_seed_entries_carry_exactly_the_delivered_stats_and_no_invented_metadata():
    entries = {entry["code"]: entry for entry in _seed_entries()}
    for code, expected in NEW_CARDS.items():
        entry = entries[code]
        assert entry["name_en"] == expected["name_en"]
        assert (entry["power"], entry["speed"], entry["intelligence"]) == (
            expected["power"],
            expected["speed"],
            expected["intelligence"],
        )
        assert entry["release_status"] == "RELEASED"
        assert entry["shop_enabled"] is True
        if code in OFFICIAL_NEW_TEXT:
            # Only these cards have received their official content, verbatim.
            affiliations, story, faction_code = OFFICIAL_NEW_TEXT[code]
            assert entry["affiliations_fa"] == affiliations
            assert entry["lore_fa"] == story
            assert entry["faction"] == faction_code and entry["sync_faction"] is True
        else:
            # No other card in this delivery has received official lore yet.
            for field in ("lore_fa", "affiliations_fa", "faction", "sync_faction"):
                assert field not in entry, f"{code} must not invent {field}"
        for field in ("faction_role_fa", "passive_key", "passive_data",
                      "hidden_modifiers", "strengths", "weaknesses"):
            assert field not in entry, f"{code} must not invent {field}"
        assert entry.get("artwork") is None  # bundled art resolves by code


@pytest.mark.asyncio
async def test_seed_registers_all_new_characters_with_unique_ids(db_session):
    characters = await _seeded(db_session)
    # 4 audited + 14 supplied/exclusive + 1 league trophy (`hero`).
    assert len(characters) == 19
    ids = [character.id for character in characters.values()]
    assert len(ids) == len(set(ids))

    for code, expected in NEW_CARDS.items():
        character = characters[code]
        assert character.power == expected["power"]
        assert character.speed == expected["speed"]
        assert character.intelligence == expected["intelligence"]
        assert character.release_status == ReleaseStatus.RELEASED
        assert character.is_active is True
        assert character.shop_enabled is True
        if code in OFFICIAL_NEW_TEXT:
            affiliations, story, faction_code = OFFICIAL_NEW_TEXT[code]
            assert character.affiliations_fa == affiliations
            assert character.lore_fa == story
            faction = await FactionService.get_for_character(db_session, character)
            assert faction is not None and faction.code == faction_code
        else:
            assert character.faction_id is None
            assert character.affiliations_fa is None and character.lore_fa == ""
        # No role or gameplay mechanic was inferred from the supplied story.
        assert character.faction_role_fa is None
        assert character.passive_key is None and character.hidden_modifiers is None


@pytest.mark.asyncio
async def test_the_four_existing_characters_keep_their_definitions(db_session):
    characters = await _seeded(db_session)
    for code, (power, speed, intelligence) in AUDITED_CARDS.items():
        character = characters[code]
        assert (character.power, character.speed, character.intelligence) == (
            power,
            speed,
            intelligence,
        )
    # Their factions/roles/passives survived the append-only seed change too.
    assert characters["sib_sorkh"].faction_id is not None
    assert characters["sib_sorkh"].passive_key == "first_strike"


# ---------------------------------------------------------------------------
# 2. Artwork: exact original files, no substitution
# ---------------------------------------------------------------------------


def test_every_new_card_uses_its_exact_original_asset():
    from sib.services.character_art import artwork_path

    assets_dir = REPO_ROOT / "assets" / "characters"
    for code in NEW_CODES:
        path = artwork_path(SimpleNamespace(code=code, artwork=None))
        assert path is not None, f"missing art for {code}"
        assert Path(path) == assets_dir / f"{code}.jpg"
        assert Path(path).is_file() and Path(path).stat().st_size > 0


@pytest.mark.asyncio
async def test_art_resolution_returns_the_bundled_file_and_never_a_substitute(db_session):
    """Resolution is by code: no asset key, no fallback to another card."""
    from sib.services.character_art import artwork_path, photo_arg, resolve_character_photo

    characters = await _seeded(db_session)
    seen = set()
    for code in NEW_CODES:
        character = characters[code]
        file_id, fs_path = await resolve_character_photo(db_session, character)
        assert file_id is None  # nothing uploaded can shadow the artwork
        assert Path(fs_path) == REPO_ROOT / "assets" / "characters" / f"{code}.jpg"
        assert photo_arg(file_id, fs_path) is not None
        seen.add(Path(fs_path).name)
    assert len(seen) == len(NEW_CODES)  # one distinct original file per card

    # A card with no bundled art resolves to NOTHING (text-only), it never
    # borrows another character's image.
    assert artwork_path(SimpleNamespace(code="no_such_card", artwork=None)) is None
    file_id, fs_path = await resolve_character_photo(
        db_session, SimpleNamespace(code="no_such_card", artwork=None)
    )
    assert file_id is None and fs_path is None


def test_every_new_card_asset_is_a_loadable_image_with_the_original_pixels():
    """The committed files are used as-is (decodable, same dimensions)."""
    from PIL import Image

    for code in NEW_CODES:
        path = REPO_ROOT / "assets" / "characters" / f"{code}.jpg"
        with Image.open(path) as image:
            image.load()
            assert image.size[0] > 0 and image.size[1] > 0


# ---------------------------------------------------------------------------
# 3./4./5. Stats: exact table, Level-1 total 60, Level-10 total 330
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_cards_have_the_exact_delivered_base_stats(db_session):
    characters = await _seeded(db_session)
    for code, expected in NEW_CARDS.items():
        character = characters[code]
        assert character.stats_dict() == {
            "power": expected["power"],
            "speed": expected["speed"],
            "intelligence": expected["intelligence"],
        }


@pytest.mark.asyncio
async def test_every_new_card_totals_60_at_level_one_and_330_at_level_ten(db_session):
    characters = await _seeded(db_session)
    for code in NEW_CODES:
        character = characters[code]
        base = base_attributes(character)
        assert base.total == 60

        at_one = attributes_at_level(base, 1)
        assert at_one == base

        # Level 10 through the REAL production helper (no new formula).
        at_ten = attributes_at_level(base, MAX_CARD_LEVEL)
        assert at_ten.total == 330
        assert at_ten.power == base.power + 9 * ATTRIBUTE_STEP
        assert at_ten.speed == base.speed + 9 * ATTRIBUTE_STEP
        assert at_ten.intelligence == base.intelligence + 9 * ATTRIBUTE_STEP

        # Every step adds exactly +10 to each attribute (including 9 -> 10).
        for level in range(1, MAX_CARD_LEVEL):
            before = attributes_at_level(base, level)
            after = attributes_at_level(base, level + 1)
            assert (
                after.power - before.power,
                after.speed - before.speed,
                after.intelligence - before.intelligence,
            ) == (ATTRIBUTE_STEP, ATTRIBUTE_STEP, ATTRIBUTE_STEP)


@pytest.mark.asyncio
async def test_owned_cards_of_new_characters_derive_stats_from_their_level(db_session):
    characters = await _seeded(db_session)
    user_id = await _buyer(db_session)
    igris = characters["igris"]
    card = PlayerCard(user_id=user_id, character_id=igris.id, level=1)
    db_session.add(card)
    await db_session.commit()

    assert card_attributes(card, igris).as_dict() == {
        "power": 32, "speed": 17, "intelligence": 11,
    }
    card.level = MAX_CARD_LEVEL
    await db_session.commit()
    assert card_attributes(card, igris).total == 330


# ---------------------------------------------------------------------------
# 6. The 60-point budget is a rule, not configuration
# ---------------------------------------------------------------------------


def test_the_budget_constant_is_60_and_has_no_config_key():
    from sib.services.config_store import CONFIG_DEFAULTS

    assert CARD_BASE_ATTRIBUTE_BUDGET == 60
    assert "card_base_attribute_budget" not in CONFIG_DEFAULTS


@pytest.mark.asyncio
async def test_runtime_config_cannot_change_the_base_budget(db_session):
    from sib.services.config_store import ConfigService

    # Configure a different budget (both a fresh row and a stale one).
    await ConfigService.set(db_session, "card_base_attribute_budget", 120)
    await db_session.commit()

    with pytest.raises(CharacterValidationError):
        await CharacterService.create(
            db_session, code="cfg_budget_120", name_fa="بودجه تنظیم‌شده",
            power=40, speed=40, intelligence=40,   # sums to 120, not 60
        )
    await db_session.rollback()

    await ConfigService.set(db_session, "card_base_attribute_budget", 30)
    await db_session.commit()
    # A 30-point card is refused just the same: 60 is authoritative.
    with pytest.raises(CharacterValidationError):
        await CharacterService.create(
            db_session, code="cfg_budget_30", name_fa="بودجه کوچک",
            power=10, speed=10, intelligence=10,
        )
    await db_session.rollback()

    valid = await CharacterService.create(
        db_session, code="cfg_budget_60", name_fa="بودجه درست",
        power=20, speed=20, intelligence=20,
    )
    await db_session.commit()
    assert valid.total_base_stats == 60

    # The retired key stays inert for every other config consumer.
    assert await ConfigService.get(db_session, "card_upgrade_costs")


@pytest.mark.asyncio
async def test_power_users_cannot_bypass_the_budget_when_editing(db_session):
    character = await CharacterService.create(
        db_session, code="edit_budget", name_fa="ویرایش", power=20, speed=20, intelligence=20
    )
    await db_session.commit()

    with pytest.raises(CharacterValidationError):
        await CharacterService.update(db_session, character.id, power=45, speed=10, intelligence=10)
    await db_session.rollback()

    # A valid redistribution still works (not a freeze of the field).
    await CharacterService.update(db_session, character.id, power=25, speed=20, intelligence=15)
    await db_session.commit()
    assert character.total_base_stats == 60


# ---------------------------------------------------------------------------
# 7. Shop: listing, screens, price, purchase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_new_cards_are_offered_in_the_shop_rotation(db_session):
    characters = await _seeded(db_session)
    entries = await ShopService.list_entries(db_session)
    offered = {character.code for _entry, character, _owned in entries}
    assert set(NEW_CODES) <= offered
    assert set(AUDITED_CARDS) <= offered


@pytest.mark.asyncio
async def test_shop_card_list_shows_every_new_card_for_300_apples(db_session):
    from sib.handlers.shop import SHOP_CARDS_PAGE_SIZE, _cards_view

    characters = await _seeded(db_session)
    user_id = await _buyer(db_session, apples=0)
    entries = await ShopService.list_entries(db_session, user_id=user_id)
    total_pages = (len(entries) + SHOP_CARDS_PAGE_SIZE - 1) // SHOP_CARDS_PAGE_SIZE
    await db_session.commit()  # _cards_view reads through the handler's own session
    assert len(entries) == 17 and total_pages == 4

    all_text = []
    all_labels = []
    for page in range(total_pages):
        text, kb = await _cards_view(user_id, page=page)
        all_text.append(text)
        labels = [button.text for row in kb.inline_keyboard for button in row]
        all_labels.extend(labels)

        card_rows = [
            row for row in kb.inline_keyboard
            if row and row[0].callback_data.startswith(("shop_item:", "shop_owned:"))
        ]
        expected_count = min(SHOP_CARDS_PAGE_SIZE, len(entries) - page * SHOP_CARDS_PAGE_SIZE)
        assert len(card_rows) == expected_count
        # Only the last page may be partial; every other page is full.
        assert expected_count == SHOP_CARDS_PAGE_SIZE or page + 1 == total_pages
        assert all(len(row) == 1 for row in card_rows)  # one vertical column
        visible_entry_ids = [int(row[0].callback_data.split(":")[1]) for row in card_rows]
        expected_entry_ids = [entry.id for entry, _character, _owned in entries][
            page * SHOP_CARDS_PAGE_SIZE : (page + 1) * SHOP_CARDS_PAGE_SIZE
        ]
        assert visible_entry_ids == expected_entry_ids
        assert f"📖 صفحه {page + 1} از {total_pages}" in text
        assert text.count(f"{SHOP_CARD_PRICE_APPLES} سیب") == expected_count

        previous = [button for button in kb.inline_keyboard[-3] if button.text == "◀️ قبلی"]
        following = [button for button in kb.inline_keyboard[-3] if button.text == "▶️ بعدی"]
        assert bool(previous) == (page > 0)
        assert bool(following) == (page + 1 < total_pages)
        if previous:
            assert previous[0].callback_data == f"shop_cards:{page - 1}"
        if following:
            assert following[0].callback_data == f"shop_cards:{page + 1}"
        assert kb.inline_keyboard[-2][0].callback_data == f"shop_cards:{page}"
        assert kb.inline_keyboard[-1][0].callback_data == "shop_refresh"

    joined_text = "\n".join(all_text)
    for code in NEW_CODES:
        name = characters[code].name_fa
        assert joined_text.count(name) == 1
        assert any(name in label and f"({SHOP_CARD_PRICE_APPLES} 🍎)" in label for label in all_labels), code
    # Across pages the unified price appears once per purchasable card.
    assert joined_text.count(f"{SHOP_CARD_PRICE_APPLES} سیب") == len(entries)


@pytest.mark.asyncio
async def test_new_card_detail_screen_shows_its_art_and_the_300_price(db_session):
    from sib.handlers.shop import _shop_item_view

    characters = await _seeded(db_session)
    user_id = await _buyer(db_session)
    entries = await ShopService.list_entries(db_session, user_id=user_id)
    entry, character, _owned = next(
        item for item in entries if item[1].code == "moon_lily"
    )
    entry_id = entry.id
    await db_session.commit()  # the handler renders through its own session

    text, kb, art_photo = await _shop_item_view(user_id, entry_id)
    assert character.name_fa in text
    assert f"💰 قیمت: {SHOP_CARD_PRICE_APPLES} سیب" in text
    assert art_photo is not None  # the bundled original art
    callbacks = [button.callback_data for row in kb.inline_keyboard for button in row]
    assert any(callback.startswith(f"shop_buy:{entry_id}:") for callback in callbacks)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", NEW_CODES)
async def test_every_new_card_is_purchasable_for_exactly_300_apples(db_session, code):
    characters = await _seeded(db_session)
    user_id = await _buyer(db_session, apples=1_000)
    entries = await ShopService.list_entries(db_session, user_id=user_id)
    entry, character, _owned = next(item for item in entries if item[1].code == code)

    outcome = await ShopService.purchase(
        db_session, user_id, entry.id, nonce=f"newcard{code[:6]:0<12}"
    )
    await db_session.commit()

    assert outcome.replayed is False
    assert outcome.price == SHOP_CARD_PRICE_APPLES
    assert await AppleWalletService.get_balance(db_session, user_id) == 1_000 - 300
    owned = await db_session.scalar(
        select(PlayerCard.id).where(
            PlayerCard.user_id == user_id, PlayerCard.character_id == character.id
        )
    )
    assert owned is not None

    ledger = (
        await db_session.scalars(
            select(AppleTransaction).where(
                AppleTransaction.user_id == user_id,
                AppleTransaction.reason == "shop_purchase",
            )
        )
    ).all()
    assert [row.amount for row in ledger] == [-300]
    assert ledger[0].ref_id == entry.id


@pytest.mark.asyncio
async def test_purchase_protections_still_hold_for_a_new_card(db_session):
    """Insufficient balance, duplicate ownership and nonce replay."""
    characters = await _seeded(db_session)
    entry, character, _owned = next(
        item
        for item in await ShopService.list_entries(db_session)
        if item[1].code == "green_knight"
    )
    entry_id, character_id = entry.id, character.id  # plain ints (rollback-safe)
    poor_user = await _buyer(db_session, apples=299)

    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, poor_user, entry_id, nonce="newcardpoor01")
    await db_session.rollback()
    assert await AppleWalletService.get_balance(db_session, poor_user) == 299

    rich_user = await _buyer(db_session, apples=1_000)
    first = await ShopService.purchase(db_session, rich_user, entry_id, nonce="newcardrich01")
    await db_session.commit()
    assert first.price == 300

    with pytest.raises(ShopError):  # already owned: never charged twice
        await ShopService.purchase(db_session, rich_user, entry_id, nonce="newcardrich02")
    await db_session.rollback()

    replay = await ShopService.purchase(db_session, rich_user, entry_id, nonce="newcardrich01")
    assert replay.replayed is True
    assert await AppleWalletService.get_balance(db_session, rich_user) == 700
    cards = await db_session.scalars(
        select(PlayerCard.id).where(
            PlayerCard.user_id == rich_user, PlayerCard.character_id == character_id
        )
    )
    assert len(list(cards.all())) == 1


# ---------------------------------------------------------------------------
# 8. Every listing surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wiki_lists_and_renders_every_new_card(db_session):
    characters = await _seeded(db_session)
    listed = {character.code for character in await WikiService.list_wiki(db_session)}
    assert set(NEW_CODES) <= listed

    for code in NEW_CODES:
        character = characters[code]
        page = await WikiService.wiki_page(db_session, character.id)
        assert page is not None
        assert character.name_fa in page
        assert str(character.power) in page

    # The index paginates the (now 17-card) roster: every character is
    # reachable through some page's buttons, and none is dropped.
    from sib.handlers.wiki import WIKI_PAGE_SIZE, _wiki_index_lines, _wiki_keyboard

    all_characters = await WikiService.list_wiki(db_session)
    page_count = (len(all_characters) + WIKI_PAGE_SIZE - 1) // WIKI_PAGE_SIZE
    callbacks = []
    lines: list[str] = []
    for page in range(page_count):
        callbacks += [
            button.callback_data
            for row in _wiki_keyboard(all_characters, page).inline_keyboard
            for button in row
        ]
        lines += _wiki_index_lines(all_characters)
    for character in all_characters:
        assert f"wiki_char:{character.id}" in callbacks
        assert character.name_fa in "\n".join(lines)


@pytest.mark.asyncio
async def test_wiki_card_screen_sends_the_exact_original_art_file(db_session):
    """Drive the real wiki callback with a fake Telegram: the photo that is
    actually sent must be the card's own bundled original file."""
    from aiogram.types import CallbackQuery, FSInputFile
    from unittest.mock import AsyncMock, MagicMock

    from sib.handlers.wiki import handle_wiki_char
    from sib.tests.test_screen_transitions import FakeBot, _photo_message

    characters = await _seeded(db_session)
    target = characters["zarush"]
    bot = FakeBot()
    message = _photo_message(bot)
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.data = f"wiki_char:{target.id}"
    callback.from_user = MagicMock(id=7, is_bot=False, first_name="کاربر")
    callback.answer = AsyncMock()

    await handle_wiki_char(callback, bot)

    media = bot.live()[0].photo
    assert isinstance(media, FSInputFile)
    assert Path(media.path) == REPO_ROOT / "assets" / "characters" / "zarush.jpg"
    assert target.name_fa in bot.live()[0].caption


@pytest.mark.asyncio
async def test_collection_lists_new_cards_once_owned(db_session):
    from sib.services.player_cards import PlayerCardService

    characters = await _seeded(db_session)
    user_id = await _buyer(db_session, apples=1_000)
    entries = await ShopService.list_entries(db_session, user_id=user_id)
    entry, character, _owned = next(item for item in entries if item[1].code == "wizard")
    await ShopService.purchase(db_session, user_id, entry.id, nonce="newcardsurf01")
    await db_session.commit()

    cards = await PlayerCardService.list_cards(db_session, user_id)
    assert [card.character_id for card in cards] == [character.id]

    listing, total_pages = await CollectionService.render_collection(db_session, user_id)
    assert total_pages >= 1
    assert characters["wizard"].name_fa in listing

    detail, _main_card_id = await CollectionService.render_card_detail_with_main_card(
        db_session, user_id, cards[0].id
    )
    assert characters["wizard"].name_fa in detail
    assert "💪 24" in detail or "`24`" in detail  # derived Level-1 Power

    # The owned-card detail screen carries the card's own original artwork.
    from aiogram.types import FSInputFile

    from sib.handlers.cards import _card_view

    card_id = cards[0].id
    await db_session.commit()
    text, _kb, photo = await _card_view(user_id, card_id)
    assert characters["wizard"].name_fa in text
    assert isinstance(photo, FSInputFile)
    assert Path(photo.path) == REPO_ROOT / "assets" / "characters" / "wizard.jpg"


# ---------------------------------------------------------------------------
# 9. Seed safety: idempotent, non-destructive, loud on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_never_touches_player_data(db_session):
    from sib.database.connection import seed_characters

    characters = await _seeded(db_session)
    user_id = await _buyer(db_session, apples=1_000)
    igris = characters["igris"]
    entry, character, _owned = next(
        item for item in await ShopService.list_entries(db_session) if item[1].code == "igris"
    )
    await ShopService.purchase(db_session, user_id, entry.id, nonce="newcardseed01")
    await db_session.commit()

    card = await db_session.scalar(
        select(PlayerCard).where(
            PlayerCard.user_id == user_id, PlayerCard.character_id == igris.id
        )
    )
    card.level = MAX_CARD_LEVEL
    await db_session.commit()
    balance = await AppleWalletService.get_balance(db_session, user_id)
    row_count = len((await db_session.scalars(select(Character))).all())

    # Official Igris content is restored; ownership/progression must not reset.
    igris.name_fa = "ایگریس (دست‌کاری‌شده)"
    await db_session.commit()

    assert await seed_characters(db_session) == 0  # nothing new, no duplicates
    await db_session.commit()

    assert len((await db_session.scalars(select(Character))).all()) == row_count
    refreshed = await db_session.get(Character, igris.id)
    assert refreshed.name_fa == "ایگریس"  # the newly supplied official name
    assert refreshed.lore_fa == IGRIS_STORY
    assert refreshed.affiliations_fa == IGRIS_AFFILIATIONS
    assert card.level == MAX_CARD_LEVEL  # player progression untouched
    assert card.character_id == igris.id
    assert await AppleWalletService.get_balance(db_session, user_id) == balance


@pytest.mark.asyncio
async def test_a_broken_seed_entry_fails_loudly_instead_of_losing_a_card(db_session, monkeypatch):
    from sib.database.connection import CharacterSeedError, seed_characters

    payload = _seed_entries()
    payload.append({  # a card whose stats do not satisfy the 60 budget
        "code": "broken_card",
        "name_fa": "کارت خراب",
        "power": 1,
        "speed": 1,
        "intelligence": 1,
        "release_status": "RELEASED",
    })
    original_read_text = Path.read_text
    payload_json = json.dumps(payload, ensure_ascii=False)

    def fake_read_text(self, encoding=None):
        if self.name == "characters_seed.json":
            return payload_json
        return original_read_text(self, encoding=encoding)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    with pytest.raises(CharacterSeedError) as err:
        await seed_characters(db_session)
    await db_session.rollback()

    assert "broken_card" in str(err.value)
    assert "broken_card" not in {
        row.code for row in (await db_session.scalars(select(Character))).all()
    }


@pytest.mark.asyncio
async def test_new_cards_enter_an_existing_installations_next_shop_rotation(db_session, monkeypatch):
    """Rotation semantics are unchanged: a rotation opened BEFORE the new
    cards existed keeps its historical entries, and the NEXT rotation
    offers all 15 cards, so an existing install picks them up at its
    normal weekly rollover (no migration, no rotation rewrite)."""
    from datetime import datetime, timedelta, timezone

    from sib.database.connection import seed_characters
    from sib.database.models import ShopRotation

    # --- Install that has not yet received the new cards -------------------
    legacy_payload = json.dumps(
        [entry for entry in _seed_entries() if entry["code"] in AUDITED_CARDS],
        ensure_ascii=False,
    )
    original_read_text = Path.read_text

    def legacy_read_text(self, encoding=None):
        if self.name == "characters_seed.json":
            return legacy_payload
        return original_read_text(self, encoding=encoding)

    monkeypatch.setattr(Path, "read_text", legacy_read_text)
    await seed_characters(db_session)
    await db_session.commit()

    old_rotation = await ShopService.current_rotation(db_session)
    await db_session.commit()
    old_rotation_id = old_rotation.id
    offered_before = {
        character.code for _e, character, _o in await ShopService.list_entries(db_session)
    }
    assert offered_before == set(AUDITED_CARDS)

    # --- The new cards ship and the bot restarts ---------------------------
    monkeypatch.undo()
    await seed_characters(db_session)
    await db_session.commit()
    await ShopService.current_rotation(db_session)  # same, still-open window
    await db_session.commit()
    assert {
        character.code for _e, character, _o in await ShopService.list_entries(db_session)
    } == set(AUDITED_CARDS)  # the historical rotation is immutable

    # --- Weekly rollover: every card is offered, the new ones included -----
    rotation = await db_session.get(ShopRotation, old_rotation_id)
    rotation.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    await ShopService.current_rotation(db_session)
    await db_session.commit()

    offered_after = {
        character.code for _e, character, _o in await ShopService.list_entries(db_session)
    }
    assert offered_after == set(AUDITED_CARDS) | set(NEW_CODES)


@pytest.mark.asyncio
async def test_new_cards_appear_after_a_restart_on_an_existing_installation(db_session):
    """The append-only sync: an install that already has the four audited
    cards picks up every supplied new one on the next start (no migration)."""
    from sib.database.connection import seed_characters

    for code, (power, speed, intelligence) in AUDITED_CARDS.items():
        await CharacterService.create(
            db_session, code=code, name_fa=f"کارت {code}", power=power,
            speed=speed, intelligence=intelligence,
        )
    await db_session.commit()

    created = await seed_characters(db_session)
    await db_session.commit()

    assert created == len(NEW_CODES) + len(EXCLUSIVE_CARDS)
    codes = {row.code for row in (await db_session.scalars(select(Character))).all()}
    assert codes == set(AUDITED_CARDS) | set(NEW_CODES) | set(EXCLUSIVE_CARDS)
