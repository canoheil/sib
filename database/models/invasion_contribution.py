"""Contribution idempotency records for the Invasion domain."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class InvasionContribution(Base, TimestampMixin):
    """One accepted contribution request, unique within its Invasion.

    The aggregate counters remain on ``InvasionParticipant`` and
    ``InvasionWave``. This small record preserves the caller's stable key so a
    retried request can be returned without applying its amount twice.
    """

    __tablename__ = "invasion_contributions"
    __table_args__ = (
        UniqueConstraint(
            "invasion_id",
            "idempotency_key",
            name="uq_invasion_contributions_invasion_key",
        ),
        CheckConstraint("amount > 0", name="ck_invasion_contributions_positive_amount"),
        CheckConstraint("wave_sequence > 0", name="ck_invasion_contributions_wave_sequence"),
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
    wave_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<InvasionContribution invasion_id={self.invasion_id} "
            f"wave_sequence={self.wave_sequence} amount={self.amount}>"
        )
