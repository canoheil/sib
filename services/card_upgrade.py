"""Card upgrade service (business rules only - no Telegram code).

Rules (global, identical for every card):

* a card has a level 1..10 and is upgraded exactly ONE level at a time;
* every upgrade adds +10 to each of the 3 attributes (see
  ``services/card_progression.py``) - no choice, no randomness;
* every upgrade costs normal Apples (wallet) + ONE special-apple material
  (Golden -> Diamond -> Obsidian as the level rises). The table is DATA:
  ``ConfigService`` key ``card_upgrade_costs`` (defaults in
  ``services/config_store.py``), one row per step, never hardcoded here.

Atomicity / concurrency (``CardUpgradeService.upgrade``), in this order:

1. lock the card row (``SELECT ... FOR UPDATE``) and verify ownership;
2. verify level < max and - when the caller passes ``expected_level`` (the
   screen the player pressed) - that the card is still at that level, so a
   double tap / replayed callback can never buy a second level;
3. lock the special-apple inventory row, then the wallet row (same order as
   the shop sell flow -> no lock-order cycle) and verify ALL resources;
4. only then mutate: compare-and-swap the level (``UPDATE ... WHERE level =
   :expected``, protects even without row locks), spend Apples (ledger row),
   consume the special apples;
5. the caller's ``get_session()`` commits; any failure after step 4 rolls the
   whole transaction back, so money, items and level always move together.

Insufficient resources are reported as an exact shortfall and change nothing.
"""

from dataclasses import dataclass
from typing import Any, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import AppleBattleQueueEntry, Character, PlayerCard
from sib.services.apple_wallet import AppleWalletService
from sib.services.card_progression import (
    ATTRIBUTE_STEP,
    MAX_CARD_LEVEL,
    MIN_CARD_LEVEL,
    CardAttributes,
    attributes_at_level,
    base_attributes,
    card_attributes,
    is_max_level,
)
from sib.services.config_store import ConfigService
from sib.services.items import ITEM_CATALOG, ItemService
from sib.services.player_cards import CardError

LEDGER_REASON = "card_upgrade"
# NOTE: a different namespace than the retired manual-upgrade system
# (``card_upgrade:<card>:<level>``): its ledger rows still exist and an
# idempotent spend would silently skip charging on a key collision.
LEDGER_KEY_FORMAT = "card_upgrade_v2:{card_id}:{to_level}"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class CardUpgradeError(CardError):
    """Base class of every upgrade refusal (nothing was changed)."""


class CardNotOwnedError(CardUpgradeError):
    def __init__(self) -> None:
        super().__init__("کارت موردنظر متعلق به شما نیست.")


class CardMaxLevelError(CardUpgradeError):
    def __init__(self) -> None:
        super().__init__(f"این کارت به بیشترین سطح (سطح {MAX_CARD_LEVEL}) رسیده است.")


class StaleUpgradeError(CardUpgradeError):
    """The card is no longer at the level the pressed screen showed."""

    def __init__(self, current_level: int) -> None:
        self.current_level = current_level
        super().__init__("سطح کارت تغییر کرده؛ صفحه ارتقا به‌روز شد.")


class InvalidUpgradeCostTable(CardUpgradeError):
    def __init__(self, detail: str) -> None:
        super().__init__(f"جدول هزینه ارتقا نامعتبر است: {detail}")


class InsufficientResourcesError(CardUpgradeError):
    def __init__(self, cost: "UpgradeCost", missing: "MissingResources") -> None:
        self.cost = cost
        self.missing = missing
        super().__init__("منابع کافی برای ارتقا نداری.")


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UpgradeCost:
    """Cost of ONE upgrade step: Apples + one special-apple material."""

    apples: int
    item_key: str
    item_quantity: int


@dataclass(frozen=True)
class MissingResources:
    """How much of each resource is still missing (0 = enough)."""

    apples: int
    item_key: str
    item_quantity: int

    @property
    def is_empty(self) -> bool:
        return self.apples <= 0 and self.item_quantity <= 0


@dataclass(frozen=True)
class UpgradePreview:
    """Everything the upgrade screen needs, computed from one read."""

    card_id: int
    character_name: str
    level: int
    next_level: Optional[int]
    current: CardAttributes
    after: Optional[CardAttributes]
    cost: Optional[UpgradeCost]
    apple_balance: int
    item_balance: int
    missing: Optional[MissingResources]

    @property
    def is_max_level(self) -> bool:
        return self.next_level is None

    @property
    def can_afford(self) -> bool:
        return self.missing is not None and self.missing.is_empty


@dataclass(frozen=True)
class UpgradeResult:
    card_id: int
    from_level: int
    to_level: int
    before: CardAttributes
    after: CardAttributes
    cost: UpgradeCost


def missing_for(cost: UpgradeCost, apples: int, item_quantity: int) -> MissingResources:
    """Exact shortfall (never negative) for the given holdings."""
    return MissingResources(
        apples=max(0, cost.apples - max(0, apples)),
        item_key=cost.item_key,
        item_quantity=max(0, cost.item_quantity - max(0, item_quantity)),
    )


# ---------------------------------------------------------------------------
# Cost table (data-driven)
# ---------------------------------------------------------------------------
def parse_cost_table(raw: Any) -> List[UpgradeCost]:
    """Validate the configured table: one row per step (levels 1->2 .. 9->10)."""
    steps = MAX_CARD_LEVEL - MIN_CARD_LEVEL
    if not isinstance(raw, list) or len(raw) != steps:
        raise InvalidUpgradeCostTable(f"باید دقیقاً {steps} ردیف باشد")
    table: List[UpgradeCost] = []
    for index, row in enumerate(raw):
        try:
            cost = UpgradeCost(
                apples=int(row["apples"]),
                item_key=str(row["item"]),
                item_quantity=int(row["quantity"]),
            )
        except (KeyError, TypeError, ValueError):
            raise InvalidUpgradeCostTable(f"ردیف {index + 1} ناقص است") from None
        if cost.apples < 1 or cost.item_quantity < 1 or cost.item_key not in ITEM_CATALOG:
            raise InvalidUpgradeCostTable(f"ردیف {index + 1} مقدار یا آیتم نامعتبر دارد")
        table.append(cost)
    return table


class CardUpgradeService:
    """Preview and perform card upgrades."""

    # ------------------------------------------------------------------
    # Costs
    # ------------------------------------------------------------------
    @classmethod
    async def cost_table(cls, session: AsyncSession) -> List[UpgradeCost]:
        """The 9-row cost table. A broken override falls back to the defaults
        (logged) so a bad admin edit can never freeze or cheapen upgrades."""
        raw = await ConfigService.get(session, "card_upgrade_costs")
        try:
            return parse_cost_table(raw)
        except InvalidUpgradeCostTable as exc:
            logger.error(f"{exc}; using built-in card_upgrade_costs defaults")
            return parse_cost_table(ConfigService.default("card_upgrade_costs"))

    @classmethod
    async def cost_for_level(
        cls, session: AsyncSession, current_level: int
    ) -> Optional[UpgradeCost]:
        """Cost of leaving ``current_level`` (None at the maximum level)."""
        if is_max_level(current_level):
            return None
        table = await cls.cost_table(session)
        return table[int(current_level) - MIN_CARD_LEVEL]

    # ------------------------------------------------------------------
    # Preview (read-only)
    # ------------------------------------------------------------------
    @classmethod
    async def preview(
        cls, session: AsyncSession, user_id: int, card_id: int
    ) -> UpgradePreview:
        card = await session.get(PlayerCard, card_id)
        if card is None or card.user_id != user_id:
            raise CardNotOwnedError()
        character = await session.get(Character, card.character_id)
        if character is None:
            raise CardUpgradeError("شخصیت این کارت پیدا نشد.")

        current = card_attributes(card, character)
        apples = await AppleWalletService.get_balance(session, user_id)
        cost = await cls.cost_for_level(session, card.level)
        if cost is None:
            return UpgradePreview(
                card_id=card.id,
                character_name=character.name_fa,
                level=card.level,
                next_level=None,
                current=current,
                after=None,
                cost=None,
                apple_balance=apples,
                item_balance=0,
                missing=None,
            )
        items = await ItemService.get_total(session, user_id, cost.item_key)

        return UpgradePreview(
            card_id=card.id,
            character_name=character.name_fa,
            level=card.level,
            next_level=card.level + 1,
            current=current,
            after=current.plus(ATTRIBUTE_STEP),
            cost=cost,
            apple_balance=apples,
            item_balance=items,
            missing=missing_for(cost, apples, items),
        )

    # ------------------------------------------------------------------
    # Upgrade (atomic)
    # ------------------------------------------------------------------
    @classmethod
    async def upgrade(
        cls,
        session: AsyncSession,
        user_id: int,
        card_id: int,
        *,
        expected_level: Optional[int] = None,
    ) -> UpgradeResult:
        """Upgrade ONE level. Must run inside a transaction the caller
        commits (``get_session()``); raises ``CardUpgradeError`` subclasses
        without changing anything when a rule is not met."""
        # 1. lock the card + ownership
        card = await session.scalar(
            select(PlayerCard)
            .where(PlayerCard.id == card_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if card is None or card.user_id != user_id:
            raise CardNotOwnedError()

        # 2. level rules
        if is_max_level(card.level):
            raise CardMaxLevelError()
        if expected_level is not None and card.level != expected_level:
            raise StaleUpgradeError(card.level)
        from_level = card.level
        to_level = from_level + 1

        character = await session.get(Character, card.character_id)
        if character is None:
            raise CardUpgradeError("شخصیت این کارت پیدا نشد.")
        cost = await cls.cost_for_level(session, from_level)
        assert cost is not None  # not at max level

        # 3. lock resources (items -> wallet) and verify ALL of them
        item_have = await ItemService.get_total(
            session, user_id, cost.item_key, for_update=True
        )
        apples_have = await AppleWalletService.get_balance(
            session, user_id, for_update=True
        )
        missing = missing_for(cost, apples_have, item_have)
        if not missing.is_empty:
            raise InsufficientResourcesError(cost, missing)

        # 4. mutate: CAS level first (nothing else changed yet if it loses)
        swapped = await session.execute(
            update(PlayerCard)
            .where(
                PlayerCard.id == card.id,
                PlayerCard.level == from_level,
                PlayerCard.level < MAX_CARD_LEVEL,
            )
            .values(level=to_level)
        )
        if swapped.rowcount != 1:
            await session.refresh(card)
            raise StaleUpgradeError(card.level)

        try:
            balance_after = await AppleWalletService.spend(
                session,
                user_id,
                cost.apples,
                reason=LEDGER_REASON,
                ref_type="card",
                ref_id=card.id,
                idempotency_key=LEDGER_KEY_FORMAT.format(
                    card_id=card.id, to_level=to_level
                ),
            )
            if balance_after != apples_have - cost.apples:
                # The wallet service treated this as an already-applied
                # (idempotent) spend: refuse rather than upgrade for free.
                raise CardUpgradeError("ثبت هزینه ارتقا انجام نشد.")
            await ItemService.consume(session, user_id, cost.item_key, cost.item_quantity)
            # A card selected as the battle loadout mirrors its level.
            await session.execute(
                update(AppleBattleQueueEntry)
                .where(AppleBattleQueueEntry.card_id == card.id)
                .values(level=to_level)
            )
            await session.refresh(card)
        except Exception:
            # Money, items and level move together or not at all.
            await session.rollback()
            raise

        before = attributes_at_level(base_attributes(character), from_level)
        after = attributes_at_level(base_attributes(character), card.level)

        from sib.services.player_xp import PlayerXPService

        await PlayerXPService.award(
            session,
            user_id=user_id,
            source="card_upgrade",
            event_key=f"card_upgrade:{character.id}:{to_level}",
        )

        log_event(
            Events.CHARACTER_EVENT,
            {
                "action": "card_upgraded",
                "user_id": user_id,
                "card_id": card.id,
                "from_level": from_level,
                "to_level": card.level,
                "apples": cost.apples,
                "item": cost.item_key,
                "item_quantity": cost.item_quantity,
            },
        )
        return UpgradeResult(
            card_id=card.id,
            from_level=from_level,
            to_level=card.level,
            before=before,
            after=after,
            cost=cost,
        )
