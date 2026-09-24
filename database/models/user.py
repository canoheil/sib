"""User model."""

from typing import Optional
from sqlalchemy import BigInteger, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column
from sib.database.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """User table storing Telegram user data."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    language_code: Mapped[str] = mapped_column(String(10), default="fa", nullable=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def display_name(self) -> str:
        """Friendly display name for Telegram messages."""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name or self.username or "بازیکن بی‌نام"

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username}>"
