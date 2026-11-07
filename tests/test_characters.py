"""Tests for the character data system (Phase 9).

Characters are data, not code: creation, edits, release lifecycle and
balance versioning all work through the service/data layer.
"""

import json

import pytest

from sib.database.models import (
    SHOP_CARD_PRICE_APPLES,
    Character,
    CharacterBalanceVersion,
    ReleaseStatus,
)
from sib.services.characters import (
    CharacterService,
    CharacterValidationError,
    render_character_card,
)
from sqlalchemy import select


@pytest.mark.asyncio
async def test_create_character_enforces_budget(db_session):
    # Sum must equal the level-1 budget (60).
    with pytest.raises(CharacterValidationError):
        await CharacterService.create(
            db_session,
            code="bad_budget",
            name_fa="شخصیت بد",
            power=30,
            speed=30,
            intelligence=30,
        )

    character = await CharacterService.create(
        db_session,
        code="good_budget",
        name_fa="شخصیت خوب",
        power=20,
        speed=20,
        intelligence=20,
    )
    await db_session.commit()

    assert character.total_base_stats == 60
    assert character.balance_version == 1
    assert character.release_status == ReleaseStatus.DRAFT


@pytest.mark.asyncio
async def test_duplicate_code_rejected(db_session):
    await CharacterService.create(
        db_session, code="dup", name_fa="الف", power=20, speed=20, intelligence=20
    )
    with pytest.raises(CharacterValidationError):
        await CharacterService.create(
            db_session, code="dup", name_fa="ب", power=20, speed=20, intelligence=20
        )


@pytest.mark.asyncio
async def test_stat_edit_writes_balance_snapshot(db_session):
    character = await CharacterService.create(
        db_session, code="bal", name_fa="بالانس", power=20, speed=20, intelligence=20
    )
    await db_session.commit()

    await CharacterService.update(
        db_session, character.id, power=25, speed=15, intelligence=20
    )
    await db_session.commit()

    assert character.balance_version == 2
    versions = await CharacterService.get_versions(db_session, character.id)
    assert [v.version for v in versions] == [2, 1]
    snap = json.loads(versions[0].snapshot)
    assert snap["power"] == 25
    assert snap["balance_version"] == 2

    # Non-balance edits do not bump the version.
    await CharacterService.update(db_session, character.id, lore_fa="داستان جدید")
    await db_session.commit()
    assert character.balance_version == 2


@pytest.mark.asyncio
async def test_release_lifecycle(db_session):
    character = await CharacterService.create(
        db_session, code="life", name_fa="چرخه", power=20, speed=20, intelligence=20
    )
    await db_session.commit()

    # Not visible in the released list yet.
    released = await CharacterService.list_released(db_session)
    assert all(c.id != character.id for c in released)

    await CharacterService.set_release_status(db_session, character.id, ReleaseStatus.RELEASED)
    await db_session.commit()
    released = await CharacterService.list_released(db_session)
    assert any(c.id == character.id for c in released)
    assert character.released_at is not None

    await CharacterService.deactivate(db_session, character.id)
    await db_session.commit()
    released = await CharacterService.list_released(db_session)
    assert all(c.id != character.id for c in released)

    await CharacterService.reactivate(db_session, character.id)
    await db_session.commit()
    released = await CharacterService.list_released(db_session)
    assert any(c.id == character.id for c in released)


@pytest.mark.asyncio
async def test_characters_seed_from_data_layer(db_session):
    """The starter roster loads from JSON without any code changes."""
    from sib.database.connection import seed_characters

    created = await seed_characters(db_session)
    await db_session.commit()
    assert created >= 4

    # Second run must not duplicate.
    created_again = await seed_characters(db_session)
    await db_session.commit()
    assert created_again == 0

    released = await CharacterService.list_released(db_session)
    assert len(released) >= 4
    # Budget rule holds for every seeded character.
    for c in released:
        assert c.total_base_stats == 60

    # At least one passive + strengths/weaknesses are data-driven.
    golden = await CharacterService.get_by_code(db_session, "sib_talayi")
    assert golden is not None
    assert golden.passive_key == "golden_hour"
    assert golden.passive_params.get("speed_boost") == 0.12
    assert golden.strengths_list


@pytest.mark.asyncio
async def test_render_card_is_persian_and_complete(db_session):
    character = await CharacterService.create(
        db_session,
        code="render",
        name_fa="قهرمان داستان",
        power=22,
        speed=20,
        intelligence=18,
        lore_fa="یک داستان کوتاه.",
        passive_key="first_strike",
        strengths=["سرعت"],
        weaknesses=["قدرت"],
    )
    await db_session.commit()

    card = render_character_card(character)
    assert "قهرمان داستان" in card
    assert "💪" in card and "⚡" in card and "🧠" in card
    # Unimplemented raw passive keys must not be presented as active mechanics.
    assert "first_strike" not in card
    assert "توانایی منفعل" not in card
    assert str(SHOP_CARD_PRICE_APPLES) in card
    assert "یک داستان کوتاه." in card
    # Rarity is no longer an active classification: it must not leak into
    # the presentation even when the (deprecated) column holds a value.
    for tier in ("کمیاب", "حماسی", "افسانه‌ای", "معمولی"):
        assert tier not in card


@pytest.mark.asyncio
async def test_rarity_defaults_to_common_and_updates_persist(db_session):
    character = await CharacterService.create(
        db_session, code="rarity_card", name_fa="کارت", power=20, speed=20, intelligence=20
    )
    assert character.rarity == "COMMON"
    await CharacterService.update(db_session, character.id, rarity="LEGENDARY")
    await db_session.commit()
    character_id = character.id
    db_session.expunge_all()
    reloaded = await db_session.get(Character, character_id)
    assert reloaded.rarity == "LEGENDARY"
    assert "💰 قیمت: 15000 سیب" in render_character_card(reloaded)


@pytest.mark.parametrize("rarity", ["MYTHIC", "rare", "", None])
async def test_invalid_rarity_rejected_on_create(db_session, rarity):
    with pytest.raises(CharacterValidationError):
        await CharacterService.create(
            db_session, code="bad_rarity", name_fa="کارت", power=20, speed=20, intelligence=20,
            rarity=rarity,
        )


@pytest.mark.asyncio
async def test_invalid_release_status_rejected(db_session):
    character = await CharacterService.create(
        db_session, code="ok_status", name_fa="سالم", power=20, speed=20, intelligence=20
    )
    await db_session.commit()
    with pytest.raises(CharacterValidationError):
        await CharacterService.set_release_status(db_session, character.id, "UNKNOWN")
