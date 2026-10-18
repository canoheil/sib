"""Apple friendship requests and accepted friendships."""

from sqlalchemy import BigInteger, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sib.database.base import Base, TimestampMixin


class FriendshipStatus:
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"


class AppleFriendship(Base, TimestampMixin):
    __tablename__ = "apple_friendships"
    __table_args__ = (
        Index("ix_apple_friendships_user_status", "user_id", "status"),
        Index("ix_apple_friendships_friend_status", "friend_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    friend_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FriendshipStatus.PENDING,
        server_default=FriendshipStatus.PENDING,
    )

    user = relationship("User", foreign_keys=[user_id])
    friend = relationship("User", foreign_keys=[friend_id])
