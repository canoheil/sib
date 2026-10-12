"""Character and balance-version models (Phase 9).

Characters are DATA, not code: name, lore, artwork, stats, passive,
price, release state, faction metadata and hidden combat modifiers all
live in the database and can be managed through the admin panel without
touching Python code.

Visible attributes (Power 💪 / Speed ⚡ / Intelligence 🧠) follow a total
attribute budget model at Level 1: the SUM of the three attributes is exactly
``services.card_progression.CARD_BASE_ATTRIBUTE_BUDGET`` (60) and it is a
permanent game rule, not configuration. Progression is deterministic and
additive: every level adds +10 to EACH attribute, so an owned card's total is
``60 + 30 * (level - 1)`` - 60 at Level 1 and 330 at Level 10.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base, TimestampMixin
from sib.database.models.shop import SHOP_CARD_PRICE_APPLES


class ReleaseStatus:
    """Lifecycle of a character's public availability."""

    DRAFT = "DRAFT"
    RELEASED = "RELEASED"
    RETIRED = "RETIRED"


class Character(Base, TimestampMixin):
    """A collectible fighter character (fully data-driven)."""

    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name_fa: Mapped[str] = mapped_column(String(120), nullable=False)
    name_en: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    lore_fa: Mapped[str] = mapped_column(Text, default="", nullable=False)
    artwork: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Visible attributes: the immutable Level-1 card definition. Their SUM
    # must be exactly the fixed budget (60) - see
    # ``services/card_progression.CARD_BASE_ATTRIBUTE_BUDGET``.
    power: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    speed: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    intelligence: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

    # Passive ability (key understood by the battle engine + params).
    passive_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    passive_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON

    # Hidden internal combat modifiers (never shown to players).
    hidden_modifiers: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON

    # Sidegrade metadata.
    strengths: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list (fa)
    weaknesses: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON list (fa)

    # Lore faction (nullable: a character may stand outside all factions).
    # Data-driven - gameplay never reads these fields.
    faction_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("factions.id"), nullable=True, index=True
    )
    # The character's role inside the faction («بانوی ماه», ...).
    faction_role_fa: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Economy / availability.
    # DEPRECATED: the per-card price. Every card costs the unified
    # ``SHOP_CARD_PRICE_APPLES`` in the shop; this column is kept
    # (normalized to that value by ``CharacterService`` and migration 033)
    # so existing installations and raw inserts stay schema-compatible and
    # nothing that ever stored a price row loses data.
    price: Mapped[int] = mapped_column(
        Integer, default=SHOP_CARD_PRICE_APPLES, nullable=False
    )
    # DEPRECATED: rarity is no longer a classification at all (cards are
    # sidegrades; the faction is the lore metadata now) and it has no effect
    # on pricing, the shop, filters, badges or labels. The column is kept,
    # with its historical values, so no existing installation loses data -
    # nothing in the codebase reads it, and CharacterService.update refuses
    # to write it (the server default only keeps raw inserts valid).
    rarity: Mapped[str] = mapped_column(String(20), default="COMMON", nullable=False)
    release_status: Mapped[str] = mapped_column(
        String(20), default=ReleaseStatus.DRAFT, nullable=False, index=True
    )
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    shop_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Current balance version (mirrors the newest snapshot row).
    balance_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # ------------------------------------------------------------------
    # JSON helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _dump(value: Any) -> Optional[str]:
        if value is None:
            return None
        import json

        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _load(value: Optional[str], default: Any = None) -> Any:
        if not value:
            return default
        import json

        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    @property
    def passive_params(self) -> Dict[str, Any]:
        return self._load(self.passive_data, {}) or {}

    @property
    def hidden_modifier_params(self) -> Dict[str, Any]:
        return self._load(self.hidden_modifiers, {}) or {}

    @property
    def strengths_list(self) -> list:
        return self._load(self.strengths, []) or []

    @property
    def weaknesses_list(self) -> list:
        return self._load(self.weaknesses, []) or []

    # Relationships
    faction = relationship("Faction", back_populates="characters")

    @property
    def total_base_stats(self) -> int:
        return self.power + self.speed + self.intelligence

    def stats_dict(self) -> Dict[str, int]:
        return {
            "power": self.power,
            "speed": self.speed,
            "intelligence": self.intelligence,
        }

    def snapshot(self) -> Dict[str, Any]:
        """Full balance-relevant snapshot for versioning.

        ``price`` is kept in the snapshot so historical rows stay
        interpretable; it is always the unified ``SHOP_CARD_PRICE_APPLES``
        for anything written from now on.
        """
        return {
            "code": self.code,
            "power": self.power,
            "speed": self.speed,
            "intelligence": self.intelligence,
            "passive_key": self.passive_key,
            "passive_data": self.passive_params,
            "hidden_modifiers": self.hidden_modifier_params,
            "price": self.price,
            "balance_version": self.balance_version,
        }

    def __repr__(self) -> str:
        return f"<Character {self.code} ({self.name_fa}) v{self.balance_version}>"


class CharacterBalanceVersion(Base, TimestampMixin):
    """Immutable snapshot of a character's balance-relevant data."""

    __tablename__ = "character_balance_versions"
    __table_args__ = (
        UniqueConstraint("character_id", "version", name="uq_character_balance_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<CharacterBalanceVersion char={self.character_id} v{self.version}>"
