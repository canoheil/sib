"""Game model."""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from goje.database.base import Base, TimestampMixin


class Game(Base, TimestampMixin):
    """Game metadata table."""
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    name_fa: Mapped[str] = mapped_column(String(100), nullable=False)
    description_fa: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_solo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_2v2: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_4v4: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    turn_timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)

    def __repr__(self) -> str:
        return f"<Game code={self.code} name_fa={self.name_fa}>"
