"""Adoptable pet companions (سیستم پت).

One row per owned pet. A pet starts as a baby, gains XP from feeding and
hunting, and evolves into its adult artwork once it reaches the configured XP
threshold. ``last_hunted_at`` backs the hunting cooldown, ``last_fed_at`` the
feeding rhythm; both are nullable because a freshly adopted pet has neither.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base

# SQLite only autoincrements INTEGER PRIMARY KEY; MariaDB keeps BIGINT AUTO_INCREMENT.
_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")

# The four species whose baby/adult artwork lives in ``assets/pets/``.
PET_TYPES = ("dog", "squirrel", "cat", "owl")


class UserPet(Base):
    """A single pet owned by a player."""

    __tablename__ = "user_pets"

    id: Mapped[int] = mapped_column(_ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "dog" | "squirrel" | "cat" | "owl" (see PET_TYPES).
    pet_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Stay NULL until the owner names the pet.
    custom_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_adult: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    xp: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    skill_level: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    last_fed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Hunting cooldown marker; NULL means the pet has never hunted.
    last_hunted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
