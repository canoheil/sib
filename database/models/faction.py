"""Faction model (lore metadata for characters).

Factions are LORE: they describe who a character is in the world, never
how strong they are. The system is pure data - a new faction is a row in
`factions` plus a `faction_id` on the characters that belong to it; no
generic code path may branch on a faction code.

Rules encoded by the data layer (not by this module):
- a character belongs to at most one faction (`faction_id` nullable);
- `faction_role_fa` holds the character's role INSIDE that faction
  (e.g. «قوی‌ترین جنگجوی امپراتوری»);
- `display_order` is the display order; `is_active` hides retired factions.
"""

from typing import List, Optional

from sqlalchemy import BigInteger, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sib.database.base import Base, TimestampMixin


class Faction(Base, TimestampMixin):
    """A lore faction (Empire, Sages, Moon Descendants, ...)."""

    __tablename__ = "factions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name_fa: Mapped[str] = mapped_column(String(120), nullable=False)
    name_en: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # Lore description (free text, Persian).
    lore_fa: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Home territory, when the faction has one (e.g. درّه ماه).
    territory_fa: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    # Emoji shown next to the faction name.
    symbol: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Display order in listings (index, wiki, ...).
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    characters: Mapped[List["Character"]] = relationship(  # noqa: F821
        "Character", back_populates="faction"
    )

    def __repr__(self) -> str:
        return f"<Faction {self.code} ({self.name_fa})>"
