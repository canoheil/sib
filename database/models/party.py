"""Party and PartyMember models."""

from datetime import datetime
from typing import List, Optional
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin
from sib.database.models.match import MatchType


class PartyStatus:
    """Status values for a party."""
    OPEN = "OPEN"
    FULL = "FULL"
    IN_QUEUE = "IN_QUEUE"
    IN_MATCH = "IN_MATCH"
    CANCELLED = "CANCELLED"


class Party(Base, TimestampMixin):
    """Team party created inside a Telegram group before matchmaking.

    ``mode`` is the game mode (solo / 2v2 / 4v4) and describes ONE team's
    size. ``match_type`` decides how the party becomes a match:

    - NORMAL: ``max_players == team size``; the party queues for an opponent.
    - FRIENDLY: ``max_players == 2 x team size``; the party itself holds BOTH
      teams of one group-internal match (bots may fill the free slots) and
      starts without a queue.
    """
    __tablename__ = "parties"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    game_id: Mapped[int] = mapped_column(Integer, ForeignKey("games.id"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="solo", nullable=False)  # solo, 2v2, 4v4
    match_type: Mapped[str] = mapped_column(String(20), default=MatchType.NORMAL, nullable=False)  # normal, friendly
    max_players: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_players_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=PartyStatus.OPEN, nullable=False, index=True)
    creator_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)

    # Relationships
    group = relationship("Group")
    game = relationship("Game")
    creator = relationship("User", foreign_keys=[creator_user_id])
    members = relationship("PartyMember", back_populates="party", cascade="all, delete-orphan", order_by="PartyMember.joined_at")

    @property
    def remaining_slots(self) -> int:
        return max(0, self.max_players - self.current_players_count)

    @property
    def is_full(self) -> bool:
        return self.current_players_count >= self.max_players

    @property
    def is_friendly(self) -> bool:
        return self.match_type == MatchType.FRIENDLY

    @property
    def team_size(self) -> int:
        """Slots per team for this party's mode (friendly holds two teams)."""
        return max(1, self.max_players // 2) if self.is_friendly else max(1, self.max_players)

    def __repr__(self) -> str:
        return f"<Party id={self.id} game_id={self.game_id} mode={self.mode} status={self.status} players={self.current_players_count}/{self.max_players}>"


class PartyMember(Base, TimestampMixin):
    """Association table for party members."""
    __tablename__ = "party_members"
    __table_args__ = (
        UniqueConstraint("party_id", "user_id", name="uq_party_member"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    party_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("parties.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    # 1 or 2. Normal parties always use team 1 (the opposing party is team 2);
    # a friendly party splits its members over team 1 and team 2.
    team: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Per-bot configuration (difficulty/strategy/loadout) for slots filled by
    # the party bot-fill option - copied to MatchPlayer at match start.
    bot_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    party = relationship("Party", back_populates="members")
    user = relationship("User")

    def __repr__(self) -> str:
        return f"<PartyMember party_id={self.party_id} user_id={self.user_id}>"
