"""Ad Campaign model for post-match monetization."""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from goje.database.base import Base, TimestampMixin


class FrequencyMode:
    EVERY_MATCH = "every_match"
    EVERY_2_MATCHES = "every_2_matches"
    EVERY_3_MATCHES = "every_3_matches"
    SMART = "smart"

    CHOICES = [EVERY_MATCH, EVERY_2_MATCHES, EVERY_3_MATCHES, SMART]


class AdCampaign(Base, TimestampMixin):
    """Ad Campaign supporting post-match monetization."""
    __tablename__ = "ad_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sponsor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    campaign_name: Mapped[str] = mapped_column(String(255), nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    ad_text: Mapped[str] = mapped_column(Text, nullable=False)
    cta_label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    target_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    frequency_mode: Mapped[str] = mapped_column(String(50), default=FrequencyMode.SMART, nullable=False)
    
    max_impressions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    impressions_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Optional targeting
    target_game_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("games.id", ondelete="SET NULL"), nullable=True
    )
    target_group_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    target_game = relationship("Game")
    target_group = relationship("Group")

    @property
    def ctr(self) -> float:
        """Click-Through-Rate as percentage."""
        if self.impressions_count <= 0:
            return 0.0
        return round((self.clicks_count / self.impressions_count) * 100.0, 2)

    @property
    def is_expired(self) -> bool:
        """Check if campaign is outside its scheduled date range."""
        now = datetime.now(timezone.utc)
        if self.start_date and now < self.start_date:
            return True
        if self.end_date and now > self.end_date:
            return True
        return False

    @property
    def is_capped(self) -> bool:
        """Check if campaign reached its impression limit."""
        if self.max_impressions is not None and self.impressions_count >= self.max_impressions:
            return True
        return False

    def is_eligible(
        self,
        game_id: Optional[int] = None,
        group_id: Optional[int] = None,
        match_number: int = 1,
    ) -> bool:
        """Determine if campaign is eligible for display."""
        if not self.is_active or self.is_expired or self.is_capped:
            return False

        if self.target_game_id is not None and game_id is not None and self.target_game_id != game_id:
            return False

        if self.target_group_id is not None and group_id is not None and self.target_group_id != group_id:
            return False

        # Frequency check
        if self.frequency_mode == FrequencyMode.EVERY_MATCH:
            return True
        elif self.frequency_mode == FrequencyMode.EVERY_2_MATCHES:
            return (match_number % 2 == 0)
        elif self.frequency_mode == FrequencyMode.EVERY_3_MATCHES:
            return (match_number % 3 == 0)
        elif self.frequency_mode == FrequencyMode.SMART:
            # Smart frequency: display every 2 matches or always if high priority
            return (self.priority >= 10 or match_number % 2 == 0)

        return True

    def __repr__(self) -> str:
        return f"<AdCampaign id={self.id} name='{self.campaign_name}' sponsor='{self.sponsor_name}' active={self.is_active}>"
