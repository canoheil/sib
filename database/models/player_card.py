"""Player card models.

Each character a player owns becomes a player card. The card carries ONLY
its own progression state: the ``level`` (1..10, see
``services/card_progression.py``). Its three attributes are never stored:
they are derived as ``Character base + 10 * (level - 1)`` so the shared
character definition stays the single source of the Level-1 values.

A player may own at most ONE card per character.

History: before migration 022 the card also stored its attributes and
``boosted_*`` counters (manual attribute allocation). That data is archived
in ``player_card_legacy_stats`` by the migration and no longer used.
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

    # Card level: 1 (new card) .. MAX_CARD_LEVEL. Changed only by
    # CardUpgradeService (one level at a time, compare-and-swap).
    level: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )

    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:
        return f"<PlayerCard user={self.user_id} char={self.character_id} L{self.level}>"


class PlayerCardLegacyStats(Base, TimestampMixin):
    """Archive of the retired manual-allocation data (written by migration 022).

    One row per card that existed when the upgrade system was replaced:
    the level and the stored/boosted attributes exactly as they were, so
    nothing a player ever had is lost. Read-only history, never used by
    gameplay.
    """

    __tablename__ = "player_card_legacy_stats"

    card_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    character_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    power: Mapped[int] = mapped_column(Integer, nullable=False)
    speed: Mapped[int] = mapped_column(Integer, nullable=False)
    intelligence: Mapped[int] = mapped_column(Integer, nullable=False)
    boosted_power: Mapped[int] = mapped_column(Integer, nullable=False)
    boosted_speed: Mapped[int] = mapped_column(Integer, nullable=False)
    boosted_intelligence: Mapped[int] = mapped_column(Integer, nullable=False)
