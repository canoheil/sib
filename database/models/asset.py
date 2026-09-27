"""Bot asset registry and generic user item inventory models."""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sib.database.base import Base, TimestampMixin


class BotAsset(Base, TimestampMixin):
    """A telegram file (photo/sticker/...) registered under a stable key.

    Features reference ``key`` (e.g. 'golden_apple'); replacing the
    artwork is a single row update - no code change.
    """

    __tablename__ = "bot_assets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(20), default="photo", nullable=False)
    file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<BotAsset key={self.key} type={self.type}>"


class UserItem(Base, TimestampMixin):
    """Generic per-user item inventory row (one row per item key)."""

    __tablename__ = "user_items"
    __table_args__ = (
        UniqueConstraint("user_id", "item_key", name="uq_user_items_user_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user = relationship("User")

    def __repr__(self) -> str:
        return f"<UserItem user={self.user_id} key={self.item_key} qty={self.quantity}>"
