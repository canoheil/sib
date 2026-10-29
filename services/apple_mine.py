"""Apple Mine reward, cooldown, and pickaxe progression rules (M2-RECON).

The M1 flat reward table is replaced by a per-pickaxe-level table: every
player owns one pickaxe (``user_pickaxes``, levels 1..5, lazily created). A
15-minute work shift rolls that level's special-apple chances first (fixed
drop quantities 3 gold / 1 diamond / 1 obsidian) and otherwise grants a
uniform random amount of normal Apples from the level's range. Upgrading
costs normal wallet Apples only and is audited in the apple ledger under a
fresh idempotency namespace.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import random
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger
from sib.database.models import AppleMineShift, UserPickaxe
from sib.services.apple_mine_narrative import pick_narrative
from sib.services.apple_wallet import (
    AppleWalletService,
    InsufficientFundsException,
)
from sib.services.config_store import ConfigService
from sib.services import disease_service
from sib.services.golden_hour import reward_multiplier as golden_hour_multiplier
from sib.services.items import ItemService
from sib.services.lock_order import lock_user_row

WORK_COOLDOWN_SECONDS = 900
MINE_INFECTION_WARNING = "⚠️ گرد و غبار معدن باعث شد به بیماری مبتلا شوی!"

MIN_PICKAXE_LEVEL = 1
MAX_PICKAXE_LEVEL = 5

LEDGER_REASON = "pickaxe_upgrade"
# Fresh key namespace (see the card_upgrade warning about retired keys):
# ``{user_id}:{to_level}`` makes a double press of the SAME step a ledger
# replay instead of a double charge; a press for a later step uses a
# different key by construction.
LEDGER_KEY_FORMAT = "pickaxe_upgrade:{user_id}:{to_level}"

# Special-apple outcome codes, inventory keys and FIXED drop quantities
# (unchanged from M1; only the chances scale with the pickaxe level).
SPECIAL_GOLD = ("golden", "apple_gold", 3)
SPECIAL_DIAMOND = ("diamond", "apple_diamond", 1)
SPECIAL_OBSIDIAN = ("obsidian", "apple_obsidian", 1)
NORMAL_OUTCOME = ("normal", "apple_normal")


class AppleMineError(Exception):
    """Base class for expected Apple Mine refusals."""


class CooldownError(AppleMineError):
    """Raised when the user has not finished the global work cooldown."""

    def __init__(self, remaining_seconds: int) -> None:
        self.remaining_seconds = max(0, int(remaining_seconds))
        super().__init__("Apple Mine cooldown is active")


class PickaxeError(AppleMineError):
    """Base class for expected pickaxe refusals (nothing was changed)."""


class PickaxeMaxLevelError(PickaxeError):
    """The pickaxe is already at the maximum level."""

    def __init__(self, level: int) -> None:
        self.level = level
        super().__init__(f"کلنگ به بالاترین سطح (سطح {level}) رسیده است.")


class PickaxeInsufficientFunds(PickaxeError):
    """The wallet cannot cover the upgrade cost."""

    def __init__(self, cost: int, missing: int) -> None:
        self.cost = cost
        self.missing = max(0, missing)
        super().__init__("سیب کافی برای ارتقا کلنگ نیست.")


class InvalidPickaxeConfig(PickaxeError):
    """A configured pickaxe table failed validation."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"جدول کلنگ نامعتبر است: {detail}")


@dataclass(frozen=True)
class RewardRow:
    """One level's shift reward table (percentages out of 100)."""

    level: int
    min_apples: int
    max_apples: int
    gold_pct: float
    diamond_pct: float
    obsidian_pct: float

    @property
    def special_pct(self) -> float:
        return self.gold_pct + self.diamond_pct + self.obsidian_pct


@dataclass(frozen=True)
class ShiftResult:
    outcome_code: str
    item_code: str
    amount: int
    narrative: str
    pickaxe_level: int = MIN_PICKAXE_LEVEL
    plant_dropped: bool = False


@dataclass(frozen=True)
class UpgradeResult:
    """A completed pickaxe upgrade (wallet debit applied)."""

    level: int
    cost: int
    balance_after: int


# ---------------------------------------------------------------------------
# Configuration tables (system_config, with validated built-in defaults)
# ---------------------------------------------------------------------------

def _parse_reward_table(raw) -> list[RewardRow]:
    """Validate ``pickaxe_rewards``; raise ValueError on any breach."""
    if not isinstance(raw, list) or len(raw) != MAX_PICKAXE_LEVEL:
        raise ValueError(f"expected exactly {MAX_PICKAXE_LEVEL} level rows")
    rows: list[RewardRow] = []
    for index, entry in enumerate(raw, start=MIN_PICKAXE_LEVEL):
        if not isinstance(entry, dict):
            raise ValueError(f"level {index} row is not an object")
        try:
            min_apples = int(entry["min"])
            max_apples = int(entry["max"])
            gold = float(entry["gold"])
            diamond = float(entry["diamond"])
            obsidian = float(entry["obsidian"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"level {index} row is malformed") from exc
        if min_apples < 1 or max_apples < min_apples:
            raise ValueError(f"level {index} has a bad normal-apple range")
        for name, value in (
            ("gold", gold),
            ("diamond", diamond),
            ("obsidian", obsidian),
        ):
            if value < 0 or value > 100:
                raise ValueError(f"level {index} has a bad {name} chance")
        if gold + diamond + obsidian > 100 + 1e-9:
            raise ValueError(f"level {index} special chances exceed 100")
        rows.append(
            RewardRow(
                level=index,
                min_apples=min_apples,
                max_apples=max_apples,
                gold_pct=gold,
                diamond_pct=diamond,
                obsidian_pct=obsidian,
            )
        )
    return rows


def _parse_cost_table(raw) -> list[int]:
    """Validate ``pickaxe_upgrade_costs``; raise ValueError on any breach."""
    if not isinstance(raw, list) or len(raw) != MAX_PICKAXE_LEVEL:
        raise ValueError(f"expected exactly {MAX_PICKAXE_LEVEL} costs")
    costs: list[int] = []
    for index, value in enumerate(raw, start=MIN_PICKAXE_LEVEL):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"cost for level {index} is not an integer")
        if value < 0:
            raise ValueError(f"cost for level {index} is negative")
        costs.append(value)
    if costs[0] != 0:
        raise ValueError("level 1 is free (first cost must be 0)")
    return costs


async def reward_table(session: AsyncSession) -> list[RewardRow]:
    """The five reward rows. A broken override falls back to the defaults
    (same policy as ``card_upgrade_costs``)."""
    raw = await ConfigService.get(session, "pickaxe_rewards")
    try:
        return _parse_reward_table(raw)
    except ValueError as exc:
        logger.error(f"{exc}; using built-in pickaxe_rewards defaults")
        return _parse_reward_table(ConfigService.default("pickaxe_rewards"))


async def reward_table_for_level(session: AsyncSession, level: int) -> RewardRow:
    table = await reward_table(session)
    return table[int(level) - MIN_PICKAXE_LEVEL]


async def upgrade_costs(session: AsyncSession) -> list[int]:
    """The five costs (index = level - 1). Broken override -> defaults."""
    raw = await ConfigService.get(session, "pickaxe_upgrade_costs")
    try:
        return _parse_cost_table(raw)
    except ValueError as exc:
        logger.error(f"{exc}; using built-in pickaxe_upgrade_costs defaults")
        return _parse_cost_table(ConfigService.default("pickaxe_upgrade_costs"))


async def upgrade_cost_for_level(session: AsyncSession, level: int) -> Optional[int]:
    """Cost to enter ``level + 1``; ``None`` at the maximum level.

    ``pickaxe_upgrade_costs`` index i = cost to enter level i+1, so the
    next step of a level-L pickaxe sits at index L.
    """
    if int(level) >= MAX_PICKAXE_LEVEL:
        return None
    return (await upgrade_costs(session))[int(level)]


# ---------------------------------------------------------------------------
# Cooldown (unchanged from M1: last shift + database clock)
# ---------------------------------------------------------------------------

def _as_utc(value: datetime) -> datetime:
    """Normalize database timestamps before doing elapsed-time arithmetic."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _database_now(session: AsyncSession) -> datetime:
    """Read the database clock so cooldown checks use the same clock as rows."""
    value = await session.scalar(select(func.now()))
    if value is None:  # pragma: no cover - SQL NOW() always returns a value
        return datetime.now(timezone.utc)
    return value


async def _last_worked_at(session: AsyncSession, user_id: int) -> datetime | None:
    return await session.scalar(
        select(AppleMineShift.worked_at)
        .where(AppleMineShift.user_id == user_id)
        .order_by(desc(AppleMineShift.worked_at), desc(AppleMineShift.id))
        .limit(1)
    )


def _remaining_from(last_worked_at: datetime | None, now: datetime) -> int:
    if last_worked_at is None:
        return 0
    elapsed = (_as_utc(now) - _as_utc(last_worked_at)).total_seconds()
    remaining = math.ceil(WORK_COOLDOWN_SECONDS - elapsed)
    return min(WORK_COOLDOWN_SECONDS, max(0, remaining))


async def remaining_cooldown(session: AsyncSession, user_id: int) -> int:
    """Return seconds until this user's global mine cooldown expires."""
    now = await _database_now(session)
    last_worked_at = await _last_worked_at(session, user_id)
    return _remaining_from(last_worked_at, now)


# ---------------------------------------------------------------------------
# Pickaxe progression
# ---------------------------------------------------------------------------

async def get_pickaxe_level(session: AsyncSession, user_id: int) -> int:
    """The user's current pickaxe level, lazily creating the Level-1 row.

    Locking read: callers that mutate state hold the ``users`` row lock
    first (see ``perform_shift`` / ``upgrade_pickaxe``); display callers
    simply pay one extra index lookup.
    """
    row = await session.scalar(
        select(UserPickaxe).where(UserPickaxe.user_id == user_id).with_for_update()
    )
    if row is None:
        row = UserPickaxe(user_id=user_id, level=MIN_PICKAXE_LEVEL)
        session.add(row)
        await session.flush()
    return int(row.level)


async def upgrade_pickaxe(session: AsyncSession, *, user_id: int) -> UpgradeResult:
    """Pay the next step's cost and advance the pickaxe one level.

    Runs under the ``users`` row lock, so a double press of the same step
    serializes: the loser either sees the new level (stale screen) or its
    keyed debit replays against the ledger and charges nothing again.
    Raises :class:`PickaxeMaxLevelError` / :class:`PickaxeInsufficientFunds`
    without changing any state.
    """
    await lock_user_row(session, user_id)
    pickaxe_level = await get_pickaxe_level(session, user_id)
    if pickaxe_level >= MAX_PICKAXE_LEVEL:
        raise PickaxeMaxLevelError(pickaxe_level)

    cost = (await upgrade_costs(session))[pickaxe_level]
    to_level = pickaxe_level + 1
    try:
        debit = await AppleWalletService.debit_once(
            session,
            user_id,
            cost,
            reason=LEDGER_REASON,
            idempotency_key=LEDGER_KEY_FORMAT.format(
                user_id=user_id, to_level=to_level
            ),
        )
    except InsufficientFundsException:
        balance = await AppleWalletService.get_balance(session, user_id)
        raise PickaxeInsufficientFunds(cost=cost, missing=cost - balance)

    # A replayed key (applied=False) means a concurrent press already paid
    # this step and moved the level; adopt the authoritative row below.
    row = await session.scalar(
        select(UserPickaxe)
        .where(UserPickaxe.user_id == user_id)
        .with_for_update()
    )
    if row is not None and row.level < to_level:
        row.level = to_level
    await session.flush()

    return UpgradeResult(level=to_level, cost=cost, balance_after=debit.balance_after)


# ---------------------------------------------------------------------------
# Work shift
# ---------------------------------------------------------------------------

def _roll_shift(row: RewardRow) -> tuple[str, str, int]:
    """One shift roll: special chances first, otherwise a normal-apple range."""
    roll = random.random() * 100.0
    if roll < row.gold_pct:
        return SPECIAL_GOLD
    if roll < row.gold_pct + row.diamond_pct:
        return SPECIAL_DIAMOND
    if roll < row.gold_pct + row.diamond_pct + row.obsidian_pct:
        return SPECIAL_OBSIDIAN
    return NORMAL_OUTCOME[0], NORMAL_OUTCOME[1], random.randint(
        row.min_apples, row.max_apples
    )


async def _plant_drop_chance(session: AsyncSession) -> int:
    key = "disease_mine_plant_drop_chance"
    try:
        chance = await ConfigService.get_int(session, key)
    except (TypeError, ValueError, OverflowError):
        chance = int(ConfigService.default(key))
    return min(100, max(0, chance))


async def _mine_infection_chance(session: AsyncSession) -> int:
    return await disease_service._percent(session, "disease_mine_infection_chance")


async def perform_shift(
    session: AsyncSession,
    *,
    user_id: int,
    group_id: int | None,
) -> ShiftResult:
    """Complete one atomic shift, its primary reward and an optional plant.

    The user-row lock serializes concurrent taps across groups and workers.
    The reward is rolled from the player's pickaxe level (M2-RECON); card
    selection was cosmetic in M1 and is gone - ``card_id`` stays NULL on new
    rows.
    """
    await lock_user_row(session, user_id)
    now = await _database_now(session)
    last_worked_at = await _last_worked_at(session, user_id)
    remaining = _remaining_from(last_worked_at, now)
    if remaining > 0:
        raise CooldownError(remaining)

    pickaxe_level = await get_pickaxe_level(session, user_id)
    reward_row = await reward_table_for_level(session, pickaxe_level)
    outcome_code, item_code, base_amount = _roll_shift(reward_row)
    multiplier = (
        golden_hour_multiplier("apple_mine", now=now)
        if item_code == NORMAL_OUTCOME[1]
        else 1
    )
    amount = base_amount * multiplier
    plant_chance = await _plant_drop_chance(session)
    plant_dropped = plant_chance > 0 and random.random() * 100 < plant_chance

    shift = AppleMineShift(
        user_id=user_id,
        group_id=group_id,
        card_id=None,
        pickaxe_level=pickaxe_level,
        outcome=outcome_code,
        item_code=item_code,
        amount=amount,
        worked_at=now,
    )
    session.add(shift)
    await session.flush()

    if plant_dropped:
        # Pre-lock inventory before the wallet, and in key order when the
        # primary reward is also an inventory item. The user row above also
        # serializes creation when one of these rows does not yet exist.
        inventory_keys = {"disease_plant"}
        if item_code != NORMAL_OUTCOME[1]:
            inventory_keys.add(item_code)
        for key in sorted(inventory_keys):
            await ItemService._lock_inventory_row(session, user_id, key)

    if item_code == NORMAL_OUTCOME[1]:
        await AppleWalletService.earn(
            session,
            user_id=user_id,
            amount=base_amount,
            reason="apple_mine",
            ref_type="apple_mine_shift",
            ref_id=shift.id,
            idempotency_key=f"apple_mine:{shift.id}",
            reward_multiplier=multiplier,
            occurred_at=now,
        )
    else:
        await ItemService.grant(session, user_id, item_code, quantity=amount)

    if plant_dropped:
        await ItemService.grant(session, user_id, "disease_plant", quantity=1)

    narrative = pick_narrative(outcome_code, user_id)
    chance = await _mine_infection_chance(session)
    if chance > 0 and disease_service._roll(chance):
        if await disease_service.infect_user(session, user_id, "mine"):
            narrative += f"\n{MINE_INFECTION_WARNING}"

    return ShiftResult(
        outcome_code=outcome_code,
        item_code=item_code,
        amount=amount,
        narrative=narrative,
        pickaxe_level=pickaxe_level,
        plant_dropped=plant_dropped,
    )
