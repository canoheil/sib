"""Marriage, MarriageProposal, and FlowerGift models.

Domain rules supported by the persistence layer:
- Marriage is between two real users stored in canonical ascending order
  (``user1_id < user2_id``), while also preserving ``proposer_id`` and
  ``accepter_id``.
- Marriage is global after creation (``origin_group_id`` is historical
  metadata only) and is NEVER deleted on divorce; divorce transitions
  ``status`` to ``DIVORCED``, resets ``love_score`` to ``0``, and sets
  ``cooldown_until`` (24h remarriage cooldown for both spouses).
- ``love_score`` (0..100, initial 0) belongs to the shared ``Marriage``
  entity, never to an individual user.
- ``MarriageProposal`` is group-scoped with a 5-minute lifetime and
  tracks statuses ``PENDING``, ``ACCEPTED``, ``REJECTED``, ``EXPIRED``,
  ``INVALIDATED``, and ``FAILED``.
- ``FlowerGift`` records spouse-to-spouse flower gifts and supports
  auditing the directional daily limit (1 from A -> B and 1 from B -> A
  per UTC day) as well as daily Love decay checks.
"""

from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sib.database.base import Base, TimestampMixin


class MarriageStatus:
    """Lifecycle states of a Marriage record."""

    ACTIVE = "ACTIVE"
    DIVORCED = "DIVORCED"

    ALL = (ACTIVE, DIVORCED)


class ProposalStatus:
    """Lifecycle states of a MarriageProposal."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    FAILED = "FAILED"

    TERMINAL = frozenset({ACCEPTED, REJECTED, EXPIRED, INVALIDATED, FAILED})
    ALL = (PENDING, ACCEPTED, REJECTED, EXPIRED, INVALIDATED, FAILED)


class FlowerGiftStatus:
    """Lifecycle states of a FlowerGift."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"

    TERMINAL = frozenset({ACCEPTED, REJECTED, INVALIDATED, EXPIRED})
    ALL = (PENDING, ACCEPTED, REJECTED, INVALIDATED, EXPIRED)


def canonical_spouse_ids(user_a: int, user_b: int) -> Tuple[int, int]:
    """Return ``(user1_id, user2_id)`` in canonical ascending order.

    Raises ``ValueError`` if both IDs refer to the same user.
    """
    a = int(user_a)
    b = int(user_b)
    if a == b:
        raise ValueError("A user cannot marry themselves (user1_id == user2_id).")
    return (a, b) if a < b else (b, a)


class MarriageProposal(Base, TimestampMixin):
    """Group-scoped marriage proposal with a 5-minute lifetime."""

    __tablename__ = "marriage_proposals"
    __table_args__ = (
        Index("ix_marriage_proposals_proposer_status", "proposer_id", "status"),
        Index("ix_marriage_proposals_target_status", "target_id", "status"),
        Index("ix_marriage_proposals_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    proposer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20),
        default=ProposalStatus.PENDING,
        server_default=ProposalStatus.PENDING,
        nullable=False,
        index=True,
    )
    invalid_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    proposer = relationship("User", foreign_keys=[proposer_id])
    target = relationship("User", foreign_keys=[target_id])
    group = relationship("Group", foreign_keys=[group_id])

    @property
    def is_pending(self) -> bool:
        return self.status == ProposalStatus.PENDING

    @property
    def is_terminal(self) -> bool:
        return self.status in ProposalStatus.TERMINAL

    @property
    def proposal(self) -> "MarriageProposal":
        """Self-reference so callers expecting a wrapper result can access ``.proposal``."""
        return self

    @property
    def proposer_display_name(self) -> str:
        if self.proposer is not None and getattr(self.proposer, "display_name", None):
            return self.proposer.display_name
        return str(self.proposer_id)

    @property
    def target_display_name(self) -> str:
        if self.target is not None and getattr(self.target, "display_name", None):
            return self.target.display_name
        return str(self.target_id)

    def __repr__(self) -> str:
        return (
            f"<MarriageProposal id={self.id} proposer={self.proposer_id} "
            f"target={self.target_id} status={self.status}>"
        )


class Marriage(Base, TimestampMixin):
    """Persistent marriage relationship and shared Sib Love state.

    ``user1_id`` and ``user2_id`` are always stored in canonical ascending
    order (``user1_id < user2_id``). Records are preserved across divorce
    by transitioning ``status`` to ``DIVORCED``.
    """

    __tablename__ = "marriages"
    __table_args__ = (
        Index("ix_marriages_user1_status", "user1_id", "status"),
        Index("ix_marriages_user2_status", "user2_id", "status"),
        Index("ix_marriages_status_last_decay_date", "status", "last_decay_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user1_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user2_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proposer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    accepter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    origin_group_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    proposal_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("marriage_proposals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default=MarriageStatus.ACTIVE,
        server_default=MarriageStatus.ACTIVE,
        nullable=False,
        index=True,
    )

    # Shared relationship metric (0..100, initial 0, reset to 0 on divorce).
    love_score: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_flower_accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_flower_accepted_date: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True
    )
    last_decay_date: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True, index=True
    )

    married_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    divorced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    divorced_by_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Relationships
    user1 = relationship("User", foreign_keys=[user1_id])
    user2 = relationship("User", foreign_keys=[user2_id])
    proposer = relationship("User", foreign_keys=[proposer_id])
    accepter = relationship("User", foreign_keys=[accepter_id])
    divorced_by = relationship("User", foreign_keys=[divorced_by_user_id])
    origin_group = relationship("Group", foreign_keys=[origin_group_id])
    proposal = relationship("MarriageProposal", foreign_keys=[proposal_id])
    flower_gifts: Mapped[List["FlowerGift"]] = relationship(
        "FlowerGift", back_populates="marriage", cascade="all, delete-orphan"
    )

    def __init__(self, **kwargs):
        u1 = kwargs.get("user1_id", kwargs.get("proposer_id"))
        u2 = kwargs.get("user2_id", kwargs.get("accepter_id"))
        if u1 is not None and u2 is not None:
            c1, c2 = canonical_spouse_ids(u1, u2)
            kwargs["user1_id"] = c1
            kwargs["user2_id"] = c2
        super().__init__(**kwargs)

    @property
    def is_active(self) -> bool:
        return self.status == MarriageStatus.ACTIVE

    @property
    def is_divorced(self) -> bool:
        return self.status == MarriageStatus.DIVORCED

    def includes_user(self, user_id: int) -> bool:
        return user_id in (self.user1_id, self.user2_id)

    def spouse_of(self, user_id: int) -> Optional[int]:
        if user_id == self.user1_id:
            return self.user2_id
        if user_id == self.user2_id:
            return self.user1_id
        return None

    def __repr__(self) -> str:
        return (
            f"<Marriage id={self.id} users=({self.user1_id}, {self.user2_id}) "
            f"status={self.status} love={self.love_score}>"
        )


class FlowerGift(Base, TimestampMixin):
    """Spouse-to-spouse flower gift offer and directional daily quota record."""

    __tablename__ = "flower_gifts"
    __table_args__ = (
        Index(
            "ix_flower_gifts_marriage_date_sender_status",
            "marriage_id",
            "gift_date",
            "sender_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    marriage_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("marriages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    receiver_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # UTC calendar date ("YYYY-MM-DD") for directional daily limit & decay checks.
    gift_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(20),
        default=FlowerGiftStatus.PENDING,
        server_default=FlowerGiftStatus.PENDING,
        nullable=False,
        index=True,
    )
    love_delta: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    marriage: Mapped["Marriage"] = relationship("Marriage", back_populates="flower_gifts")
    sender = relationship("User", foreign_keys=[sender_id])
    receiver = relationship("User", foreign_keys=[receiver_id])

    @property
    def is_pending(self) -> bool:
        return self.status == FlowerGiftStatus.PENDING

    @property
    def is_terminal(self) -> bool:
        return self.status in FlowerGiftStatus.TERMINAL

    @property
    def gift(self) -> "FlowerGift":
        """Self-reference so callers expecting a wrapper can access ``.gift``."""
        return self

    @property
    def sender_display_name(self) -> str:
        if self.sender is not None and getattr(self.sender, "display_name", None):
            return self.sender.display_name
        return str(self.sender_id)

    @property
    def receiver_display_name(self) -> str:
        if self.receiver is not None and getattr(self.receiver, "display_name", None):
            return self.receiver.display_name
        return str(self.receiver_id)

    def __repr__(self) -> str:
        return (
            f"<FlowerGift id={self.id} marriage={self.marriage_id} "
            f"{self.sender_id}->{self.receiver_id} date={self.gift_date} status={self.status}>"
        )
