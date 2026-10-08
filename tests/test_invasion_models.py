"""Persistence tests for the independent Invasion domain."""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from sib.database.models import (
    Invasion,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
    InvasionWave,
)
from sib.services.card_progression import card_attributes
from sib.services.characters import CharacterService
from sib.services.player_cards import PlayerCardService
from sib.tests.test_parties import create_test_group, create_test_user


UTC = timezone.utc
FIXED_TIME = datetime(2026, 10, 7, 12, 30, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamp result for timezone-aware assertions."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _new_invasion(group, **overrides) -> Invasion:
    values = {
        "occurrence_key": f"invasion-{uuid.uuid4().hex}",
        "status": InvasionStatus.SCHEDULED,
        "scheduled_at": FIXED_TIME,
        "enemy_faction_code": "moon_descendants",
        "enemy_power": 420,
        "event_config_json": json.dumps(
            {"difficulty": {"label": "test"}}, ensure_ascii=False
        ),
        "event_spec_json": json.dumps({"seed": "test-seed"}),
    }
    values.update(overrides)
    return Invasion(group=group, **values)


def _enable_sqlite_foreign_keys(session) -> None:
    """Enable FK enforcement only in this test's SQLite engine connections."""
    engine = session.bind
    if engine.dialect.name != "sqlite":
        return

    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    event.listen(engine.sync_engine, "connect", enable_foreign_keys)


@pytest.mark.asyncio
async def test_invasion_persists_configuration_lifecycle_and_group(db_session):
    group = await create_test_group(db_session, "Invasion persistence group")
    scheduled_at = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
    started_at = datetime(2026, 10, 8, 8, 5, tzinfo=UTC)
    next_transition_at = datetime(2026, 10, 8, 8, 15, tzinfo=UTC)
    event_config = {"difficulty": {"tier": "fixture", "modifiers": ["test"]}}
    event_spec = {"seed": 12345, "spec": {"version": 1}}

    invasion = _new_invasion(
        group,
        occurrence_key="invasion-persistence-test",
        status=InvasionStatus.ACTIVE,
        scheduled_at=scheduled_at,
        started_at=started_at,
        next_transition_at=next_transition_at,
        current_wave_sequence=2,
        announcement_message_id=9001,
        target_region_code="region-fixture",
        target_region_name_snapshot="Fixture region",
        enemy_faction_code="moon_descendants",
        enemy_power=730,
        event_config_json=json.dumps(event_config),
        event_spec_json=json.dumps(event_spec),
    )
    db_session.add(invasion)
    await db_session.commit()
    await db_session.refresh(invasion)

    assert invasion.group is group
    assert invasion.group_id == group.id
    assert invasion.status == InvasionStatus.ACTIVE
    assert _as_utc(invasion.scheduled_at) == scheduled_at
    assert _as_utc(invasion.started_at) == started_at
    assert _as_utc(invasion.next_transition_at) == next_transition_at
    assert invasion.ended_at is None
    assert invasion.current_wave_sequence == 2
    assert invasion.announcement_message_id == 9001
    assert invasion.target_region_code == "region-fixture"
    assert invasion.target_region_name_snapshot == "Fixture region"
    assert invasion.enemy_faction_code == "moon_descendants"
    assert invasion.enemy_power == 730
    assert json.loads(invasion.event_config_json) == event_config
    assert json.loads(invasion.event_spec_json) == event_spec
    assert invasion.created_at is not None
    assert invasion.updated_at is not None


@pytest.mark.asyncio
async def test_invasion_occurrence_key_is_unique(db_session):
    group = await create_test_group(db_session, "Invasion occurrence group")
    db_session.add(_new_invasion(group, occurrence_key="same-occurrence"))
    await db_session.commit()

    db_session.add(_new_invasion(group, occurrence_key="same-occurrence"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", InvasionOutcome.ALL)
async def test_invasion_persists_each_approved_outcome(db_session, outcome):
    group = await create_test_group(db_session, "Invasion outcome group")
    invasion = _new_invasion(
        group,
        status=InvasionStatus.FINISHED,
        ended_at=FIXED_TIME,
        outcome=outcome,
    )
    db_session.add(invasion)
    await db_session.commit()
    await db_session.refresh(invasion)

    assert invasion.status == InvasionStatus.FINISHED
    assert _as_utc(invasion.ended_at) == FIXED_TIME
    assert invasion.outcome == outcome


@pytest.mark.asyncio
async def test_invasion_rejects_unapproved_status_and_outcome(db_session):
    group = await create_test_group(db_session, "Invasion constraint group")
    db_session.add(_new_invasion(group, outcome="VICTORY"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    db_session.add(_new_invasion(group, status="CANCELLED"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_wave_order_parent_configuration_and_sequence_uniqueness(db_session):
    group = await create_test_group(db_session, "Invasion wave group")
    invasion = _new_invasion(group)
    db_session.add(invasion)
    await db_session.flush()

    wave_two = InvasionWave(
        invasion=invasion,
        wave_sequence=2,
        status=InvasionStatus.SCHEDULED,
        enemy_composition_json=json.dumps({"enemies": [{"code": "second"}]}),
    )
    wave_one = InvasionWave(
        invasion=invasion,
        wave_sequence=1,
        status=InvasionStatus.ACTIVE,
        started_at=FIXED_TIME,
        deadline_at=datetime(2026, 10, 7, 12, 45, tzinfo=UTC),
        next_transition_at=datetime(2026, 10, 7, 12, 40, tzinfo=UTC),
        enemy_composition_json=json.dumps({"enemies": [{"code": "first"}]}),
    )
    db_session.add_all([wave_two, wave_one])
    await db_session.commit()
    await db_session.refresh(invasion, attribute_names=["waves"])

    assert [wave.wave_sequence for wave in invasion.waves] == [1, 2]
    assert wave_one.invasion is invasion
    assert wave_two.invasion is invasion
    assert wave_one.status == InvasionStatus.ACTIVE
    assert _as_utc(wave_one.started_at) == FIXED_TIME
    assert _as_utc(wave_one.deadline_at) == datetime(
        2026, 10, 7, 12, 45, tzinfo=UTC
    )
    assert _as_utc(wave_one.next_transition_at) == datetime(
        2026, 10, 7, 12, 40, tzinfo=UTC
    )
    assert json.loads(wave_one.enemy_composition_json) == {
        "enemies": [{"code": "first"}]
    }
    assert wave_one.contribution_total == 0
    assert wave_one.created_at is not None
    assert wave_one.updated_at is not None

    db_session.add(
        InvasionWave(
            invasion_id=invasion.id,
            wave_sequence=1,
            enemy_composition_json="{}",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_participant_without_main_card_is_allowed_and_user_is_unique(db_session):
    group = await create_test_group(db_session, "Invasion participant group")
    user = await create_test_user(db_session, "Invasion participant")
    invasion = _new_invasion(group)
    db_session.add(invasion)
    await db_session.flush()

    assert await PlayerCardService.get_main_card(db_session, user.id) is None
    participant = InvasionParticipant(
        invasion=invasion,
        user=user,
        joined_at=FIXED_TIME,
    )
    db_session.add(participant)
    await db_session.commit()
    await db_session.refresh(participant)

    assert participant.invasion is invasion
    assert participant.user is user
    assert participant.main_card_id_snapshot is None
    assert participant.character_id_snapshot is None
    assert _as_utc(participant.joined_at) == FIXED_TIME
    assert participant.contribution_total == 0
    assert participant.created_at is not None
    assert participant.updated_at is not None

    db_session.add(
        InvasionParticipant(invasion_id=invasion.id, user_id=user.id)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_main_card_snapshot_is_complete_and_stable_after_live_changes(db_session):
    group = await create_test_group(db_session, "Invasion snapshot group")
    user = await create_test_user(db_session, "Invasion snapshot owner")
    other_user = await create_test_user(db_session, "Later card owner")
    invasion = _new_invasion(group)
    db_session.add(invasion)
    await db_session.commit()

    character = await CharacterService.create(
        db_session,
        code=f"invasion_snapshot_{uuid.uuid4().hex[:8]}",
        name_fa="نام اولیه",
        name_en="Original name",
        artwork="characters/original.png",
        power=22,
        speed=20,
        intelligence=18,
    )
    alternate_character = await CharacterService.create(
        db_session,
        code=f"invasion_alternate_{uuid.uuid4().hex[:8]}",
        name_fa="نام جایگزین",
        power=20,
        speed=20,
        intelligence=20,
    )
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    alternate_card = await PlayerCardService.acquire(
        db_session, user.id, alternate_character.id
    )
    card.level = 3
    await db_session.commit()
    await PlayerCardService.set_favorite(
        db_session, user.id, card.id, expected_main_card_id=0
    )
    await db_session.commit()

    main_card = await PlayerCardService.get_main_card(db_session, user.id)
    assert main_card is not None and main_card.id == card.id
    attributes = card_attributes(main_card, character)
    participant = InvasionParticipant(
        invasion=invasion,
        user=user,
        main_card_id_snapshot=main_card.id,
        character_id_snapshot=character.id,
        character_code_snapshot=character.code,
        character_name_fa_snapshot=character.name_fa,
        character_name_en_snapshot=character.name_en,
        character_artwork_snapshot=character.artwork,
        card_level_snapshot=main_card.level,
        power_snapshot=attributes.power,
        speed_snapshot=attributes.speed,
        intelligence_snapshot=attributes.intelligence,
        character_balance_version_snapshot=character.balance_version,
    )
    db_session.add(participant)
    await db_session.commit()
    await db_session.refresh(participant)

    snapshot_fields = (
        "main_card_id_snapshot",
        "character_id_snapshot",
        "character_code_snapshot",
        "character_name_fa_snapshot",
        "character_name_en_snapshot",
        "character_artwork_snapshot",
        "card_level_snapshot",
        "power_snapshot",
        "speed_snapshot",
        "intelligence_snapshot",
        "character_balance_version_snapshot",
    )
    persisted_snapshot = {
        name: getattr(participant, name) for name in snapshot_fields
    }
    assert persisted_snapshot == {
        "main_card_id_snapshot": card.id,
        "character_id_snapshot": character.id,
        "character_code_snapshot": character.code,
        "character_name_fa_snapshot": "نام اولیه",
        "character_name_en_snapshot": "Original name",
        "character_artwork_snapshot": "characters/original.png",
        "card_level_snapshot": 3,
        "power_snapshot": attributes.power,
        "speed_snapshot": attributes.speed,
        "intelligence_snapshot": attributes.intelligence,
        "character_balance_version_snapshot": 1,
    }
    assert (attributes.power, attributes.speed, attributes.intelligence) == (42, 40, 38)

    # Change the selected Main Card, transfer and upgrade its former card, and
    # revise the source Character balance and display identity after joining.
    await PlayerCardService.set_favorite(
        db_session,
        user.id,
        alternate_card.id,
        expected_main_card_id=card.id,
    )
    card.level = 5
    card.user_id = other_user.id
    await CharacterService.update(
        db_session,
        character.id,
        name_fa="نام به‌روزشده",
        name_en="Updated name",
        artwork="characters/updated.png",
        power=25,
        speed=20,
        intelligence=15,
    )
    await db_session.commit()

    assert (await PlayerCardService.get_main_card(db_session, user.id)).id == (
        alternate_card.id
    )
    assert character.balance_version == 2
    assert card.level == 5 and card.user_id == other_user.id
    await db_session.refresh(participant)
    assert {
        name: getattr(participant, name) for name in snapshot_fields
    } == persisted_snapshot


@pytest.mark.asyncio
async def test_invasion_group_foreign_key_rejects_missing_group(db_session):
    _enable_sqlite_foreign_keys(db_session)
    invasion = Invasion(
        group_id=-987654321,
        occurrence_key=f"missing-group-{uuid.uuid4().hex}",
        scheduled_at=FIXED_TIME,
        enemy_faction_code="moon_descendants",
        enemy_power=100,
        event_config_json="{}",
    )
    db_session.add(invasion)

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_group_and_user_foreign_keys_cascade_deletes(db_session):
    _enable_sqlite_foreign_keys(db_session)
    group = await create_test_group(db_session, "Invasion cascade group")
    user_one = await create_test_user(db_session, "Invasion cascade one")
    user_two = await create_test_user(db_session, "Invasion cascade two")
    invasion = _new_invasion(group)
    wave = InvasionWave(
        invasion=invasion,
        wave_sequence=1,
        enemy_composition_json="{}",
    )
    participant_one = InvasionParticipant(invasion=invasion, user=user_one)
    participant_two = InvasionParticipant(invasion=invasion, user=user_two)
    db_session.add_all([invasion, wave, participant_one, participant_two])
    await db_session.commit()

    assert invasion.group is group
    assert wave.invasion is invasion
    assert participant_one.invasion is invasion
    assert participant_one.user is user_one

    await db_session.delete(user_one)
    await db_session.commit()
    participant_count = await db_session.scalar(
        select(func.count()).select_from(InvasionParticipant)
    )
    assert participant_count == 1

    await db_session.delete(group)
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(Invasion)) == 0
    assert await db_session.scalar(select(func.count()).select_from(InvasionWave)) == 0
    assert (
        await db_session.scalar(
            select(func.count()).select_from(InvasionParticipant)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_deleting_invasion_cascades_to_waves_and_participants(db_session):
    group = await create_test_group(db_session, "Invasion parent cascade group")
    user = await create_test_user(db_session, "Invasion parent cascade user")
    invasion = _new_invasion(group)
    wave = InvasionWave(
        invasion=invasion,
        wave_sequence=1,
        enemy_composition_json="{}",
    )
    participant = InvasionParticipant(invasion=invasion, user=user)
    db_session.add_all([invasion, wave, participant])
    await db_session.commit()

    await db_session.delete(invasion)
    await db_session.commit()

    assert await db_session.scalar(select(func.count()).select_from(InvasionWave)) == 0
    assert (
        await db_session.scalar(
            select(func.count()).select_from(InvasionParticipant)
        )
        == 0
    )
