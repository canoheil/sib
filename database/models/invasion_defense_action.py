"""Auditable V1 defense-action records for persistent Invasions."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class InvasionDefenseAction(Base, TimestampMixin):
    """One accepted gameplay action linked to its existing contribution row.

    This table records action identity and calculation inputs/results; it is
    not a second contribution ledger. Contribution aggregates and the
    idempotency record remain in ``invasion_contributions``.
    """

    __tablename__ = "invasion_defense_actions"
    __table_args__ = (
        UniqueConstraint(
            "invasion_id",
            "idempotency_key",
            name="uq_invasion_defense_actions_invasion_key",
        ),
        UniqueConstraint(
            "contribution_id",
            name="uq_invasion_defense_actions_contribution",
        ),
        # At most one defense action per participant per Wave: enforced by the
        # database, so simultaneous callbacks cannot both commit.
        UniqueConstraint(
            "participant_id",
            "wave_id",
            "action_code",
            name="uq_invasion_defense_actions_participant_wave_action",
        ),
        CheckConstraint(
            "wave_sequence > 0",
            name="ck_invasion_defense_actions_wave_sequence",
        ),
        CheckConstraint(
            "action_version > 0",
            name="ck_invasion_defense_actions_version",
        ),
        CheckConstraint(
            "contribution_amount > 0",
            name="ck_invasion_defense_actions_positive_contribution",
        ),
        CheckConstraint(
            "power_snapshot_used IS NULL OR power_snapshot_used >= 0",
            name="ck_invasion_defense_actions_power_snapshot",
        ),
        CheckConstraint(
            "participant_total_after >= 0 AND wave_total_after >= 0",
            name="ck_invasion_defense_actions_totals",
        ),
        Index(
            "ix_invasion_defense_actions_participant_created",
            "participant_id",
            "created_at",
        ),
        Index(
            "ix_invasion_defense_actions_wave_created",
            "wave_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invasion_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("invasions.id", ondelete="CASCADE"),
        nullable=False,
    )
    participant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("invasion_participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    wave_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("invasion_waves.id", ondelete="CASCADE"),
        nullable=False,
    )
    contribution_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("invasion_contributions.id", ondelete="CASCADE"),
        nullable=False,
    )
    wave_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action_code: Mapped[str] = mapped_column(String(32), nullable=False)
    action_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    power_snapshot_used: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    contribution_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    participant_total_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wave_total_after: Mapped[int] = mapped_column(BigInteger, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<InvasionDefenseAction id={self.id} invasion_id={self.invasion_id} "
            f"participant_id={self.participant_id} code={self.action_code!r} "
            f"wave_sequence={self.wave_sequence} amount={self.contribution_amount}>"
        )
