"""Random-event engine for an active Invasion v2 battle.

The engine is deliberately *not* a second background loop: it is one extra
step inside ``InvasionScheduler.tick``'s transaction (planning + applying) and
one post-commit presentation pass in ``MaintenanceService.run_once``.

Time model
----------
``inv2_invasions.started_at`` is written only after the announcement photo was
accepted, so it is the single clock that means "players can see this battle".
Every event offset is relative to it, and nothing is planned for a battle that
was never announced:

    offset(i) = guard_head + sum(gap)         0 < guard_head < ... < W - guard_tail

Lifecycle
---------
``PLANNED`` → (due, battle still ACTIVE, group reachable) → ``APPLIED`` →
(photo accepted by Telegram) → ``ACTIVE`` → ``CLOSED``. Anything that never
applies ends in ``SKIPPED``. One-shot events (heal / meteor) go
``APPLIED → CLOSED`` when their message is delivered, because they have no
window to keep open.

Lock order
----------
The tick already holds ``Group`` rows (from ``_spawn_if_due``) and takes
``Inv2Invasion`` before the events of that invasion, so the added direction is
``Invasion → Event`` — the same direction the join path uses, never the
reverse. The chest claim takes ``Event`` and then a user mutex via the wallet /
item services, and never the invasion row.
"""

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger
from sib.database.models import (
    Inv2EventClaim,
    Inv2EventEffect,
    Inv2Invasion,
    Inv2RandomEvent,
    User,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.sib_claim import fa_digits

from . import event_catalog as catalog
from .circuit_breaker import is_group_available
from .enums import InvasionStatus

# ---------------------------------------------------------------------------
# Wire values
# ---------------------------------------------------------------------------
PLANNED = "PLANNED"
APPLIED = "APPLIED"
ACTIVE = "ACTIVE"
CLOSED = "CLOSED"
SKIPPED = "SKIPPED"

OPEN_STATUSES = (PLANNED, APPLIED, ACTIVE)

#: A timed event never matters once fewer seconds than this remain.
MIN_USEFUL_WINDOW = 60


def _utc(value: datetime) -> datetime:
    """Normalize MariaDB's naive UTC DATETIME fields for Python arithmetic."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanConfig:
    """Everything that shapes a plan, read once per tick."""

    guard_head: int = 60
    guard_tail: int = 120
    min_gap: int = 300
    max_gap: int = 420
    min_events: int = 3
    max_events: int = 7
    loot_window: int = 120
    loot_max_claims: int = 3
    loot_apples: int = 40
    weights: dict = None  # type: ignore[assignment]

    def effective_weights(self) -> dict:
        base = {template.code: template.weight for template in catalog.EVENTS}
        override = self.weights or {}
        for code, value in override.items():
            if code in base:
                try:
                    base[code] = max(0.0, float(value))
                except (TypeError, ValueError):
                    continue
        return base


async def load_plan_config(session: AsyncSession) -> PlanConfig:
    """Read the tunables with safe fallbacks; never raise on a bad value."""

    async def _int(key: str, default: int, *, minimum: int = 0) -> int:
        raw = await ConfigService.get(session, key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("inv2 events: invalid %s=%r, using %s", key, raw, default)
            return default
        return value if value >= minimum else default

    weights = await ConfigService.get(session, "inv2_event_weights")
    return PlanConfig(
        guard_head=await _int("inv2_event_guard_head_seconds", 60, minimum=0),
        guard_tail=await _int("inv2_event_guard_tail_seconds", 120, minimum=0),
        min_gap=await _int("inv2_event_min_gap_seconds", 300, minimum=1),
        max_gap=await _int("inv2_event_max_gap_seconds", 420, minimum=1),
        min_events=await _int("inv2_event_min_per_invasion", 3, minimum=1),
        max_events=await _int("inv2_event_max_per_invasion", 7, minimum=1),
        loot_window=await _int("inv2_event_loot_window_seconds", 120, minimum=1),
        loot_max_claims=await _int("inv2_event_loot_max_claims", 3, minimum=1),
        loot_apples=await _int("inv2_event_loot_apples", 40, minimum=1),
        weights=weights if isinstance(weights, dict) else {},
    )


async def events_enabled(session: AsyncSession) -> bool:
    """Fail-closed read of the dedicated kill switch."""
    try:
        return bool(await ConfigService.get(session, "inv2_events_enabled"))
    except Exception:
        logger.exception("inv2 events: enabled flag read failed; treating as disabled")
        return False


# ---------------------------------------------------------------------------
# Pure planning helpers
# ---------------------------------------------------------------------------
def plan_offsets(window_seconds: int, config: PlanConfig, rng: random.Random) -> list[int]:
    """Return the K event offsets (seconds after ``started_at``).

    The count grows with the battle length but stays inside the configured
    bounds; gaps stay inside ``[min_gap, max_gap]`` and the last offset is
    exactly ``window - guard_tail`` so an event never lands in the final
    minutes where a two-minute buff would be wasted.
    """
    usable = window_seconds - config.guard_head - config.guard_tail
    if usable <= MIN_USEFUL_WINDOW:
        return []
    count = int(usable // 360) + 1
    count = max(config.min_events, min(config.max_events, count))
    # A short battle gets fewer events rather than gaps below the floor.
    while count > 1 and (count - 1) * config.min_gap > usable:
        count -= 1
    if count <= 0:
        return []
    if count == 1:
        return [config.guard_head]

    base_gap = usable / (count - 1)
    jitter = max(0.0, min(30.0, base_gap - config.min_gap, config.max_gap - base_gap))
    gaps = [base_gap + rng.uniform(-jitter, jitter) for _ in range(count - 1)]
    total = sum(gaps) or 1.0
    gaps = [gap * usable / total for gap in gaps]

    offsets = [config.guard_head]
    elapsed = 0.0
    for gap in gaps[:-1]:
        elapsed += gap
        offsets.append(config.guard_head + elapsed)
    offsets.append(window_seconds - config.guard_tail)
    return [int(round(offset)) for offset in offsets]


def _bias(template: catalog.EventTemplate, slot: int, count: int) -> float:
    """Front-load player help and loot; lean on Shiyasib late in the battle."""
    first_half = slot < max(1, count / 2)
    if first_half and template.category in (catalog.PLAYER_BUFF, catalog.LOOT):
        return 1.5
    if not first_half and template.category == catalog.BOSS_BUFF:
        return 1.5
    return 1.0


def _weighted_pick(
    templates: Sequence[catalog.EventTemplate],
    weights: Sequence[float],
    rng: random.Random,
) -> Optional[catalog.EventTemplate]:
    total = sum(max(0.0, weight) for weight in weights)
    if total <= 0:
        return None
    draw = rng.random() * total
    cumulative = 0.0
    for template, weight in zip(templates, weights):
        cumulative += max(0.0, weight)
        if draw < cumulative:
            return template
    return templates[-1]


def choose_templates(
    count: int,
    config: PlanConfig,
    *,
    hp_remaining: int,
    total_hp: int,
    rng: random.Random,
) -> list[catalog.EventTemplate]:
    """Pick ``count`` distinct, eligible templates with phase bias."""
    weights = config.effective_weights()
    candidates = [
        template for template in catalog.EVENTS
        if weights.get(template.code, 0) > 0
        and template.eligible(hp_remaining=hp_remaining, total_hp=total_hp)
    ]
    chosen: list[catalog.EventTemplate] = []
    for slot in range(count):
        pool = [template for template in candidates if template not in chosen]
        if not pool:
            break
        picked = _weighted_pick(
            pool, [weights[t.code] * _bias(t, slot, count) for t in pool], rng
        )
        if picked is not None:
            chosen.append(picked)

    # Variety guarantees: never open with a boss debuff, and make sure the
    # battle offers at least one chest when it has room for two events.
    if len(chosen) >= 2 and chosen[0].category == catalog.BOSS_BUFF:
        alternative = next(
            (t for t in candidates if t.category != catalog.BOSS_BUFF and t not in chosen[1:]),
            None,
        )
        if alternative is not None:
            chosen[0] = alternative
    if len(chosen) >= 2 and not any(t.category == catalog.LOOT for t in chosen):
        loot = max(
            (t for t in candidates if t.category == catalog.LOOT),
            key=lambda t: weights.get(t.code, 0.0),
            default=None,
        )
        if loot is not None and loot not in chosen[:-1]:
            chosen[-1] = loot
    # De-duplicate defensively: a plan must never repeat a template.
    unique: list[catalog.EventTemplate] = []
    for template in chosen:
        if template not in unique:
            unique.append(template)
    return unique


def build_plan(
    *,
    window_seconds: int,
    hp_remaining: int,
    total_hp: int,
    config: PlanConfig,
    rng: random.Random,
) -> list[tuple[int, catalog.EventTemplate]]:
    """Return ``[(offset_seconds, template), ...]`` for one invasion."""
    offsets = plan_offsets(window_seconds, config, rng)
    if not offsets:
        return []
    templates = choose_templates(
        len(offsets), config, hp_remaining=hp_remaining, total_hp=total_hp, rng=rng
    )
    return list(zip(offsets[: len(templates)], templates))


# ---------------------------------------------------------------------------
# Timed modifiers read by the join path
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Modifiers:
    """Folded, ready-to-apply modifiers of every active event."""

    damage_multiplier: float = 1.0
    crit_bonus: float = 0.0
    damage_cap_pct: Optional[float] = None

    @property
    def active(self) -> bool:
        return (
            self.damage_multiplier != 1.0
            or self.crit_bonus != 0.0
            or self.damage_cap_pct is not None
        )

    def crit_chance(self, base: float = 0.10) -> float:
        return max(0.0, min(1.0, base + self.crit_bonus))


NO_MODIFIERS = Modifiers()


async def load_active_modifiers(
    session: AsyncSession, invasion_id: int, now: datetime
) -> Modifiers:
    """Read the modifiers whose event window contains ``now``.

    One indexed query on ``(invasion_id, status)``; the small result is
    filtered in Python because MariaDB returns naive datetimes while SQLite
    stores them without an offset, so a SQL time comparison would be
    dialect-dependent. No lock is taken.
    """
    try:
        rows = (await session.execute(
            select(
                Inv2RandomEvent.status,
                Inv2RandomEvent.starts_at,
                Inv2RandomEvent.ends_at,
                Inv2EventEffect.kind,
                Inv2EventEffect.value,
            )
            .join(Inv2EventEffect, Inv2EventEffect.event_id == Inv2RandomEvent.id)
            .where(
                Inv2RandomEvent.invasion_id == invasion_id,
                Inv2RandomEvent.status == ACTIVE,
                Inv2EventEffect.kind.in_(catalog.TIMED_KINDS),
            )
        )).all()
    except Exception:
        # A missing/broken event table must never block a normal attack.
        logger.exception("inv2 events: modifier read failed invasion=%s", invasion_id)
        return NO_MODIFIERS

    moment = _utc(now)
    multipliers: list[float] = []
    crit_bonuses: list[float] = []
    caps: list[float] = []
    for status, starts_at, ends_at, kind, value in rows:
        if status != ACTIVE or starts_at is None or ends_at is None:
            continue
        if not (_utc(starts_at) <= moment < _utc(ends_at)):
            continue
        if kind == catalog.DAMAGE_MULTIPLIER:
            multipliers.append(float(value))
        elif kind == catalog.CRIT_BONUS:
            crit_bonuses.append(float(value))
        elif kind == catalog.DAMAGE_CAP_PCT:
            caps.append(float(value))

    multiplier = 1.0
    for value in multipliers:
        multiplier *= max(0.0, value)
    return Modifiers(
        damage_multiplier=multiplier,
        crit_bonus=sum(crit_bonuses),
        damage_cap_pct=max(caps) if caps else None,
    )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class InvasionEventScheduler:
    """Plan, apply and close random events inside the existing invasion tick."""

    # -- planning ---------------------------------------------------------
    @classmethod
    async def _plan_if_needed(
        cls, session: AsyncSession, invasion: Inv2Invasion, now: datetime,
        config: PlanConfig, rng: random.Random,
    ) -> int:
        existing = await session.scalar(
            select(Inv2RandomEvent.id)
            .where(Inv2RandomEvent.invasion_id == invasion.id)
            .limit(1)
        )
        if existing is not None:
            return 0
        started_at = _utc(invasion.started_at)
        ends_at = _utc(invasion.ends_at)
        window = int((ends_at - started_at).total_seconds())
        if window <= 0:
            return 0
        plan = build_plan(
            window_seconds=window,
            hp_remaining=invasion.enemy_hp_remaining,
            total_hp=invasion.enemy_hp,
            config=config,
            rng=rng,
        )
        if not plan:
            return 0
        narrative_rng = random.Random(f"{invasion.id}:{started_at.timestamp()}")
        for slot, (offset, template) in enumerate(plan, start=1):
            loot = template.loot
            event = Inv2RandomEvent(
                invasion_id=invasion.id,
                group_id=invasion.group_id,
                slot_no=slot,
                template_code=template.code,
                category=template.category,
                status=PLANNED,
                scheduled_at=started_at + timedelta(seconds=offset),
                duration_seconds=(
                    config.loot_window if template.category == catalog.LOOT
                    else template.duration_seconds
                ),
                narrative=narrative_rng.choice(list(template.narratives)),
                loot_item_key=loot.reward_type if loot else None,
                loot_amount=(
                    config.loot_apples if loot and loot.is_wallet
                    else loot.default_amount if loot else None
                ),
                loot_max_claims=config.loot_max_claims if loot else None,
                loot_claimed=0,
                send_attempts=0,
            )
            session.add(event)
            await session.flush()
            for kind, value in template.effects:
                session.add(Inv2EventEffect(event_id=event.id, kind=kind, value=value))
        return len(plan)

    # -- applying ---------------------------------------------------------
    @classmethod
    async def _apply_one(
        cls, session: AsyncSession, event: Inv2RandomEvent, now: datetime
    ) -> str:
        """Apply one due event; returns its new status."""
        invasion = await session.scalar(
            select(Inv2Invasion)
            .where(Inv2Invasion.id == event.invasion_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            invasion is None
            or invasion.status != InvasionStatus.ACTIVE.value
            or invasion.ends_at is None
            or _utc(invasion.ends_at) <= now
        ):
            event.status = SKIPPED
            event.resolved_at = now
            return SKIPPED
        if not await is_group_available(invasion.group_id, session=session, now=now):
            event.status = SKIPPED
            event.resolved_at = now
            return SKIPPED

        template = catalog.BY_CODE.get(event.template_code)
        if template is None:
            event.status = SKIPPED
            event.resolved_at = now
            return SKIPPED

        for kind, value in template.effects:
            if kind == catalog.BOSS_HEAL_PCT:
                event.applied_amount = await cls._apply_heal(session, invasion, value, now)
                if event.applied_amount is None:
                    event.status = SKIPPED
                    event.resolved_at = now
                    return SKIPPED
            elif kind == catalog.INSTANT_DAMAGE_PCT:
                event.applied_amount = await cls._apply_damage(session, invasion, value, now)
                if event.applied_amount is None:
                    event.status = SKIPPED
                    event.resolved_at = now
                    return SKIPPED

        event.status = APPLIED
        return APPLIED

    @staticmethod
    async def _cas_hp(session: AsyncSession, invasion: Inv2Invasion, new_hp: int,
                      observed: int) -> bool:
        result = await session.execute(
            update(Inv2Invasion)
            .where(
                Inv2Invasion.id == invasion.id,
                Inv2Invasion.status == InvasionStatus.ACTIVE.value,
                Inv2Invasion.enemy_hp_remaining == observed,
            )
            .execution_options(synchronize_session=False)
            .values(enemy_hp_remaining=new_hp)
        )
        if result.rowcount == 1:
            invasion.enemy_hp_remaining = new_hp
            return True
        return False

    @classmethod
    async def _apply_heal(
        cls, session: AsyncSession, invasion: Inv2Invasion, pct: float, now: datetime
    ) -> Optional[int]:
        for _ in range(2):
            observed = int(invasion.enemy_hp_remaining)
            if observed <= 0 or observed >= invasion.enemy_hp:
                return None  # nothing to heal
            heal = max(1, round(abs(pct) * invasion.enemy_hp))
            new_hp = min(invasion.enemy_hp, observed + heal)
            if new_hp == observed:
                return None
            if await cls._cas_hp(session, invasion, new_hp, observed):
                return new_hp - observed
            await session.refresh(invasion)
        return None

    @classmethod
    async def _apply_damage(
        cls, session: AsyncSession, invasion: Inv2Invasion, pct: float, now: datetime
    ) -> Optional[int]:
        """Instant damage is deliberately non-lethal: at least 1 HP remains.

        A lethal meteor would become a third terminal writer (settlement +
        rewards + region flag) and change victory from "a player's killing
        blow" to "a timer". That is a product decision, not an implementation
        detail, so v1 clamps at 1 HP.
        """
        for _ in range(2):
            observed = int(invasion.enemy_hp_remaining)
            if observed <= 1:
                return None  # nothing meaningful to hit
            amount = max(1, round(abs(pct) * invasion.enemy_hp))
            new_hp = max(1, observed - amount)
            if new_hp == observed:
                return None
            if await cls._cas_hp(session, invasion, new_hp, observed):
                return observed - new_hp
            await session.refresh(invasion)
        return None

    # -- tick -------------------------------------------------------------
    @classmethod
    async def tick(
        cls,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
        rng: Optional[random.Random] = None,
        stats: Optional[dict] = None,
        config: Optional[PlanConfig] = None,
    ) -> dict:
        """Plan, apply and close events for every active invasion.

        Runs inside the caller's transaction. Returns a small counters dict;
        also folded into ``stats`` when the caller passed the tick's stats.
        """
        counts = {"planned": 0, "applied": 0, "skipped": 0, "closed": 0}
        if not await events_enabled(session):
            return counts
        moment = _utc(now or datetime.now(timezone.utc))
        rng = rng or random.Random()
        config = config or await load_plan_config(session)

        active = list((await session.scalars(
            select(Inv2Invasion)
            .where(
                Inv2Invasion.status == InvasionStatus.ACTIVE.value,
                Inv2Invasion.started_at.is_not(None),
                Inv2Invasion.ends_at.is_not(None),
                Inv2Invasion.ends_at > moment,
            )
            .order_by(Inv2Invasion.id)
            .with_for_update()
        )).all())
        for invasion in active:
            counts["planned"] += await cls._plan_if_needed(
                session, invasion, moment, config, rng
            )

        due = list((await session.scalars(
            select(Inv2RandomEvent)
            .where(
                Inv2RandomEvent.status == PLANNED,
                Inv2RandomEvent.scheduled_at <= moment,
            )
            .order_by(Inv2RandomEvent.invasion_id, Inv2RandomEvent.slot_no)
            .with_for_update()
        )).all())
        for event in due:
            outcome = await cls._apply_one(session, event, moment)
            if outcome == APPLIED:
                counts["applied"] += 1
            else:
                counts["skipped"] += 1

        await cls._close_stale(session, moment, counts)
        for key, value in counts.items():
            if stats is not None:
                stats[f"events_{key}"] = value
        if any(counts.values()):
            logger.info("inv2_event_tick %s", counts)
        return counts

    @classmethod
    async def _close_stale(cls, session: AsyncSession, now: datetime, counts: dict) -> None:
        """Close finished windows and skip events whose battle is gone."""
        open_events = list((await session.scalars(
            select(Inv2RandomEvent)
            .where(Inv2RandomEvent.status.in_(OPEN_STATUSES))
            .order_by(Inv2RandomEvent.id)
            .with_for_update()
        )).all())
        if not open_events:
            return
        cache: dict[int, Inv2Invasion] = {}
        for event in open_events:
            invasion = cache.get(event.invasion_id)
            if invasion is None:
                invasion = await session.get(Inv2Invasion, event.invasion_id)
                cache[event.invasion_id] = invasion
            battle_over = (
                invasion is None or invasion.status != InvasionStatus.ACTIVE.value
            )
            # ``resolved_at`` doubles as the "caption already finalised"
            # marker: leaving it NULL on a CLOSED event with a delivered
            # message tells the presentation pass to remove the chest button.
            if battle_over:
                if event.status == ACTIVE:
                    event.status = CLOSED
                    event.resolved_at = None if event.message_id else now
                else:
                    event.status = SKIPPED
                    event.resolved_at = now
                counts["closed"] += 1
                continue
            if (
                event.status == ACTIVE
                and event.ends_at is not None
                and _utc(event.ends_at) <= now
            ):
                event.status = CLOSED
                event.resolved_at = None if event.message_id else now
                counts["closed"] += 1

    # -- chest ------------------------------------------------------------
    @classmethod
    async def claim_loot(
        cls,
        session: AsyncSession,
        event_id: int,
        user_id: int,
        *,
        group_id: int,
        message_id: Optional[int],
        now: Optional[datetime] = None,
    ) -> "LootClaimResult":
        """First-N claim, exactly once, under the event row lock.

        The caller owns the transaction. Two unique constraints
        (``(event_id, user_id)`` and ``(event_id, slot_no)``) are the final
        arbiter; the CAS counter below is what makes the common path fast and
        deterministic.
        """
        moment = _utc(now or datetime.now(timezone.utc))
        event = await session.scalar(
            select(Inv2RandomEvent)
            .where(Inv2RandomEvent.id == event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if event is None or event.category != catalog.LOOT:
            return LootClaimResult("missing", "این صندوق پیدا نشد.", event_id)
        if event.group_id != group_id or event.message_id != message_id:
            return LootClaimResult("wrong_message", "این صندوق به این پیام تعلق ندارد.", event_id)
        if event.status != ACTIVE:
            return LootClaimResult("closed", "این صندوق بسته شده است.", event_id)
        if event.ends_at is None or _utc(event.ends_at) <= moment:
            return LootClaimResult("closed", "زمان این صندوق تمام شده است.", event_id)
        max_claims = int(event.loot_max_claims or catalog.DEFAULT_LOOT_MAX_CLAIMS)

        previous = await session.scalar(
            select(Inv2EventClaim.id).where(
                Inv2EventClaim.event_id == event.id,
                Inv2EventClaim.user_id == user_id,
            )
        )
        if previous is not None:
            return LootClaimResult(
                "already", "قبلاً از این صندوق برداشته‌اید.", event_id,
                claimed=int(event.loot_claimed), max_claims=max_claims,
            )

        # A battle that already ended must not hand out a prize.
        invasion_status = await session.scalar(
            select(Inv2Invasion.status).where(Inv2Invasion.id == event.invasion_id)
        )
        if invasion_status != InvasionStatus.ACTIVE.value:
            return LootClaimResult("closed", "این یورش به پایان رسیده است.", event_id)

        result = await session.execute(
            update(Inv2RandomEvent)
            .where(
                Inv2RandomEvent.id == event.id,
                Inv2RandomEvent.status == ACTIVE,
                Inv2RandomEvent.loot_claimed < max_claims,
            )
            .execution_options(synchronize_session=False)
            .values(loot_claimed=Inv2RandomEvent.loot_claimed + 1)
        )
        if result.rowcount != 1:
            return LootClaimResult(
                "empty", "صندوقچه خالی شده است.", event_id,
                claimed=int(event.loot_claimed), max_claims=max_claims,
            )
        claimed = int(await session.scalar(
            select(Inv2RandomEvent.loot_claimed).where(Inv2RandomEvent.id == event.id)
        ))

        template = catalog.BY_CODE.get(event.template_code)
        loot = template.loot if template else None
        if loot is None:
            return LootClaimResult("missing", "این جایزه تعریف نشده است.", event_id)

        amount = int(event.loot_amount or loot.default_amount)
        # Grant through the canonical service BEFORE the claim row is flushed
        # so a reward failure rolls the counter increment back with it.
        if loot.is_wallet:
            await AppleWalletService.earn(
                session,
                user_id,
                amount,
                reason="inv2_event_loot",
                ref_type="inv2_random_event",
                ref_id=event.id,
                idempotency_key=f"inv2evt:{event.id}:{user_id}",
            )
        else:
            await ItemService.grant(session, user_id, loot.reward_type, amount)

        session.add(Inv2EventClaim(
            event_id=event.id,
            user_id=user_id,
            slot_no=claimed,
            reward_type=loot.reward_type,
            reward_amount=amount,
            claimed_at=moment,
        ))
        await session.flush()
        event.loot_claimed = claimed

        label = (
            f"{fa_digits(amount)} سیب" if loot.is_wallet
            else f"{fa_digits(amount)} {loot.label_fa}"
        )
        return LootClaimResult(
            "ok",
            f"🎁 {label} گرفتی!",
            event_id,
            claimed=claimed,
            max_claims=max_claims,
            reward_label=label,
        )


@dataclass(frozen=True)
class LootClaimResult:
    """Outcome of one chest click; ``status`` drives the Persian answer."""

    status: str
    message_fa: str
    event_id: Optional[int] = None
    claimed: int = 0
    max_claims: int = 0
    reward_label: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


async def claim_loot(
    session: AsyncSession,
    event_id: int,
    user_id: int,
    *,
    group_id: int,
    message_id: Optional[int],
    now: Optional[datetime] = None,
) -> "LootClaimResult":
    """Module-level alias so callers need one obvious import."""
    return await InvasionEventScheduler.claim_loot(
        session, event_id, user_id,
        group_id=group_id, message_id=message_id, now=now,
    )


async def winners_for_event(session: AsyncSession, event_id: int) -> list[str]:
    """Display names of the players who already claimed, oldest first."""
    from sib.core.text import plain_display_name

    rows = (await session.execute(
        select(User.first_name, User.username)
        .join(Inv2EventClaim, Inv2EventClaim.user_id == User.id)
        .where(Inv2EventClaim.event_id == event_id)
        .order_by(Inv2EventClaim.slot_no)
    )).all()
    names = []
    for first_name, username in rows:
        names.append(plain_display_name(first_name or username, "مدافع"))
    return names
