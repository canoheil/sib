"""Match model."""

from datetime import datetime
from typing import Optional, List
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin


class MatchState:
    """Standard state machine states for matches."""
    CREATED = "CREATED"
    WAITING_FOR_OPPONENT = "WAITING_FOR_OPPONENT"
    OPPONENT_FOUND = "OPPONENT_FOUND"
    WAITING_FOR_ACCEPTANCE = "WAITING_FOR_ACCEPTANCE"
    ACTIVE = "ACTIVE"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"


class Match(Base, TimestampMixin):
    """Match model representing a game session between two groups/teams."""
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(Integer, ForeignKey("games.id"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="solo", nullable=False)  # solo, 2v2, 4v4
    state: Mapped[str] = mapped_column(String(50), default=MatchState.CREATED, nullable=False, index=True)

    group1_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("groups.id"), nullable=True, index=True)
    group2_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("groups.id"), nullable=True, index=True)
    party1_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("parties.id"), nullable=True)
    party2_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("parties.id"), nullable=True)
    group1_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    group2_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    
    winner_group_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("groups.id"), nullable=True)
    is_draw: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    # Persistent serialized state of the active game
    game_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Turn tracking
    current_turn_player_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    turn_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Acceptance tracking
    group1_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    group2_accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    acceptance_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Rating application tracking (idempotency)
    rating_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    group1_rating_change: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    group2_rating_change: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Post-match monetization
    ad_campaign_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("ad_campaigns.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    game = relationship("Game")
    group1 = relationship("Group", foreign_keys=[group1_id])
    group2 = relationship("Group", foreign_keys=[group2_id])
    winner_group = relationship("Group", foreign_keys=[winner_group_id])
    players = relationship("MatchPlayer", back_populates="match", cascade="all, delete-orphan")
    ad_campaign = relationship("AdCampaign")

    def __repr__(self) -> str:
        return f"<Match id={self.id} game_id={self.game_id} state={self.state}>"
