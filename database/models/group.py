"""Group model."""

from typing import Optional
from sqlalchemy import BigInteger, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sib.database.base import Base, TimestampMixin


class Group(Base, TimestampMixin):
    """Group table storing Telegram groups/supergroups."""
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    stats = relationship("GroupStats", back_populates="group", uselist=False, cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Group id={self.id} title={self.title}>"
