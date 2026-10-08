"""Persistent Invasion event, wave, and participant records.

Invasions are an independent domain. Main Card fields on a participant are
conditional historical snapshots, not links to mutable card/character rows.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sib.database.base import Base, TimestampMixin


class InvasionStatus:
    """Persisted lifecycle vocabulary for invasions and their waves."""

    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    FINISHED = "FINISHED"

    ALL = (SCHEDULED, ACTIVE, FINISHED)


class InvasionOutcome:
    """Approved terminal outcomes; no outcome calculation is implemented here."""

    TOTAL_VICTORY = "TOTAL_VICTORY"
    NARROW_DEFENSE = "NARROW_DEFENSE"
    DEFEAT = "DEFEAT"

    ALL = (TOTAL_VICTORY, NARROW_DEFENSE, DEFEAT)


class Invasion(Base, TimestampMixin):
    """One group-scoped Invasion event and its durable configuration snapshot."""

    __tablename__ = "invasions"
    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_invasions_occurrence_key"),
        CheckConstraint(
            "status IN ('SCHEDULED', 'ACTIVE', 'FINISHED')",
            name="ck_invasions_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('TOTAL_VICTORY', 'NARROW_DEFENSE', 'DEFEAT')",
            name="ck_invasions_outcome",
        ),
        Index("ix_invasions_group_status", "group_id", "status"),
        Index(
            "ix_invasions_status_next_transition_at",
            "status",
            "next_transition_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        default=InvasionStatus.SCHEDULED,
        server_default=InvasionStatus.SCHEDULED,
        nullable=False,
    )

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_transition_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_wave_sequence: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    outcome: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    announcement_message_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )

    # Optional stable region metadata only; no Region/Territory entity is implied.
    target_region_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_region_name_snapshot: Mapped[Optional[str]] = mapped_column(
        String(160), nullable=True
    )

    # Faction code and power are copied event values, not live faction links.
    enemy_faction_code: Mapped[str] = mapped_column(String(64), nullable=False)
    enemy_power: Mapped[int] = mapped_column(Integer, nullable=False)
    # JSON text snapshots (repository convention): event difficulty/configuration
    # is required; a deterministic seed or resolved event specification is optional.
    event_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    event_spec_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    group = relationship("Group")
    waves: Mapped[List["InvasionWave"]] = relationship(
        "InvasionWave",
        back_populates="invasion",
        cascade="all, delete-orphan",
        order_by="InvasionWave.wave_sequence",
    )
    participants: Mapped[List["InvasionParticipant"]] = relationship(
        "InvasionParticipant",
        back_populates="invasion",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Invasion id={self.id} group_id={self.group_id} status={self.status}>"


class InvasionWave(Base, TimestampMixin):
    """One ordered wave with its own persisted enemy-composition snapshot."""

    __tablename__ = "invasion_waves"
    __table_args__ = (
        UniqueConstraint(
            "invasion_id", "wave_sequence", name="uq_invasion_waves_invasion_sequence"
        ),
        CheckConstraint(
            "status IN ('SCHEDULED', 'ACTIVE', 'FINISHED')",
            name="ck_invasion_waves_status",
        ),
        Index(
            "ix_invasion_waves_status_next_transition_at",
            "status",
            "next_transition_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invasion_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("invasions.id", ondelete="CASCADE"),
        nullable=False,
    )
    wave_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        default=InvasionStatus.SCHEDULED,
        server_default=InvasionStatus.SCHEDULED,
        nullable=False,
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_transition_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    enemy_composition_json: Mapped[str] = mapped_column(Text, nullable=False)
    contribution_total: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )

    invasion: Mapped["Invasion"] = relationship("Invasion", back_populates="waves")

    def __repr__(self) -> str:
        return (
            f"<InvasionWave invasion_id={self.invasion_id} "
            f"sequence={self.wave_sequence} status={self.status}>"
        )


class InvasionParticipant(Base, TimestampMixin):
    """A unique event participant with an optional, denormalized Main Card snapshot."""

    __tablename__ = "invasion_participants"
    __table_args__ = (
        UniqueConstraint(
            "invasion_id", "user_id", name="uq_invasion_participants_invasion_user"
        ),
        CheckConstraint(
            "(main_card_id_snapshot IS NULL "
            "AND character_id_snapshot IS NULL "
            "AND character_code_snapshot IS NULL "
            "AND character_name_fa_snapshot IS NULL "
            "AND character_name_en_snapshot IS NULL "
            "AND character_artwork_snapshot IS NULL "
            "AND card_level_snapshot IS NULL "
            "AND power_snapshot IS NULL "
            "AND speed_snapshot IS NULL "
            "AND intelligence_snapshot IS NULL "
            "AND character_balance_version_snapshot IS NULL) "
            "OR (main_card_id_snapshot IS NOT NULL "
            "AND character_id_snapshot IS NOT NULL "
            "AND character_code_snapshot IS NOT NULL "
            "AND character_name_fa_snapshot IS NOT NULL "
            "AND card_level_snapshot IS NOT NULL "
            "AND power_snapshot IS NOT NULL "
            "AND speed_snapshot IS NOT NULL "
            "AND intelligence_snapshot IS NOT NULL "
            "AND character_balance_version_snapshot IS NOT NULL)",
            name="ck_invasion_participants_main_card_snapshot_complete",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invasion_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("invasions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    contribution_total: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )

    # These are intentionally scalar snapshots without PlayerCard/Character FKs.
    # A participant may have no Main Card; then every snapshot field is NULL.
    main_card_id_snapshot: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    character_id_snapshot: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    character_code_snapshot: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    character_name_fa_snapshot: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
    character_name_en_snapshot: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
    character_artwork_snapshot: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    card_level_snapshot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    power_snapshot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    speed_snapshot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    intelligence_snapshot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    character_balance_version_snapshot: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    invasion: Mapped["Invasion"] = relationship(
        "Invasion", back_populates="participants"
    )
    user = relationship("User")

    def __repr__(self) -> str:
        return (
            f"<InvasionParticipant invasion_id={self.invasion_id} "
            f"user_id={self.user_id}>"
        )
