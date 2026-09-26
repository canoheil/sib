"""Player card models (Phase 10).

Each character a player owns becomes a player card with a LEVEL and an
attribute allocation. The total attribute budget is determined ONLY by
the level (never by the character): 60/120/180/240/300 for levels 1-5
and extensible beyond 5 via configuration.

A player may own at most ONE card per character.
"""

from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin


class PlayerCard(Base, TimestampMixin):
    """A player's owned instance of a character."""

    __tablename__ = "player_cards"
    __table_args__ = (
        UniqueConstraint("user_id", "character_id", name="uq_player_card_user_character"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    character_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)

    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Attribute allocation — the SUM equals the level budget.
    power: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    speed: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    intelligence: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

    # Which attributes the player chose to boost during upgrades.
    boosted_power: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    boosted_speed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    boosted_intelligence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:
        return f"<PlayerCard user={self.user_id} char={self.character_id} L{self.level}>"
