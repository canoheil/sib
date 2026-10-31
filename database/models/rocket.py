"""Per-player cooldown state for Apple Rocket."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base


class UserRocketState(Base):
    """Last time a player started an Apple Rocket round."""

    __tablename__ = "user_rocket_states"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    last_played_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
