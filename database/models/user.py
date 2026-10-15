"""User model."""

from typing import Optional
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, String, DateTime, ForeignKey, false
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
    # Presentation preference only; the profile-skin service normalizes any
    # unsupported legacy/imported value back to Green Apple at read time.
    profile_skin: Mapped[str] = mapped_column(
        String(32),
        default="green_apple",
        server_default="green_apple",
        nullable=False,
    )

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    banned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    banned_by: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    ban_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    @property
    def display_name(self) -> str:
        """Friendly display name for Telegram messages."""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name or self.username or "بازیکن بی‌نام"

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username}>"
