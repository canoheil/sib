"""Idempotent Apple-reward claims for eligible Friendly matches."""

from datetime import date

from sqlalchemy import BigInteger, Date, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class FriendlyMatchReward(Base, TimestampMixin):
    """One Friendly reward claim per match and human participant.

    ``reward_date`` is explicitly the UTC calendar date used for daily caps
    and repeat-opponent checks; it does not depend on a database server's
    timezone or on the time the asynchronous ledger is later inspected.
    """

    __tablename__ = "friendly_match_rewards"
    __table_args__ = (
        UniqueConstraint("match_id", "user_id", name="uq_friendly_reward_match_user"),
        Index("ix_friendly_reward_user_date", "user_id", "reward_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("matches.id", ondelete="CASCADE", name="fk_friendly_reward_match"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_friendly_reward_user"),
        nullable=False,
        index=True,
    )
    reward_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
