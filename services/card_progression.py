"""Card progression rules (pure, no I/O).

The card model is split in three layers so future cards need NO special
upgrade logic:

1. **Base definition** - ``Character.power/speed/intelligence`` are the
   immutable Level-1 attributes of a card type. They are never changed by
   a player's upgrade.
2. **Owned progression** - ``PlayerCard.level`` (1..``MAX_CARD_LEVEL``) is the
   only progression state a player's own card carries.
3. **Derived stats** - a card's attributes are ALWAYS computed as
   ``base + ATTRIBUTE_STEP * (level - 1)`` for each of the three attributes.
   No random gains, no player-chosen distribution, no stored copies that can
   drift from the level.

The rules here are global (same range, same step for every card) and are
CODE, not configuration: the Level-1 budget (``CARD_BASE_ATTRIBUTE_BUDGET``)
and the per-level step (``ATTRIBUTE_STEP``) cannot be overridden at runtime.
The upgrade COST table is separate data: see ``services/card_upgrade.py`` and
``config_store.CONFIG_DEFAULTS["card_upgrade_costs"]``.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

MIN_CARD_LEVEL = 1
MAX_CARD_LEVEL = 10
# Every successful upgrade adds exactly this many points to EACH attribute.
ATTRIBUTE_STEP = 10

# The permanent Level-1 attribute budget: Power + Speed + Intelligence of
# EVERY character definition equals exactly this. It is a GAME RULE, not
# configuration - it lives here (with the rest of the progression rules) and
# cannot be overridden at runtime: there is no `card_base_attribute_budget`
# system_config key any more (see CharacterService.validate_base_budget).
CARD_BASE_ATTRIBUTE_BUDGET = 60

# The exactly-3 card attributes: (key, emoji, Persian label). The keys are
# the ``Character`` column names; the labels are the ones players already see.
CARD_ATTRIBUTES: Tuple[Tuple[str, str, str], ...] = (
    ("power", "💪", "قدرت"),
    ("speed", "⚡", "سرعت"),
    ("intelligence", "🧠", "هوش"),
)
ATTRIBUTE_KEYS: Tuple[str, ...] = tuple(key for key, _emoji, _label in CARD_ATTRIBUTES)


@dataclass(frozen=True)
class CardAttributes:
    """The three attributes of a card at a given level (immutable)."""

    power: int
    speed: int
    intelligence: int

    @property
    def total(self) -> int:
        return self.power + self.speed + self.intelligence

    def as_dict(self) -> Dict[str, int]:
        return {key: getattr(self, key) for key in ATTRIBUTE_KEYS}

    def plus(self, amount: int) -> "CardAttributes":
        """A copy with ``amount`` added to every attribute."""
        return CardAttributes(
            power=self.power + amount,
            speed=self.speed + amount,
            intelligence=self.intelligence + amount,
        )


def clamp_level(level: Any) -> int:
    """Coerce any stored level into the valid range (defensive reads)."""
    try:
        value = int(level)
    except (TypeError, ValueError):
        return MIN_CARD_LEVEL
    return max(MIN_CARD_LEVEL, min(MAX_CARD_LEVEL, value))


def is_max_level(level: int) -> bool:
    return int(level) >= MAX_CARD_LEVEL


def next_level(level: int) -> Optional[int]:
    """The level after ``level`` (always exactly +1), or None at the maximum."""
    return None if is_max_level(level) else int(level) + 1


def base_attributes(character: Any) -> CardAttributes:
    """Level-1 attributes of a card type (read from its definition)."""
    return CardAttributes(
        power=int(character.power),
        speed=int(character.speed),
        intelligence=int(character.intelligence),
    )


def attributes_at_level(base: CardAttributes, level: int) -> CardAttributes:
    """Deterministic attributes of a card with ``base`` at ``level``."""
    return base.plus(ATTRIBUTE_STEP * (clamp_level(level) - MIN_CARD_LEVEL))


def card_attributes(card: Any, character: Any) -> CardAttributes:
    """Attributes of an owned ``PlayerCard`` (its level + its character's base)."""
    return attributes_at_level(base_attributes(character), card.level)
