"""Transactional domain operations for persistent Invasions.

This module has no Telegram, scheduler, or reward dependencies. The private
``event_spec_json`` shape is intentionally small: ``{"waves": [composition, ...]}``
plus optional caller metadata. All wave rows are materialized at creation, so
advancing an existing event never regenerates its enemy configuration from
newer application defaults.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import (
    Character,
    Group,
    Invasion,
    InvasionContribution,
    InvasionDefenseAction,
    InvasionOutcome,
    InvasionParticipant,
    InvasionStatus,
    InvasionWave,
    PlayerCard,
    User,
)
from sib.services.card_progression import card_attributes
from sib.services.player_cards import PlayerCardService

MAX_SIGNED_BIGINT = (1 << 63) - 1
MAX_SIGNED_INTEGER = (1 << 31) - 1
_UTC = timezone.utc

# Four-wave battles (schema v2). A battle's clock starts at its persisted
# activation time; every later wave boundary is derived from the previous
# boundary, so wave timing is deterministic regardless of tick latency.
BATTLE_SCHEMA_VERSION = 2
BATTLE_TIMING_BASIS = "activation"
# Legacy Phase-5 events were always two waves. Enemy power scales with
# wave_count / LEGACY_WAVE_COUNT so each wave keeps its legacy strength.
LEGACY_WAVE_COUNT = 2
# A heartbeat gap up to this many seconds is ordinary scheduling jitter and is
# never compensated. Anything longer is treated as downtime, and only the part
# above this tolerance is restored, so a normal 5-second tick can never extend
# a battle. Each genuine stall therefore loses at most this many seconds.
HEARTBEAT_TOLERANCE_SECONDS = 30
# Default total downtime compensation allowed for one four-wave battle. The
# live value is snapshotted per battle at activation (``downtime_compensation_
# max_seconds``); this default covers battles without that field.
DEFAULT_DOWNTIME_COMPENSATION_MAX_SECONDS = 20 * 60


def outcome_for(defense_total: int, enemy_power: int) -> str:
    """Approved threshold rule shared by legacy and four-wave resolution.

    Integer comparisons only: TOTAL_VICTORY when defense >= enemy power,
    NARROW_DEFENSE when defense is at least 75% of enemy power, else DEFEAT.
    """
    if defense_total >= enemy_power:
        return InvasionOutcome.TOTAL_VICTORY
    if defense_total * 100 >= enemy_power * 75:
        return InvasionOutcome.NARROW_DEFENSE
    return InvasionOutcome.DEFEAT


class InvasionDefenseActionCode:
    """Stable V1 action codes accepted by the domain action API."""

    BRACE = "brace"
    ALL = (BRACE,)


# Provisional Phase 7 V1 tuning: one point for taking a defensive action plus
# one point per ten persisted Main Card power. No-card participants use power 0.
# This does not change the existing event power or outcome thresholds.
BRACE_BASE_CONTRIBUTION = 1
BRACE_POWER_DIVISOR = 10
INVASION_DEFENSE_ACTION_VERSION = 1


class InvasionError(ValueError):
    """Base class for deterministic Invasion domain errors."""


class InvasionNotFoundError(InvasionError):
    """The requested Invasion does not exist."""


class InvasionGroupNotFoundError(InvasionError):
    """Creation referenced a Group that does not exist."""


class InvasionGroupMismatchError(InvasionError):
    """The Invasion belongs to a different Group context."""


class InvasionOccurrenceConflictError(InvasionError):
    """An occurrence key already identifies a different configuration."""


class InvasionConfigurationError(InvasionError):
    """A required persisted configuration value is invalid or incomplete."""


class InvasionStateError(InvasionError):
    """An operation is not valid in the Invasion's current state."""


class InvasionAlreadyResolvedError(InvasionStateError):
    """A normal gameplay mutation was attempted after final resolution."""


class InvasionTransitionNotDueError(InvasionStateError):
    """A persisted transition timestamp has not yet been reached."""


class InvasionJoinClosedError(InvasionStateError):
    """The Invasion is not currently accepting participants."""


class InvasionUserNotFoundError(InvasionError):
    """The participant User does not exist."""


class InvasionNotParticipantError(InvasionError):
    """The requested player has not joined this Invasion."""


class InvasionNotActiveError(InvasionStateError):
    """The Invasion has not started and cannot accept defense actions."""


class InvasionMainCardSnapshotError(InvasionError):
    """The canonical Main Card changed or its Character cannot be snapshotted."""


class InvasionParticipantNotFoundError(InvasionError):
    """The requested participant does not exist."""


class InvasionParticipantMismatchError(InvasionError):
    """The participant belongs to a different Invasion."""


class InvasionWaveError(InvasionError):
    """A requested wave is missing, stale, or in an invalid state."""


class InvasionInvalidWaveError(InvasionWaveError):
    """The requested wave sequence does not identify a configured wave."""


class InvasionStaleWaveError(InvasionWaveError):
    """The requested wave is not the active, unexpired current wave."""


class InvasionInvalidDefenseActionError(InvasionError):
    """The defense action type or action request is invalid."""


class InvasionDefenseActionConflictError(InvasionError):
    """A defense-action idempotency key was reused with another payload."""


class InvasionDefenseActionAlreadyUsedError(InvasionStateError):
    """The participant already used this defense action in the current Wave."""


class InvasionContributionError(InvasionError):
    """A contribution amount or contribution target is invalid."""


class InvasionContributionConflictError(InvasionContributionError):
    """An idempotency key was reused for different contribution data."""


@dataclass(frozen=True)
class ContributionResult:
    """Persisted totals after one caller-validated contribution."""

    invasion_id: int
    participant_id: int
    wave_sequence: int
    amount: int
    participant_total: int
    wave_total: int
    invasion_total: int
    duplicate: bool = False


@dataclass(frozen=True)
class InvasionDefenseActionResult:
    """Stable, persisted result of one accepted defense action."""

    action_id: int
    invasion_id: int
    participant_id: int
    wave_sequence: int
    action_code: str
    action_version: int
    idempotency_key: str
    power_snapshot_used: Optional[int]
    contribution_amount: int
    participant_total_after: int
    wave_total_after: int
    created_at: datetime
    duplicate: bool = False


@dataclass(frozen=True)
class InvasionDefenseActionAvailability:
    """Persisted viewer/action state for the current Invasion Wave."""

    participant_id: Optional[int]
    participant_total: Optional[int]
    wave_sequence: Optional[int]
    wave_total: int
    wave_actionable: bool
    brace_used: bool
    brace_available: bool


@dataclass(frozen=True)
class InvasionResolutionResult:
    """Final result derived from persisted wave totals."""

    invasion_id: int
    outcome: str
    defense_total: int
    enemy_power: int
    duplicate: bool = False


@dataclass(frozen=True)
class BattleAdvanceResult:
    """What one locked timeline pass changed, for idempotent presentation.

    ``transitions`` lists ``wave_started:<sequence>``, ``resolved``, and
    ``interrupted`` entries in the order they were committed by this pass.
    ``paused_seconds`` is the heartbeat downtime restored to the active wave
    (0 when none). ``interrupted`` means the battle was finalized without an
    outcome because its downtime exceeded the compensation budget.
    """

    invasion_id: int
    status: str
    current_wave_sequence: Optional[int]
    paused_seconds: int = 0
    transitions: tuple[str, ...] = ()
    outcome: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.paused_seconds or self.transitions)


def _is_wave_action_conflict(error: IntegrityError) -> bool:
    """True only for the one-action-per-participant-per-Wave uniqueness rule."""
    message = str(getattr(error, "orig", error))
    return (
        "uq_invasion_defense_actions_participant_wave_action" in message
        or (
            "invasion_defense_actions.participant_id" in message
            and "invasion_defense_actions.wave_id" in message
        )
    )


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise InvasionConfigurationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)


def _now_utc(value: Optional[datetime]) -> datetime:
    return _as_utc(value or datetime.now(_UTC), field_name="now")


def _strict_int(value: Any, *, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvasionConfigurationError(f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise InvasionConfigurationError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return value


def _json_object(value: Any, *, field_name: str) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping):
        raise InvasionConfigurationError(f"{field_name} must be a JSON object")
    if any(not isinstance(key, str) for key in value):
        raise InvasionConfigurationError(f"{field_name} keys must be strings")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded), encoded
    except (TypeError, ValueError):
        raise InvasionConfigurationError(
            f"{field_name} must contain JSON-serializable values"
        ) from None


def _canonical_json(value: Mapping[str, Any], *, field_name: str) -> str:
    return _json_object(value, field_name=field_name)[1]


class InvasionService:
    """Create and transition Invasions inside the caller's transaction.

    Methods flush but never commit or roll back the caller's transaction.
    State-changing operations lock the Invasion row first; this is the shared
    serialization point for joins, waves, contribution, and resolution on
    MySQL/MariaDB. Unique constraints remain the final boundary for occurrence
    creation, participant joins, and contribution request keys.
    """

    # ------------------------------------------------------------------
    # Creation and lookup
    # ------------------------------------------------------------------
    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        group_id: int,
        occurrence_key: str,
        scheduled_at: datetime,
        enemy_faction_code: str,
        enemy_power: int,
        event_config: Mapping[str, Any],
        wave_compositions: Sequence[Mapping[str, Any]],
        event_spec: Optional[Mapping[str, Any]] = None,
        target_region_code: Optional[str] = None,
        target_region_name_snapshot: Optional[str] = None,
        activated_at: Optional[datetime] = None,
        wave_duration_seconds: Optional[int] = None,
    ) -> Invasion:
        """Create one occurrence and persist every configured wave.

        ``wave_compositions`` is the smallest internal wave-spec input: an
        ordered, non-empty sequence of JSON objects, one per wave. Wave
        sequences are one-based.

        Legacy form (``activated_at`` omitted): the occurrence is SCHEDULED and
        its first wave transitions at ``scheduled_at``.

        Four-wave form (``activated_at`` plus ``wave_duration_seconds``): the
        battle is created already ACTIVE. Wave 1 starts at ``activated_at`` and
        ends ``wave_duration_seconds`` later; later waves are SCHEDULED and are
        activated by the timeline at the previous wave's exact deadline.
        Exact duplicate calls return the original Invasion; reusing a key with
        different data raises ``InvasionOccurrenceConflictError``.
        """
        activated_utc: Optional[datetime] = None
        if activated_at is not None:
            activated_utc = _as_utc(activated_at, field_name="activated_at")
            wave_duration_seconds = _strict_int(
                wave_duration_seconds,
                field_name="wave_duration_seconds",
                minimum=1,
                maximum=MAX_SIGNED_INTEGER,
            )
        elif wave_duration_seconds is not None:
            raise InvasionConfigurationError(
                "wave_duration_seconds requires activated_at"
            )
        group_id = _strict_int(
            group_id,
            field_name="group_id",
            minimum=-(1 << 63),
            maximum=MAX_SIGNED_BIGINT,
        )
        if not isinstance(occurrence_key, str) or not occurrence_key.strip():
            raise InvasionConfigurationError("occurrence_key must not be empty")
        if len(occurrence_key) > 255:
            raise InvasionConfigurationError("occurrence_key exceeds 255 characters")
        if not isinstance(enemy_faction_code, str) or not enemy_faction_code.strip():
            raise InvasionConfigurationError("enemy_faction_code must not be empty")
        if len(enemy_faction_code) > 64:
            raise InvasionConfigurationError("enemy_faction_code exceeds 64 characters")
        enemy_power = _strict_int(
            enemy_power,
            field_name="enemy_power",
            minimum=1,
            maximum=MAX_SIGNED_INTEGER,
        )
        scheduled_at = _as_utc(scheduled_at, field_name="scheduled_at")
        _event_config, event_config_json = _json_object(
            event_config, field_name="event_config"
        )

        if (
            not isinstance(wave_compositions, Sequence)
            or isinstance(wave_compositions, (str, bytes, bytearray))
            or not wave_compositions
        ):
            raise InvasionConfigurationError(
                "wave_compositions must be a non-empty ordered sequence"
            )
        normalized_compositions: list[dict[str, Any]] = []
        wave_json: list[str] = []
        for index, composition in enumerate(wave_compositions, start=1):
            normalized, encoded = _json_object(
                composition, field_name=f"wave_compositions[{index - 1}]"
            )
            normalized_compositions.append(normalized)
            wave_json.append(encoded)

        metadata: Optional[dict[str, Any]] = None
        if event_spec is not None:
            metadata, _metadata_json = _json_object(event_spec, field_name="event_spec")
            if "waves" in metadata:
                raise InvasionConfigurationError(
                    "event_spec metadata must not override the internal 'waves' key"
                )
        event_spec_payload: dict[str, Any] = {"waves": normalized_compositions}
        if metadata is not None:
            event_spec_payload["metadata"] = metadata
        event_spec_json = _canonical_json(event_spec_payload, field_name="event_spec")

        if target_region_code is not None:
            if not isinstance(target_region_code, str):
                raise InvasionConfigurationError("target_region_code must be a string")
            if len(target_region_code) > 64:
                raise InvasionConfigurationError("target_region_code exceeds 64 characters")
        if target_region_name_snapshot is not None:
            if not isinstance(target_region_name_snapshot, str):
                raise InvasionConfigurationError(
                    "target_region_name_snapshot must be a string"
                )
            if len(target_region_name_snapshot) > 160:
                raise InvasionConfigurationError(
                    "target_region_name_snapshot exceeds 160 characters"
                )

        if await session.get(Group, group_id) is None:
            raise InvasionGroupNotFoundError(f"Group {group_id} does not exist")

        existing = await cls._by_occurrence_key(
            session, occurrence_key, for_update=True
        )
        request_values = {
            "group_id": group_id,
            "scheduled_at": scheduled_at,
            "enemy_faction_code": enemy_faction_code,
            "enemy_power": enemy_power,
            "event_config_json": event_config_json,
            "event_spec_json": event_spec_json,
            "target_region_code": target_region_code,
            "target_region_name_snapshot": target_region_name_snapshot,
        }
        if existing is not None:
            cls._assert_same_occurrence(existing, request_values)
            return existing

        if activated_utc is None:
            invasion = Invasion(
                group_id=group_id,
                occurrence_key=occurrence_key,
                status=InvasionStatus.SCHEDULED,
                scheduled_at=scheduled_at,
                next_transition_at=scheduled_at,
                current_wave_sequence=1,
                enemy_faction_code=enemy_faction_code,
                enemy_power=enemy_power,
                event_config_json=event_config_json,
                event_spec_json=event_spec_json,
                target_region_code=target_region_code,
                target_region_name_snapshot=target_region_name_snapshot,
                waves=[
                    InvasionWave(
                        wave_sequence=sequence,
                        status=InvasionStatus.SCHEDULED,
                        next_transition_at=(scheduled_at if sequence == 1 else None),
                        enemy_composition_json=composition_json,
                    )
                    for sequence, composition_json in enumerate(wave_json, start=1)
                ],
            )
        else:
            cls._assert_v2_config(
                _event_config,
                wave_count=len(wave_json),
                wave_duration_seconds=wave_duration_seconds,
            )
            first_deadline = activated_utc + timedelta(seconds=wave_duration_seconds)
            invasion = Invasion(
                group_id=group_id,
                occurrence_key=occurrence_key,
                status=InvasionStatus.ACTIVE,
                scheduled_at=scheduled_at,
                started_at=activated_utc,
                next_transition_at=first_deadline,
                current_wave_sequence=1,
                heartbeat_at=activated_utc,
                presentation_stage=0,
                enemy_faction_code=enemy_faction_code,
                enemy_power=enemy_power,
                event_config_json=event_config_json,
                event_spec_json=event_spec_json,
                target_region_code=target_region_code,
                target_region_name_snapshot=target_region_name_snapshot,
                waves=[
                    InvasionWave(
                        wave_sequence=sequence,
                        status=(
                            InvasionStatus.ACTIVE
                            if sequence == 1
                            else InvasionStatus.SCHEDULED
                        ),
                        started_at=(activated_utc if sequence == 1 else None),
                        deadline_at=(first_deadline if sequence == 1 else None),
                        next_transition_at=(first_deadline if sequence == 1 else None),
                        enemy_composition_json=composition_json,
                    )
                    for sequence, composition_json in enumerate(wave_json, start=1)
                ],
            )
        # The database uniqueness constraint is the final race boundary. If a
        # competing transaction wins after the lookup, this flush raises and
        # the caller's transaction manager rolls back; retrying create then
        # finds and returns the winner without committing caller work here.
        session.add(invasion)
        await session.flush()
        return invasion

    @staticmethod
    def _assert_v2_config(
        event_config: Mapping[str, Any],
        *,
        wave_count: int,
        wave_duration_seconds: int,
    ) -> None:
        """Reject a four-wave battle whose snapshot disagrees with its waves."""
        if event_config.get("schema_version") != BATTLE_SCHEMA_VERSION or (
            event_config.get("timing_basis") != BATTLE_TIMING_BASIS
        ):
            raise InvasionConfigurationError(
                "activated battles require the activation-relative event schema"
            )
        if event_config.get("wave_count") != wave_count:
            raise InvasionConfigurationError(
                "event_config wave_count must match the number of wave compositions"
            )
        if event_config.get("wave_duration_seconds") != wave_duration_seconds:
            raise InvasionConfigurationError(
                "event_config wave_duration_seconds must match the wave duration"
            )

    @staticmethod
    def _battle_timing(invasion: Invasion) -> Optional[dict[str, Any]]:
        """Return the four-wave timing snapshot, or None for legacy events.

        Anything that is not an explicit schema-v2 activation snapshot is a
        legacy event and keeps the original Phase-5 timeline. A malformed v2
        snapshot is a configuration error: it must never fall back silently.
        """
        raw = invasion.event_config_json
        if not isinstance(raw, str) or not raw:
            return None
        try:
            config = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(config, dict):
            return None
        if config.get("schema_version") != BATTLE_SCHEMA_VERSION or (
            config.get("timing_basis") != BATTLE_TIMING_BASIS
        ):
            return None
        wave_count = _strict_int(
            config.get("wave_count"),
            field_name="event_config.wave_count",
            minimum=1,
            maximum=MAX_SIGNED_INTEGER,
        )
        wave_duration_seconds = _strict_int(
            config.get("wave_duration_seconds"),
            field_name="event_config.wave_duration_seconds",
            minimum=1,
            maximum=MAX_SIGNED_INTEGER,
        )
        max_compensation_seconds = _strict_int(
            config.get(
                "downtime_compensation_max_seconds",
                DEFAULT_DOWNTIME_COMPENSATION_MAX_SECONDS,
            ),
            field_name="event_config.downtime_compensation_max_seconds",
            minimum=0,
            maximum=MAX_SIGNED_INTEGER,
        )
        return {
            "wave_count": wave_count,
            "wave_duration_seconds": wave_duration_seconds,
            "max_compensation_seconds": max_compensation_seconds,
        }

    @staticmethod
    def _assert_same_occurrence(
        existing: Invasion, request_values: Mapping[str, Any]
    ) -> None:
        same = (
            existing.group_id == request_values["group_id"]
            and _as_utc(existing.scheduled_at, field_name="scheduled_at")
            == request_values["scheduled_at"]
            and existing.enemy_faction_code == request_values["enemy_faction_code"]
            and existing.enemy_power == request_values["enemy_power"]
            and existing.event_config_json == request_values["event_config_json"]
            and existing.event_spec_json == request_values["event_spec_json"]
            and existing.target_region_code == request_values["target_region_code"]
            and existing.target_region_name_snapshot
            == request_values["target_region_name_snapshot"]
        )
        if not same:
            raise InvasionOccurrenceConflictError(
                "occurrence_key already belongs to a different Invasion configuration"
            )

    @staticmethod
    async def _by_occurrence_key(
        session: AsyncSession, occurrence_key: str, *, for_update: bool
    ) -> Optional[Invasion]:
        stmt = (
            select(Invasion)
            .where(Invasion.occurrence_key == occurrence_key)
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    @classmethod
    async def get(cls, session: AsyncSession, invasion_id: int) -> Invasion:
        invasion_id = _strict_int(
            invasion_id,
            field_name="invasion_id",
            minimum=1,
            maximum=MAX_SIGNED_BIGINT,
        )
        invasion = await session.scalar(
            select(Invasion)
            .where(Invasion.id == invasion_id)
            .execution_options(populate_existing=True)
        )
        if invasion is None:
            raise InvasionNotFoundError(f"Invasion {invasion_id} does not exist")
        return invasion

    @classmethod
    async def set_announcement_message_id(
        cls,
        session: AsyncSession,
        invasion_id: int,
        message_id: int,
        *,
        expected_group_id: int,
        expected_message_id: Optional[int],
        presentation_stage: Optional[int] = None,
    ) -> bool:
        """Persist the latest Telegram announcement id with a group-scoped CAS.

        ``presentation_stage`` (four-wave battles) is saved in the same update,
        so an announcement sent for wave N is never followed by a duplicate
        notice for wave N.

        Telegram calls happen outside DB transactions. The caller supplies the
        message id it read before the call so a slower publisher cannot replace
        a newer stored id. This presentation metadata does not affect Invasion
        lifecycle state; the caller still owns commit/rollback.
        """
        invasion_id = _strict_int(
            invasion_id,
            field_name="invasion_id",
            minimum=1,
            maximum=MAX_SIGNED_BIGINT,
        )
        message_id = _strict_int(
            message_id,
            field_name="message_id",
            minimum=1,
            maximum=MAX_SIGNED_BIGINT,
        )
        expected_group_id = _strict_int(
            expected_group_id,
            field_name="expected_group_id",
            minimum=-(1 << 63),
            maximum=MAX_SIGNED_BIGINT,
        )
        if expected_message_id is not None:
            expected_message_id = _strict_int(
                expected_message_id,
                field_name="expected_message_id",
                minimum=1,
                maximum=MAX_SIGNED_BIGINT,
            )

        message_id_condition = (
            Invasion.announcement_message_id.is_(None)
            if expected_message_id is None
            else Invasion.announcement_message_id == expected_message_id
        )
        values: dict[str, Any] = {"announcement_message_id": message_id}
        if presentation_stage is not None:
            values["presentation_stage"] = _strict_int(
                presentation_stage,
                field_name="presentation_stage",
                minimum=0,
                maximum=MAX_SIGNED_INTEGER,
            )
        result = await session.execute(
            update(Invasion)
            .where(
                Invasion.id == invasion_id,
                Invasion.group_id == expected_group_id,
                message_id_condition,
            )
            .values(**values)
        )
        return int(result.rowcount or 0) == 1

    @classmethod
    async def _locked_invasion(
        cls, session: AsyncSession, invasion_id: int
    ) -> Invasion:
        invasion_id = _strict_int(
            invasion_id,
            field_name="invasion_id",
            minimum=1,
            maximum=MAX_SIGNED_BIGINT,
        )
        invasion = await session.scalar(
            select(Invasion)
            .where(Invasion.id == invasion_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if invasion is None:
            raise InvasionNotFoundError(f"Invasion {invasion_id} does not exist")
        return invasion

    @classmethod
    async def _locked_wave(
        cls, session: AsyncSession, invasion_id: int, wave_sequence: int
    ) -> Optional[InvasionWave]:
        return await session.scalar(
            select(InvasionWave)
            .where(
                InvasionWave.invasion_id == invasion_id,
                InvasionWave.wave_sequence == wave_sequence,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @classmethod
    async def _locked_current_wave(
        cls, session: AsyncSession, invasion: Invasion
    ) -> Optional[InvasionWave]:
        if invasion.current_wave_sequence is None:
            return None
        return await cls._locked_wave(
            session, invasion.id, invasion.current_wave_sequence
        )

    @staticmethod
    def _assert_not_resolved(invasion: Invasion) -> None:
        if invasion.status == InvasionStatus.FINISHED:
            raise InvasionAlreadyResolvedError(
                f"Invasion {invasion.id} is already finished"
            )

    @staticmethod
    def _require_active(invasion: Invasion) -> None:
        if invasion.status != InvasionStatus.ACTIVE:
            raise InvasionStateError(
                f"Invasion {invasion.id} must be ACTIVE; current status is {invasion.status}"
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @classmethod
    async def start(
        cls,
        session: AsyncSession,
        invasion_id: int,
        *,
        now: Optional[datetime] = None,
        deadline_at: Optional[datetime] = None,
        allow_overdue_deadline: bool = False,
    ) -> Invasion:
        """Transition SCHEDULED -> ACTIVE and start the due initial wave.

        Repeating ``start`` for an already-active event is a no-op when its
        persisted deadline matches. The event and first-wave transition times
        must be due. ``allow_overdue_deadline`` is reserved for durable
        maintenance/recovery when an absolute snapshotted deadline passed
        during downtime; normal callers still reject past deadlines.
        """
        invasion = await cls._locked_invasion(session, invasion_id)
        requested_deadline = (
            _as_utc(deadline_at, field_name="deadline_at")
            if deadline_at is not None
            else None
        )
        if invasion.status == InvasionStatus.ACTIVE:
            if requested_deadline is not None:
                wave = await cls._locked_current_wave(session, invasion)
                if wave is None or not cls._same_optional_time(
                    wave.deadline_at, requested_deadline
                ):
                    raise InvasionWaveError(
                        "the active wave already has a different deadline"
                    )
            return invasion
        cls._assert_not_resolved(invasion)
        if invasion.status != InvasionStatus.SCHEDULED:
            raise InvasionStateError(f"cannot start Invasion from {invasion.status}")

        now_utc = _now_utc(now)
        scheduled_at = _as_utc(invasion.scheduled_at, field_name="scheduled_at")
        if now_utc < scheduled_at:
            raise InvasionTransitionNotDueError("Invasion scheduled_at has not been reached")

        wave = await cls._locked_current_wave(session, invasion)
        if wave is None or wave.status != InvasionStatus.SCHEDULED:
            raise InvasionWaveError("the initial scheduled wave is missing or invalid")
        if wave.next_transition_at is None:
            raise InvasionWaveError("the initial wave has no persisted transition time")
        if now_utc < _as_utc(wave.next_transition_at, field_name="wave.next_transition_at"):
            raise InvasionTransitionNotDueError("the initial wave transition is not due")
        cls._validate_deadline(
            requested_deadline,
            now_utc,
            allow_overdue=allow_overdue_deadline,
        )
        invasion.status = InvasionStatus.ACTIVE
        invasion.started_at = now_utc
        cls._activate_wave(invasion, wave, now_utc, requested_deadline)
        await session.flush()
        return invasion

    @staticmethod
    def _same_optional_time(value: Optional[datetime], expected: datetime) -> bool:
        return value is not None and _as_utc(value, field_name="deadline_at") == expected

    @staticmethod
    def _validate_deadline(
        deadline_at: Optional[datetime],
        now: datetime,
        *,
        allow_overdue: bool = False,
    ) -> None:
        if (
            deadline_at is not None
            and deadline_at < now
            and not allow_overdue
        ):
            raise InvasionConfigurationError("deadline_at must not be in the past")

    @staticmethod
    def _activate_wave(
        invasion: Invasion,
        wave: InvasionWave,
        now: datetime,
        deadline_at: Optional[datetime],
    ) -> None:
        wave.status = InvasionStatus.ACTIVE
        wave.started_at = now
        wave.deadline_at = deadline_at
        wave.next_transition_at = deadline_at
        invasion.next_transition_at = deadline_at

    # ------------------------------------------------------------------
    # Join and historical Main Card snapshot
    # ------------------------------------------------------------------
    @classmethod
    async def join(
        cls,
        session: AsyncSession,
        invasion_id: int,
        user_id: int,
        *,
        expected_group_id: int,
        now: Optional[datetime] = None,
    ) -> InvasionParticipant:
        """Join an active event once, snapshotting the canonical Main Card.

        No Main Card is an allowed state: in that case the participant's whole
        snapshot stays NULL. Contribution remains possible because this engine
        records caller-validated amounts and does not derive combat from a card.
        """
        invasion = await cls._locked_invasion(session, invasion_id)
        expected_group_id = _strict_int(
            expected_group_id,
            field_name="expected_group_id",
            minimum=-(1 << 63),
            maximum=MAX_SIGNED_BIGINT,
        )
        if invasion.group_id != expected_group_id:
            raise InvasionGroupMismatchError(
                f"Invasion {invasion.id} belongs to Group {invasion.group_id}, "
                f"not Group {expected_group_id}"
            )
        join_now = _now_utc(now)
        await cls._maintain_locked(session, invasion, join_now)
        if invasion.status == InvasionStatus.FINISHED:
            raise InvasionJoinClosedError("finished Invasions do not accept participants")
        if invasion.status != InvasionStatus.ACTIVE or invasion.current_wave_sequence is None:
            raise InvasionJoinClosedError("participants may join only an active Invasion")

        current_wave = await cls._locked_current_wave(session, invasion)
        if current_wave is None or current_wave.status == InvasionStatus.FINISHED:
            raise InvasionJoinClosedError("the Invasion has no joinable wave")
        if cls._battle_timing(invasion) is not None and (
            current_wave.deadline_at is None
            or join_now >= _as_utc(current_wave.deadline_at, field_name="wave.deadline_at")
        ):
            # Participation closes exactly when the battle's final wave ends.
            # After timeline maintenance a due deadline cannot reach here; this
            # guard keeps the rule explicit for four-wave battles.
            raise InvasionJoinClosedError("participation for this wave has closed")

        user_id = _strict_int(
            user_id,
            field_name="user_id",
            minimum=1,
            maximum=MAX_SIGNED_BIGINT,
        )
        existing = await cls._participant_by_pair(
            session, invasion.id, user_id, for_update=True
        )
        if existing is not None:
            return existing

        locked_user_id = await session.scalar(
            select(User.id)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_user_id is None:
            raise InvasionUserNotFoundError(f"User {user_id} does not exist")

        snapshot: dict[str, Any] = {}
        main_card = await PlayerCardService.get_main_card(session, user_id)
        if main_card is not None:
            # The shared API chooses the Main Card. Re-lock that same row to
            # stabilize ownership, favorite state, and level against upgrades
            # or transfers before deriving the snapshot.
            locked_card = await session.scalar(
                select(PlayerCard)
                .where(PlayerCard.id == main_card.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                locked_card is None
                or locked_card.user_id != user_id
                or not locked_card.is_favorite
            ):
                raise InvasionMainCardSnapshotError(
                    "Main Card changed while the participant was joining; retry join"
                )

            character = await session.scalar(
                select(Character)
                .where(Character.id == locked_card.character_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if character is None:
                raise InvasionMainCardSnapshotError(
                    "the selected Main Card has no Character to snapshot"
                )
            attributes = card_attributes(locked_card, character)
            snapshot = {
                "main_card_id_snapshot": locked_card.id,
                "character_id_snapshot": character.id,
                "character_code_snapshot": character.code,
                "character_name_fa_snapshot": character.name_fa,
                "character_name_en_snapshot": character.name_en,
                "character_artwork_snapshot": character.artwork,
                "card_level_snapshot": locked_card.level,
                "power_snapshot": attributes.power,
                "speed_snapshot": attributes.speed,
                "intelligence_snapshot": attributes.intelligence,
                "character_balance_version_snapshot": character.balance_version,
            }

        participant = InvasionParticipant(
            invasion_id=invasion.id,
            user_id=user_id,
            joined_at=_now_utc(now),
            **snapshot,
        )
        # The event lock serializes ordinary joins where row locks exist; the
        # database's (invasion_id, user_id) uniqueness remains authoritative.
        # A uniqueness race aborts the caller transaction, whose retry returns
        # the participant created by the winner.
        session.add(participant)
        await session.flush()
        return participant

    @staticmethod
    async def _participant_by_pair(
        session: AsyncSession,
        invasion_id: int,
        user_id: int,
        *,
        for_update: bool,
    ) -> Optional[InvasionParticipant]:
        stmt = (
            select(InvasionParticipant)
            .where(
                InvasionParticipant.invasion_id == invasion_id,
                InvasionParticipant.user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await session.scalar(stmt)

    # ------------------------------------------------------------------
    # Wave state
    # ------------------------------------------------------------------
    @classmethod
    async def get_current_wave(
        cls, session: AsyncSession, invasion_id: int
    ) -> Optional[InvasionWave]:
        invasion = await cls.get(session, invasion_id)
        if invasion.current_wave_sequence is None:
            return None
        wave = await session.scalar(
            select(InvasionWave)
            .where(
                InvasionWave.invasion_id == invasion.id,
                InvasionWave.wave_sequence == invasion.current_wave_sequence,
            )
            .execution_options(populate_existing=True)
        )
        if wave is None:
            raise InvasionWaveError("current_wave_sequence points to a missing wave")
        return wave

    @classmethod
    async def has_remaining_waves(
        cls, session: AsyncSession, invasion_id: int
    ) -> bool:
        invasion = await cls.get(session, invasion_id)
        if invasion.status == InvasionStatus.FINISHED:
            return False
        remaining = await session.scalar(
            select(func.count(InvasionWave.id)).where(
                InvasionWave.invasion_id == invasion.id,
                InvasionWave.status != InvasionStatus.FINISHED,
            )
        )
        return int(remaining or 0) > 0

    @classmethod
    async def advance_wave(
        cls,
        session: AsyncSession,
        invasion_id: int,
        expected_wave_sequence: int,
        *,
        now: Optional[datetime] = None,
        deadline_at: Optional[datetime] = None,
        allow_overdue_deadline: bool = False,
    ) -> Optional[InvasionWave]:
        """Perform at most one due transition for an expected wave sequence.

        A due SCHEDULED wave starts. Durable maintenance may opt to start it
        with an overdue snapshotted deadline, then finish it on a later bounded
        pass. An ACTIVE wave
        with a reached persisted deadline finishes and schedules the next wave
        at this call's timestamp. An active wave without a due deadline is
        returned unchanged. A replay
        naming a wave that has already advanced returns the current wave without
        mutating it; the expected sequence makes deadline-transition retries
        safe. No sleeping or process-local timer is used.
        """
        invasion = await cls._locked_invasion(session, invasion_id)
        cls._assert_not_resolved(invasion)
        cls._require_active(invasion)
        expected_wave_sequence = _strict_int(
            expected_wave_sequence,
            field_name="expected_wave_sequence",
            minimum=1,
            maximum=MAX_SIGNED_INTEGER,
        )
        if invasion.current_wave_sequence is None:
            return None

        wave = await cls._locked_current_wave(session, invasion)
        if wave is None:
            raise InvasionWaveError("current_wave_sequence points to a missing wave")
        if wave.wave_sequence != expected_wave_sequence:
            if wave.wave_sequence > expected_wave_sequence:
                return wave
            raise InvasionWaveError("expected wave sequence is ahead of the current wave")

        now_utc = _now_utc(now)
        requested_deadline = (
            _as_utc(deadline_at, field_name="deadline_at")
            if deadline_at is not None
            else None
        )

        if wave.status == InvasionStatus.SCHEDULED:
            if wave.next_transition_at is None:
                raise InvasionWaveError("scheduled wave has no persisted transition time")
            if now_utc < _as_utc(
                wave.next_transition_at, field_name="wave.next_transition_at"
            ):
                raise InvasionTransitionNotDueError("wave transition time has not been reached")
            cls._validate_deadline(
                requested_deadline,
                now_utc,
                allow_overdue=allow_overdue_deadline,
            )
            cls._activate_wave(invasion, wave, now_utc, requested_deadline)
            await session.flush()
            return wave

        if wave.status == InvasionStatus.ACTIVE:
            if requested_deadline is not None and not cls._same_optional_time(
                wave.deadline_at, requested_deadline
            ):
                raise InvasionWaveError("an active wave's deadline cannot be replaced")
            if wave.deadline_at is None or now_utc < _as_utc(
                wave.deadline_at, field_name="wave.deadline_at"
            ):
                return wave
            return await cls._finish_wave_locked(session, invasion, wave, now_utc)

        raise InvasionWaveError("current wave is already finished")

    @classmethod
    async def finish_wave(
        cls,
        session: AsyncSession,
        invasion_id: int,
        wave_sequence: int,
        *,
        now: Optional[datetime] = None,
    ) -> InvasionWave:
        """Finish the requested active wave and persist the next transition.

        Finishing an already-finished wave is idempotent while the Invasion is
        still ACTIVE. A wave transition is rejected after final resolution.
        """
        invasion = await cls._locked_invasion(session, invasion_id)
        cls._assert_not_resolved(invasion)
        cls._require_active(invasion)
        wave_sequence = _strict_int(
            wave_sequence,
            field_name="wave_sequence",
            minimum=1,
            maximum=MAX_SIGNED_INTEGER,
        )
        wave = await cls._locked_wave(session, invasion.id, wave_sequence)
        if wave is None:
            raise InvasionWaveError(
                f"wave {wave_sequence} does not belong to Invasion {invasion.id}"
            )
        if wave.status == InvasionStatus.FINISHED:
            return wave
        if invasion.current_wave_sequence != wave_sequence:
            raise InvasionWaveError("only the current wave can be finished")
        if wave.status != InvasionStatus.ACTIVE:
            raise InvasionWaveError("only an active wave can be finished")

        now_utc = _now_utc(now)
        if wave.started_at is None or now_utc < _as_utc(
            wave.started_at, field_name="wave.started_at"
        ):
            raise InvasionWaveError("wave cannot finish before it starts")
        await cls._finish_wave_locked(session, invasion, wave, now_utc)
        return wave

    @classmethod
    async def _finish_wave_locked(
        cls,
        session: AsyncSession,
        invasion: Invasion,
        wave: InvasionWave,
        ended_at: datetime,
    ) -> Optional[InvasionWave]:
        """Finish a validated current wave while the Invasion row is locked."""
        next_wave = await session.scalar(
            select(InvasionWave)
            .where(
                InvasionWave.invasion_id == invasion.id,
                InvasionWave.wave_sequence > wave.wave_sequence,
            )
            .order_by(InvasionWave.wave_sequence.asc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if next_wave is not None and next_wave.status != InvasionStatus.SCHEDULED:
            raise InvasionWaveError("the next configured wave is not SCHEDULED")

        wave.status = InvasionStatus.FINISHED
        wave.ended_at = ended_at
        wave.next_transition_at = None
        if next_wave is None:
            invasion.current_wave_sequence = None
            invasion.next_transition_at = None
        else:
            next_wave.next_transition_at = ended_at
            invasion.current_wave_sequence = next_wave.wave_sequence
            invasion.next_transition_at = ended_at
        await session.flush()
        return next_wave

    # ------------------------------------------------------------------
    # Four-wave battle timeline (schema v2)
    # ------------------------------------------------------------------
    @classmethod
    async def advance_battle(
        cls,
        session: AsyncSession,
        invasion_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> BattleAdvanceResult:
        """Bring one battle up to ``now`` under its Invasion row lock.

        Idempotent: calling it again at the same instant changes nothing. Legacy
        events return an unchanged result, because their timeline is untouched.
        """
        invasion = await cls._locked_invasion(session, invasion_id)
        return await cls._maintain_locked(session, invasion, _now_utc(now))

    @classmethod
    async def _maintain_locked(
        cls,
        session: AsyncSession,
        invasion: Invasion,
        now_utc: datetime,
    ) -> BattleAdvanceResult:
        """Restore downtime, then commit every wave boundary that is due.

        Runs on an already locked Invasion, so join, Brace, and the maintenance
        tick all observe the same logical wave at a given instant.

        1. Downtime: if the heartbeat is older than the tolerance, the excess is
           added to the active wave's deadline (and the battle's next
           transition), as long as the battle's total compensation stays within
           its snapshotted budget. The remaining playable time is therefore the
           time that was left at the last heartbeat, minus at most the
           tolerance. A stall that would exceed the budget finalizes the battle
           as interrupted instead (see ``_interrupt_locked``).
        2. Boundaries: while the active wave's deadline has passed, it ends at
           that exact deadline and the next wave starts there, lasting one wave
           duration. The final wave's end resolves the battle exactly once.

        Because boundaries are derived from persisted deadlines, a late tick
        yields the same timeline as an on-time tick, and no wave is processed
        twice (each committed transition advances the persisted sequence).
        """
        timing = cls._battle_timing(invasion)
        if timing is None or invasion.status != InvasionStatus.ACTIVE:
            return BattleAdvanceResult(
                invasion_id=int(invasion.id),
                status=invasion.status,
                current_wave_sequence=invasion.current_wave_sequence,
                outcome=invasion.outcome,
            )

        wave_seconds = timedelta(seconds=timing["wave_duration_seconds"])
        paused_seconds = 0
        if invasion.heartbeat_at is not None:
            heartbeat_utc = _as_utc(invasion.heartbeat_at, field_name="heartbeat_at")
            gap_seconds = int((now_utc - heartbeat_utc).total_seconds())
            excess = gap_seconds - HEARTBEAT_TOLERANCE_SECONDS
            if excess > 0:
                wave = await cls._locked_current_wave(session, invasion)
                if wave is None or wave.status != InvasionStatus.ACTIVE or (
                    wave.deadline_at is None
                ):
                    raise InvasionWaveError(
                        "active battle has no running wave to restore"
                    )
                already = cls._compensated_seconds(
                    invasion, wave, timing["wave_duration_seconds"]
                )
                if already + excess > timing["max_compensation_seconds"]:
                    # Over budget: the battle ends at its last known-running
                    # instant. Nothing is extended, so a long outage can never
                    # hold the Group's next occurrence.
                    await cls._interrupt_locked(
                        session, invasion, ended_at=heartbeat_utc
                    )
                    return BattleAdvanceResult(
                        invasion_id=int(invasion.id),
                        status=invasion.status,
                        current_wave_sequence=None,
                        transitions=("interrupted",),
                        outcome=None,
                    )
                restored = _as_utc(wave.deadline_at, field_name="wave.deadline_at") + (
                    timedelta(seconds=excess)
                )
                wave.deadline_at = restored
                wave.next_transition_at = restored
                invasion.next_transition_at = restored
                paused_seconds = excess
        # The heartbeat only moves forward. Two workers may read the clock at
        # slightly different instants; a late writer holding an older instant
        # must never make the next tick look like downtime.
        if invasion.heartbeat_at is None or now_utc > _as_utc(
            invasion.heartbeat_at, field_name="heartbeat_at"
        ):
            invasion.heartbeat_at = now_utc

        transitions: list[str] = []
        for _step in range(timing["wave_count"] + 1):
            if invasion.status != InvasionStatus.ACTIVE:
                break
            wave = await cls._locked_current_wave(session, invasion)
            if wave is None:
                raise InvasionWaveError("current_wave_sequence points to a missing wave")
            if wave.status != InvasionStatus.ACTIVE or wave.deadline_at is None:
                raise InvasionWaveError("active battle has an inconsistent current wave")
            deadline = _as_utc(wave.deadline_at, field_name="wave.deadline_at")
            if now_utc < deadline:
                break

            wave.status = InvasionStatus.FINISHED
            wave.ended_at = deadline
            wave.next_transition_at = None
            next_wave = await session.scalar(
                select(InvasionWave)
                .where(
                    InvasionWave.invasion_id == invasion.id,
                    InvasionWave.wave_sequence == wave.wave_sequence + 1,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if next_wave is None:
                await cls._finalize_locked(session, invasion, deadline)
                transitions.append("resolved")
                break
            if next_wave.status != InvasionStatus.SCHEDULED:
                raise InvasionWaveError("the next configured wave is not SCHEDULED")
            next_deadline = deadline + wave_seconds
            next_wave.status = InvasionStatus.ACTIVE
            next_wave.started_at = deadline
            next_wave.deadline_at = next_deadline
            next_wave.next_transition_at = next_deadline
            invasion.current_wave_sequence = next_wave.wave_sequence
            invasion.next_transition_at = next_deadline
            transitions.append(f"wave_started:{next_wave.wave_sequence}")
        else:
            raise InvasionWaveError("battle advanced more waves than it has")

        await session.flush()
        return BattleAdvanceResult(
            invasion_id=int(invasion.id),
            status=invasion.status,
            current_wave_sequence=invasion.current_wave_sequence,
            paused_seconds=paused_seconds,
            transitions=tuple(transitions),
            outcome=invasion.outcome,
        )

    @staticmethod
    def _compensated_seconds(
        invasion: Invasion, wave: InvasionWave, wave_seconds: int
    ) -> int:
        """Total downtime already added to this battle's timeline (in seconds).

        Wave k is nominally due at ``started_at + k * wave_seconds``. Each
        restoration moves the current wave's deadline, and every later wave
        starts at the previous deadline, so the gap between the current
        deadline and its nominal instant is exactly the compensation granted so
        far. It is derived from persisted fields, so the budget survives
        restarts without a separate counter.
        """
        if invasion.started_at is None or wave.deadline_at is None:
            raise InvasionWaveError(
                "battle timeline is missing its activation or wave deadline"
            )
        started = _as_utc(invasion.started_at, field_name="invasion.started_at")
        nominal = started + timedelta(seconds=wave.wave_sequence * wave_seconds)
        shift = _as_utc(wave.deadline_at, field_name="wave.deadline_at") - nominal
        seconds = shift // timedelta(seconds=1)
        if seconds < 0:
            raise InvasionWaveError(
                "battle deadline precedes its nominal wave schedule"
            )
        return int(seconds)

    @classmethod
    async def _interrupt_locked(
        cls,
        session: AsyncSession,
        invasion: Invasion,
        *,
        ended_at: datetime,
    ) -> None:
        """Finalize an over-budget battle as interrupted (caller holds the lock).

        Reuses the existing voided terminal shape: FINISHED with no outcome. It
        is therefore never a player-caused defeat and is excluded from reward
        settlement. ``rewards_settled_at`` is stamped in the same flush, so
        nothing is owed. ``ended_at`` is the last heartbeat, the last instant
        the battle was known to be running. Every wave is closed, which also
        removes the battle from the overlap guard for its Group.
        """
        waves = list(
            (
                await session.scalars(
                    select(InvasionWave)
                    .where(InvasionWave.invasion_id == invasion.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        for wave in waves:
            wave.status = InvasionStatus.FINISHED
            if wave.ended_at is None:
                wave.ended_at = ended_at
            wave.next_transition_at = None
        invasion.status = InvasionStatus.FINISHED
        invasion.outcome = None
        invasion.ended_at = ended_at
        invasion.current_wave_sequence = None
        invasion.next_transition_at = None
        invasion.rewards_settled_at = ended_at
        await session.flush()

    @classmethod
    async def _finalize_locked(
        cls,
        session: AsyncSession,
        invasion: Invasion,
        ended_at: datetime,
    ) -> None:
        """Resolve a battle whose final wave has ended (caller holds the lock)."""
        defense_total = await cls._invasion_contribution_total(session, invasion.id)
        invasion.status = InvasionStatus.FINISHED
        invasion.outcome = outcome_for(defense_total, invasion.enemy_power)
        invasion.ended_at = ended_at
        invasion.current_wave_sequence = None
        invasion.next_transition_at = None
        await session.flush()

    @classmethod
    async def void_missed_legacy_battle(
        cls,
        session: AsyncSession,
        invasion_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        """Close a legacy SCHEDULED occurrence whose first window already passed.

        Players never had an opportunity to defend, so it is recorded as
        FINISHED with no outcome instead of resolving as a defeat. It has no
        outcome, so it is never settled. Returns False when nothing was closed.
        """
        invasion = await cls._locked_invasion(session, invasion_id)
        if invasion.status != InvasionStatus.SCHEDULED:
            return False
        ended_at = _now_utc(now)
        invasion.status = InvasionStatus.FINISHED
        invasion.outcome = None
        invasion.ended_at = ended_at
        invasion.current_wave_sequence = None
        invasion.next_transition_at = None
        waves = list(
            (
                await session.scalars(
                    select(InvasionWave)
                    .where(InvasionWave.invasion_id == invasion.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        for wave in waves:
            wave.status = InvasionStatus.FINISHED
            wave.ended_at = ended_at
            wave.next_transition_at = None
        await session.flush()
        return True

    @classmethod
    async def set_presentation_stage(
        cls,
        session: AsyncSession,
        invasion_id: int,
        *,
        expected_stage: Optional[int],
        new_stage: int,
    ) -> bool:
        """Compare-and-set the delivered Telegram stage (presentation only).

        Called after a Telegram notice succeeds, so a retry re-sends only what
        was not yet delivered. A lost race returns False and changes nothing.
        """
        invasion_id = _strict_int(
            invasion_id, field_name="invasion_id", minimum=1, maximum=MAX_SIGNED_BIGINT
        )
        new_stage = _strict_int(
            new_stage, field_name="new_stage", minimum=0, maximum=MAX_SIGNED_INTEGER
        )
        stage_condition = (
            Invasion.presentation_stage.is_(None)
            if expected_stage is None
            else Invasion.presentation_stage == expected_stage
        )
        result = await session.execute(
            update(Invasion)
            .where(Invasion.id == invasion_id, stage_condition)
            .values(presentation_stage=new_stage)
        )
        return int(result.rowcount or 0) == 1

    # ------------------------------------------------------------------
    # Defense actions
    # ------------------------------------------------------------------
    @classmethod
    async def get_defense_action_availability(
        cls,
        session: AsyncSession,
        invasion_id: int,
        user_id: Optional[int] = None,
        *,
        now: Optional[datetime] = None,
    ) -> InvasionDefenseActionAvailability:
        """Read persisted Wave/action state, optionally for one viewer.

        This is a presentation read model only. ``perform_defense_action``
        revalidates the persisted state under the Invasion row lock before it
        accepts anything. ``now`` is a trusted service/test seam, never a
        transport-supplied timestamp.
        """
        invasion_id = _strict_int(
            invasion_id,
            field_name="invasion_id",
            minimum=1,
            maximum=MAX_SIGNED_BIGINT,
        )
        if user_id is not None:
            user_id = _strict_int(
                user_id,
                field_name="user_id",
                minimum=1,
                maximum=MAX_SIGNED_BIGINT,
            )
        invasion = await session.get(Invasion, invasion_id)
        if invasion is None:
            raise InvasionNotFoundError(f"Invasion {invasion_id} does not exist")

        participant = None
        if user_id is not None:
            participant = await session.scalar(
                select(InvasionParticipant).where(
                    InvasionParticipant.invasion_id == invasion.id,
                    InvasionParticipant.user_id == user_id,
                )
            )
        wave = None
        if invasion.current_wave_sequence is not None:
            wave = await session.scalar(
                select(InvasionWave).where(
                    InvasionWave.invasion_id == invasion.id,
                    InvasionWave.wave_sequence == invasion.current_wave_sequence,
                )
            )

        now_utc = _now_utc(now)
        wave_actionable = bool(
            invasion.status == InvasionStatus.ACTIVE
            and wave is not None
            and wave.status == InvasionStatus.ACTIVE
            and (
                wave.deadline_at is None
                or now_utc < _as_utc(
                    wave.deadline_at,
                    field_name="wave.deadline_at",
                )
            )
        )
        brace_used = False
        if participant is not None and wave is not None:
            brace_used = bool(
                await session.scalar(
                    select(InvasionDefenseAction.id)
                    .where(
                        InvasionDefenseAction.invasion_id == invasion.id,
                        InvasionDefenseAction.participant_id == participant.id,
                        InvasionDefenseAction.wave_id == wave.id,
                        InvasionDefenseAction.action_code
                        == InvasionDefenseActionCode.BRACE,
                    )
                    .limit(1)
                )
            )

        return InvasionDefenseActionAvailability(
            participant_id=int(participant.id) if participant is not None else None,
            participant_total=(
                int(participant.contribution_total)
                if participant is not None
                else None
            ),
            wave_sequence=(
                int(invasion.current_wave_sequence)
                if invasion.current_wave_sequence is not None
                else None
            ),
            wave_total=int(wave.contribution_total) if wave is not None else 0,
            wave_actionable=wave_actionable,
            brace_used=brace_used,
            brace_available=bool(
                participant is not None and wave_actionable and not brace_used
            ),
        )

    @classmethod
    async def perform_defense_action(
        cls,
        session: AsyncSession,
        invasion_id: int,
        user_id: int,
        *,
        expected_group_id: int,
        wave_sequence: int,
        action_code: str,
        idempotency_key: str,
        now: Optional[datetime] = None,
    ) -> InvasionDefenseActionResult:
        """Accept one deterministic V1 defense action within the caller's transaction.

        V1 supports only ``brace``. Its provisional contribution is
        ``1 + floor(persisted participant power_snapshot / 10)``; a participant
        without a Main Card snapshot contributes the base 1. The action never
        reads or recalculates a live PlayerCard/Character. The action record,
        existing contribution-ledger row, and both aggregates are flushed in
        the same transaction and this method never commits or rolls back.

        MySQL/MariaDB serializes all mutations for one Invasion on its row lock.
        A participant may perform ``brace`` only once per Wave: after exact-key
        replay detection, the service checks the persisted action record while
        holding that Invasion lock. This parent-row serialization is the
        authoritative concurrency guard; it reuses the Phase 7 table and needs
        no parallel ledger or schema change. A matching action replay is
        returned before lifecycle/deadline checks, even after resolution.
        Reusing a key for another payload is a deterministic domain conflict.
        ``now`` is a trusted service/test seam; transport callers must never
        pass a client-supplied timestamp.
        """
        user_id = _strict_int(
            user_id,
            field_name="user_id",
            minimum=1,
            maximum=MAX_SIGNED_BIGINT,
        )
        expected_group_id = _strict_int(
            expected_group_id,
            field_name="expected_group_id",
            minimum=-(1 << 63),
            maximum=MAX_SIGNED_BIGINT,
        )
        if (
            isinstance(wave_sequence, bool)
            or not isinstance(wave_sequence, int)
            or wave_sequence <= 0
            or wave_sequence > MAX_SIGNED_INTEGER
        ):
            raise InvasionInvalidWaveError("wave_sequence must be a positive integer")
        if (
            not isinstance(action_code, str)
            or not action_code.strip()
            or len(action_code) > 32
        ):
            raise InvasionInvalidDefenseActionError("action_code is invalid")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvasionInvalidDefenseActionError("idempotency_key must not be empty")
        if len(idempotency_key) > 255:
            raise InvasionInvalidDefenseActionError("idempotency_key exceeds 255 characters")

        invasion = await cls._locked_invasion(session, invasion_id)
        if invasion.group_id != expected_group_id:
            raise InvasionGroupMismatchError(
                f"Invasion {invasion.id} belongs to Group {invasion.group_id}, "
                f"not Group {expected_group_id}"
            )
        # Bring waves up to the action instant first, so Brace is judged against
        # the wave that is logically current, not a stale persisted one.
        await cls._maintain_locked(session, invasion, _now_utc(now))

        participant = await session.scalar(
            select(InvasionParticipant)
            .where(
                InvasionParticipant.invasion_id == invasion.id,
                InvasionParticipant.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if participant is None:
            raise InvasionNotParticipantError(
                f"User {user_id} is not a participant in Invasion {invasion.id}"
            )

        existing = await cls._locked_defense_action(
            session,
            invasion.id,
            idempotency_key,
        )
        if existing is not None:
            if (
                existing.participant_id != participant.id
                or existing.wave_sequence != wave_sequence
                or existing.action_code != action_code
            ):
                raise InvasionDefenseActionConflictError(
                    "idempotency_key already belongs to a different defense action"
                )
            return cls._defense_action_result(existing, duplicate=True)

        if action_code not in InvasionDefenseActionCode.ALL:
            raise InvasionInvalidDefenseActionError(
                f"unsupported defense action code: {action_code!r}"
            )
        if invasion.status == InvasionStatus.FINISHED:
            raise InvasionAlreadyResolvedError(
                "finished Invasions do not accept new defense actions"
            )
        if invasion.status != InvasionStatus.ACTIVE:
            raise InvasionNotActiveError(
                "defense actions require an active Invasion"
            )

        wave = await cls._locked_wave(session, invasion.id, wave_sequence)
        if wave is None:
            raise InvasionInvalidWaveError(
                f"wave {wave_sequence} does not belong to Invasion {invasion.id}"
            )
        if (
            invasion.current_wave_sequence != wave_sequence
            or wave.status != InvasionStatus.ACTIVE
        ):
            raise InvasionStaleWaveError(
                "defense action must target the currently active wave"
            )
        # Resolve the service clock only after the row locks are held, so a
        # request that waited on another transaction cannot cross its deadline
        # using a timestamp captured before the wait.
        now_utc = _now_utc(now)
        if wave.deadline_at is not None and now_utc >= _as_utc(
            wave.deadline_at,
            field_name="wave.deadline_at",
        ):
            raise InvasionStaleWaveError("the current wave deadline has passed")

        # Idempotent replay above wins over availability so a retried callback
        # receives its original result. Distinct action keys cannot bypass the
        # per-participant/per-Wave rule: every writer first locks this Invasion,
        # then performs this current-read against the existing action table.
        already_used = await cls._locked_wave_defense_action(
            session,
            invasion_id=invasion.id,
            participant_id=participant.id,
            wave_id=wave.id,
            action_code=action_code,
        )
        if already_used is not None:
            raise InvasionDefenseActionAlreadyUsedError(
                f"participant {participant.id} already used {action_code!r} "
                f"in Wave {wave.wave_sequence}"
            )

        power_snapshot_used = participant.power_snapshot
        if power_snapshot_used is not None and (
            isinstance(power_snapshot_used, bool)
            or not isinstance(power_snapshot_used, int)
            or power_snapshot_used < 0
        ):
            raise InvasionInvalidDefenseActionError(
                "participant power snapshot is invalid"
            )
        power_for_formula = power_snapshot_used if power_snapshot_used is not None else 0
        if action_code == InvasionDefenseActionCode.BRACE:
            contribution_amount = (
                BRACE_BASE_CONTRIBUTION
                + power_for_formula // BRACE_POWER_DIVISOR
            )
        else:  # guarded by the explicit action-code registry above
            raise InvasionInvalidDefenseActionError(
                f"unsupported defense action code: {action_code!r}"
            )

        try:
            contribution_result = await cls.record_contribution(
                session,
                invasion.id,
                participant.id,
                wave_sequence,
                contribution_amount,
                idempotency_key=idempotency_key,
                now=now_utc,
            )
        except InvasionContributionConflictError as error:
            raise InvasionDefenseActionConflictError(
                "idempotency_key already belongs to another contribution"
            ) from error
        if contribution_result.duplicate:
            # A bare contribution with the same key is not an accepted action;
            # do not manufacture a second action record around another source.
            raise InvasionDefenseActionConflictError(
                "idempotency_key already belongs to a non-action contribution"
            )

        contribution = await cls._locked_contribution(
            session,
            invasion.id,
            idempotency_key,
        )
        if (
            contribution is None
            or contribution.participant_id != participant.id
            or contribution.wave_id != wave.id
            or contribution.amount != contribution_amount
        ):
            raise InvasionStateError(
                "accepted defense contribution could not be linked to its action"
            )

        action = InvasionDefenseAction(
            invasion_id=invasion.id,
            participant_id=participant.id,
            wave_id=wave.id,
            contribution_id=contribution.id,
            wave_sequence=wave_sequence,
            action_code=action_code,
            action_version=INVASION_DEFENSE_ACTION_VERSION,
            idempotency_key=idempotency_key,
            power_snapshot_used=power_snapshot_used,
            contribution_amount=contribution_amount,
            participant_total_after=contribution_result.participant_total,
            wave_total_after=contribution_result.wave_total,
            created_at=now_utc,
            updated_at=now_utc,
        )
        # Capture display values first: a failed flush expires loaded instances,
        # and reading them afterwards would itself fail inside the session.
        already_used_message = (
            f"participant {int(participant.id)} already used {action_code!r} "
            f"in Wave {int(wave.wave_sequence)}"
        )
        session.add(action)
        try:
            await session.flush()
        except IntegrityError as error:
            # The database rule (one action per participant, Wave, and code) is
            # the atomic backstop for simultaneous callbacks that both passed
            # the locked pre-check. The caller's transaction then rolls back.
            if _is_wave_action_conflict(error):
                raise InvasionDefenseActionAlreadyUsedError(already_used_message) from error
            raise
        return cls._defense_action_result(action, duplicate=False)

    @staticmethod
    async def _locked_defense_action(
        session: AsyncSession,
        invasion_id: int,
        idempotency_key: str,
    ) -> Optional[InvasionDefenseAction]:
        return await session.scalar(
            select(InvasionDefenseAction)
            .where(
                InvasionDefenseAction.invasion_id == invasion_id,
                InvasionDefenseAction.idempotency_key == idempotency_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @staticmethod
    async def _locked_wave_defense_action(
        session: AsyncSession,
        *,
        invasion_id: int,
        participant_id: int,
        wave_id: int,
        action_code: str,
    ) -> Optional[InvasionDefenseAction]:
        return await session.scalar(
            select(InvasionDefenseAction)
            .where(
                InvasionDefenseAction.invasion_id == invasion_id,
                InvasionDefenseAction.participant_id == participant_id,
                InvasionDefenseAction.wave_id == wave_id,
                InvasionDefenseAction.action_code == action_code,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @staticmethod
    def _defense_action_result(
        action: InvasionDefenseAction,
        *,
        duplicate: bool,
    ) -> InvasionDefenseActionResult:
        return InvasionDefenseActionResult(
            action_id=int(action.id),
            invasion_id=int(action.invasion_id),
            participant_id=int(action.participant_id),
            wave_sequence=int(action.wave_sequence),
            action_code=action.action_code,
            action_version=int(action.action_version),
            idempotency_key=action.idempotency_key,
            power_snapshot_used=action.power_snapshot_used,
            contribution_amount=int(action.contribution_amount),
            participant_total_after=int(action.participant_total_after),
            wave_total_after=int(action.wave_total_after),
            created_at=_as_utc(action.created_at, field_name="action.created_at"),
            duplicate=duplicate,
        )

    # ------------------------------------------------------------------
    # Contribution
    # ------------------------------------------------------------------
    @classmethod
    async def record_contribution(
        cls,
        session: AsyncSession,
        invasion_id: int,
        participant_id: int,
        wave_sequence: int,
        amount: int,
        *,
        idempotency_key: str,
        now: Optional[datetime] = None,
    ) -> ContributionResult:
        """Apply one positive caller-validated amount exactly once per key.

        The required opaque key is supplied by the caller and is unique within
        this Invasion. Replaying the same key and payload returns the current
        aggregates without incrementing them; reusing a key for different data
        raises ``InvasionContributionConflictError``. The ledger row and both
        aggregate updates share the caller's transaction.

        No Main Card is required: contribution is not calculated from card
        stats in this engine. Wave aggregates are the authoritative event
        defense total (summed at resolution); participant totals are maintained
        in parallel for per-player state.
        """
        if (
            isinstance(amount, bool)
            or not isinstance(amount, int)
            or amount <= 0
            or amount > MAX_SIGNED_BIGINT
        ):
            raise InvasionContributionError(
                f"amount must be a positive integer no greater than {MAX_SIGNED_BIGINT}"
            )
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvasionContributionError("idempotency_key must not be empty")
        if len(idempotency_key) > 255:
            raise InvasionContributionError("idempotency_key exceeds 255 characters")
        participant_id = _strict_int(
            participant_id,
            field_name="participant_id",
            minimum=1,
            maximum=MAX_SIGNED_BIGINT,
        )
        wave_sequence = _strict_int(
            wave_sequence,
            field_name="wave_sequence",
            minimum=1,
            maximum=MAX_SIGNED_INTEGER,
        )
        invasion = await cls._locked_invasion(session, invasion_id)
        await cls._maintain_locked(session, invasion, _now_utc(now))
        cls._assert_not_resolved(invasion)
        cls._require_active(invasion)

        participant = await session.scalar(
            select(InvasionParticipant)
            .where(InvasionParticipant.id == participant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if participant is None:
            raise InvasionParticipantNotFoundError(
                f"participant {participant_id} does not exist"
            )
        if participant.invasion_id != invasion.id:
            raise InvasionParticipantMismatchError(
                f"participant {participant_id} belongs to Invasion {participant.invasion_id}"
            )

        existing = await cls._locked_contribution(
            session, invasion.id, idempotency_key
        )
        if existing is not None:
            cls._assert_same_contribution(
                existing,
                participant_id=participant.id,
                wave_sequence=wave_sequence,
                amount=amount,
            )
            return await cls._contribution_result(
                session, invasion, participant, existing, duplicate=True
            )

        if invasion.current_wave_sequence != wave_sequence:
            raise InvasionWaveError("contribution must target the current wave")
        wave = await cls._locked_wave(session, invasion.id, wave_sequence)
        if wave is None:
            raise InvasionWaveError(f"wave {wave_sequence} does not exist")
        if wave.status != InvasionStatus.ACTIVE:
            raise InvasionWaveError("contribution requires an active wave")

        now_utc = _now_utc(now)
        if wave.deadline_at is not None and now_utc >= _as_utc(
            wave.deadline_at, field_name="wave.deadline_at"
        ):
            raise InvasionContributionError("wave deadline has been reached")
        if participant.contribution_total + amount > MAX_SIGNED_BIGINT:
            raise InvasionContributionError("participant contribution total would overflow")
        if wave.contribution_total + amount > MAX_SIGNED_BIGINT:
            raise InvasionContributionError("wave contribution total would overflow")

        record = InvasionContribution(
            invasion_id=invasion.id,
            participant_id=participant.id,
            wave_id=wave.id,
            wave_sequence=wave.wave_sequence,
            amount=amount,
            idempotency_key=idempotency_key,
        )
        # The event lock serializes contribution keys where supported; the
        # unique index remains the integrity boundary. Do not create a nested
        # transaction here: this method must never commit caller work.
        session.add(record)
        await session.flush()

        invasion_total = await cls._invasion_contribution_total(session, invasion.id)
        participant.contribution_total += amount
        wave.contribution_total += amount
        await session.flush()
        return ContributionResult(
            invasion_id=invasion.id,
            participant_id=participant.id,
            wave_sequence=wave.wave_sequence,
            amount=amount,
            participant_total=participant.contribution_total,
            wave_total=wave.contribution_total,
            invasion_total=invasion_total + amount,
            duplicate=False,
        )

    @staticmethod
    async def _locked_contribution(
        session: AsyncSession, invasion_id: int, idempotency_key: str
    ) -> Optional[InvasionContribution]:
        return await session.scalar(
            select(InvasionContribution)
            .where(
                InvasionContribution.invasion_id == invasion_id,
                InvasionContribution.idempotency_key == idempotency_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @staticmethod
    def _assert_same_contribution(
        existing: InvasionContribution,
        *,
        participant_id: int,
        wave_sequence: int,
        amount: int,
    ) -> None:
        if (
            existing.participant_id != participant_id
            or existing.wave_sequence != wave_sequence
            or existing.amount != amount
        ):
            raise InvasionContributionConflictError(
                "idempotency_key already belongs to a different contribution"
            )

    @classmethod
    async def _contribution_result(
        cls,
        session: AsyncSession,
        invasion: Invasion,
        participant: InvasionParticipant,
        record: InvasionContribution,
        *,
        duplicate: bool,
    ) -> ContributionResult:
        wave = await session.scalar(
            select(InvasionWave)
            .where(InvasionWave.id == record.wave_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if wave is None or wave.invasion_id != invasion.id:
            raise InvasionWaveError("contribution references a missing wave")
        invasion_total = await cls._invasion_contribution_total(session, invasion.id)
        return ContributionResult(
            invasion_id=invasion.id,
            participant_id=participant.id,
            wave_sequence=record.wave_sequence,
            amount=record.amount,
            participant_total=participant.contribution_total,
            wave_total=wave.contribution_total,
            invasion_total=invasion_total,
            duplicate=duplicate,
        )

    @staticmethod
    async def _invasion_contribution_total(
        session: AsyncSession, invasion_id: int
    ) -> int:
        total = await session.scalar(
            select(func.coalesce(func.sum(InvasionWave.contribution_total), 0)).where(
                InvasionWave.invasion_id == invasion_id
            )
        )
        return int(total or 0)

    # ------------------------------------------------------------------
    # Final outcome
    # ------------------------------------------------------------------
    @classmethod
    async def resolve(
        cls,
        session: AsyncSession,
        invasion_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> InvasionResolutionResult:
        """Resolve an exhausted active event using persisted wave totals.

        ``TOTAL_VICTORY`` is defense >= enemy power; ``NARROW_DEFENSE`` is
        defense * 100 >= enemy_power * 75; otherwise the outcome is ``DEFEAT``.
        Integer comparisons avoid floating-point threshold drift. On
        MySQL/MariaDB the Invasion row lock serializes concurrent resolutions;
        a conditional UPDATE is an additional compare-and-swap guard.
        """
        invasion = await cls._locked_invasion(session, invasion_id)
        if invasion.status == InvasionStatus.FINISHED:
            if invasion.outcome not in InvasionOutcome.ALL:
                raise InvasionStateError("finished Invasion has no approved outcome")
            defense_total = await cls._invasion_contribution_total(
                session, invasion.id
            )
            return InvasionResolutionResult(
                invasion_id=invasion.id,
                outcome=invasion.outcome,
                defense_total=defense_total,
                enemy_power=invasion.enemy_power,
                duplicate=True,
            )
        if invasion.status != InvasionStatus.ACTIVE:
            raise InvasionStateError(
                "only an active Invasion can be resolved through the resolution path"
            )
        if invasion.current_wave_sequence is not None:
            raise InvasionWaveError("all waves must finish before resolving the Invasion")

        wave_count = await session.scalar(
            select(func.count(InvasionWave.id)).where(
                InvasionWave.invasion_id == invasion.id
            )
        )
        unfinished_count = await session.scalar(
            select(func.count(InvasionWave.id)).where(
                InvasionWave.invasion_id == invasion.id,
                InvasionWave.status != InvasionStatus.FINISHED,
            )
        )
        if not wave_count or unfinished_count:
            raise InvasionWaveError("the Invasion has unfinished or missing wave state")
        if invasion.enemy_power <= 0:
            raise InvasionConfigurationError("enemy_power must be positive to resolve")

        defense_total = await cls._invasion_contribution_total(session, invasion.id)
        outcome = outcome_for(defense_total, invasion.enemy_power)
        ended_at = _now_utc(now)

        claim = await session.execute(
            update(Invasion)
            .where(
                Invasion.id == invasion.id,
                Invasion.status == InvasionStatus.ACTIVE,
                Invasion.outcome.is_(None),
                Invasion.current_wave_sequence.is_(None),
            )
            .values(
                status=InvasionStatus.FINISHED,
                outcome=outcome,
                ended_at=ended_at,
                next_transition_at=None,
            )
        )
        if int(claim.rowcount or 0) != 1:
            current = await cls._locked_invasion(session, invasion.id)
            if current.status == InvasionStatus.FINISHED and current.outcome in InvasionOutcome.ALL:
                current_total = await cls._invasion_contribution_total(
                    session, current.id
                )
                return InvasionResolutionResult(
                    invasion_id=current.id,
                    outcome=current.outcome,
                    defense_total=current_total,
                    enemy_power=current.enemy_power,
                    duplicate=True,
                )
            raise InvasionStateError("Invasion resolution lost its state claim")

        invasion.status = InvasionStatus.FINISHED
        invasion.outcome = outcome
        invasion.ended_at = ended_at
        invasion.next_transition_at = None
        await session.flush()
        return InvasionResolutionResult(
            invasion_id=invasion.id,
            outcome=outcome,
            defense_total=defense_total,
            enemy_power=invasion.enemy_power,
            duplicate=False,
        )
