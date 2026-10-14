"""Recently observed, group-scoped participants for invasion v2 power reads."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin

_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class Inv2GroupMember(Base, TimestampMixin):
    __tablename__ = "inv2_group_members"
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", name="uq_inv2_group_members_user_group"),
        Index("ix_inv2_group_members_group_last_seen", "group_id", "last_seen_at"),
        Index("ix_inv2_group_members_group_id", "group_id"),
        Index("ix_inv2_group_members_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", name="fk_inv2_group_members_user"), nullable=False
    )
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.id", name="fk_inv2_group_members_group"), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
