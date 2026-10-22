"""Domain service for the global Halloween event («سیبومورتیس» / Sibomortis).

All event logic is gated behind ``system_config['halloween_enabled']``.

Lock hierarchy (extending ``services/lock_order.py`` with zero cycles):
    Rank -1  halloween_loot_drops    (ONLY in claim_group_drop, before users)
    Rank  0  users                   (lock_user_row — canonical per-user mutex)
    Rank  4  player_cards            (Amber acquisition at 25% milestone)
    Rank  5  user_items              (pumpkin_seed & item grants/consumption)
    Rank  6  apple_wallets           (normal Apple awards)
    Rank  7  player_progressions     (collection XP awards)
    Rank  8  halloween_event_state   (global boss HP update — ALWAYS LAST in attack!)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import (
    Character,
    Group,
    HalloweenAttack,
    HalloweenEventState,
    HalloweenLootDrop,
    HalloweenLootDropClaim,
    HalloweenRewardClaim,
    HalloweenSeedCooldown,
    User,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.card_progression import card_attributes
from sib.services.characters import CharacterService
from sib.services.config_store import ConfigService
from sib.services.invasion_v2.damage import compute_damage
from sib.services.items import ItemService
from sib.services.lock_order import lock_user_row
from sib.services.player_cards import DuplicateCardError, PlayerCardService

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS_HALLOWEEN_DIR = REPO_ROOT / "assets" / "halloween"
SIBOMORTIS_IMAGE_PATH = ASSETS_HALLOWEEN_DIR / "sibomortis.jpg"
AMBER_CARD_IMAGE_PATH = REPO_ROOT / "assets" / "characters" / "amber.jpg"

LOOT_DROP_VARIANTS: Dict[str, Tuple[str, Path]] = {
    "pumpkin_chest": (
        "🎃 یک **صندوقچهٔ کدوی افسانه‌ای** در گروه ظاهر شد! ۳ نفر اولی که دکمه را بزنند ۱ تا ۳ دونه کدو می‌گیرند!",
        ASSETS_HALLOWEEN_DIR / "loot_pumpkin_chest.jpg",
    ),
    "lantern_crate": (
        "🕯 **فانوس شکستهٔ شکارچی سایه** وسط گروه پیدا شد! ۳ نفر اول ۱ تا ۳ دونه کدو غنیمت می‌گیرند!",
        ASSETS_HALLOWEEN_DIR / "loot_lantern_crate.jpg",
    ),
    "autumn_hollow": (
        "🍂 در **کندهٔ جادویی باغ خزان** چند دونه کدوی نورانی می‌درخشد! ۳ نفر اول سهمیه می‌گیرند!",
        ASSETS_HALLOWEEN_DIR / "loot_autumn_hollow.jpg",
    ),
}

MILESTONES: Tuple[int, ...] = (75, 50, 25, 0)
BASE_UNARMED_POWER = 10
TOP3_VICTORY_BONUS_APPLES = 1000

DAY7_WEAKENING_ANNOUNCEMENT = (
    "💀 سیبومورتیس به شدت ضعیف شده است! زمان ضربه نهایی فرا رسیده!"
)
DEFEAT_ANNOUNCEMENT = "🎉 سیبومورتیس شکست خورد!"

LOOT_COOLDOWN_ALERT = (
    "⏳ شما به تازگی غنیمت گرفته‌اید. لطفاً بعداً تلاش کنید."
)
NO_SEEDS_ALERT = "🎃 شما دونه کدو ندارید! ابتدا غنیمت بگیرید."
EVENT_DISABLED_ALERT = "🎃 رویداد هالووین در حال حاضر فعال نیست."


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def phase_for_hp(boss_hp: int, max_hp: int) -> int:
    """Global boss phase 1..5 derived monotonically from ``boss_hp / max_hp``."""
    if boss_hp <= 0:
        return 5
    if boss_hp <= max_hp * 25 // 100:
        return 4
    if boss_hp <= max_hp * 50 // 100:
        return 3
    if boss_hp <= max_hp * 75 // 100:
        return 2
    return 1


def milestone_hp_threshold(max_hp: int, milestone_pct: int) -> int:
    """Maximum ``boss_hp`` at which ``milestone_pct`` becomes unlocked."""
    if milestone_pct == 0:
        return 0
    return (max_hp * milestone_pct) // 100


class HalloweenError(ValueError):
    """Player-facing Halloween error with Persian message."""


class HalloweenDisabledError(HalloweenError):
    """Raised when ``halloween_enabled`` is False."""


class HalloweenCooldownError(HalloweenError):
    """Raised when a player presses Claim Loot while still on cooldown."""

    def __init__(self, remaining_seconds: int) -> None:
        self.remaining_seconds = max(1, int(remaining_seconds))
        super().__init__(LOOT_COOLDOWN_ALERT)


class HalloweenNoSeedsError(HalloweenError):
    """Raised when attacking without any ``pumpkin_seed``."""

    def __init__(self) -> None:
        super().__init__(NO_SEEDS_ALERT)


class HalloweenBossDefeatedError(HalloweenError):
    """Raised when attacking after ``boss_hp`` already reached zero."""


class HalloweenMilestoneLockedError(HalloweenError):
    """Raised when claiming a milestone before ``boss_hp`` reaches it."""


class HalloweenAlreadyClaimedError(HalloweenError):
    """Raised when attempting to claim a milestone or chest twice."""


@dataclass(frozen=True)
class SeedClaimOutcome:
    seeds_granted: int
    total_seeds: int
    next_claim_at: datetime


@dataclass(frozen=True)
class AttackOutcome:
    damage: int
    is_crit: bool
    boss_hp_after: int
    max_hp: int
    remaining_seeds: int
    card_name_fa: Optional[str]


@dataclass(frozen=True)
class MilestoneStatus:
    milestone_pct: int
    threshold_hp: int
    state: str  # "locked" | "unlocked" | "claimed"


@dataclass(frozen=True)
class MilestoneClaimOutcome:
    milestone_pct: int
    summary: Dict[str, object]
    unlocked_amber: bool = False


@dataclass(frozen=True)
class DropClaimOutcome:
    drop_id: int
    seeds_granted: int
    claimed_count: int
    max_claims: int
    depleted: bool


@dataclass(frozen=True)
class MaintenanceOutcome:
    weakened: bool = False
    defeated: bool = False
    spawned_drop_ids: Tuple[int, ...] = ()
    expired_drop_ids: Tuple[int, ...] = ()
    group_ids: Tuple[int, ...] = ()


class HalloweenService:
    """Authoritative business logic for the 7-day Sibomortis event."""

    @staticmethod
    async def is_enabled(session: Optional[AsyncSession] = None) -> bool:
        """True strictly when ``halloween_enabled`` is truthy in config."""
        async def _read(s: AsyncSession) -> bool:
            val = await ConfigService.get(s, "halloween_enabled")
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return bool(val)
            if isinstance(val, str):
                return val.strip().lower() in ("true", "1", "yes")
            return False

        try:
            if session is not None:
                return await _read(session)
            from sib.database.connection import get_session

            async with get_session() as own:
                return await _read(own)
        except Exception:
            return False

    @classmethod
    async def ensure_active_event(
        cls,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
    ) -> HalloweenEventState:
        """Return the active global event row, creating the singleton if none exists."""
        moment = _utc(now or datetime.now(timezone.utc))
        state = await session.scalar(
            select(HalloweenEventState)
            .where(HalloweenEventState.is_active == True)  # noqa: E712
            .order_by(HalloweenEventState.id.desc())
            .limit(1)
        )
        if state is not None:
            return state

        default_max_hp = int(
            await ConfigService.get(session, "halloween_default_max_hp") or 500_000
        )
        duration_days = int(
            await ConfigService.get(session, "halloween_duration_days") or 7
        )
        state = HalloweenEventState(
            boss_hp=default_max_hp,
            max_hp=default_max_hp,
            current_phase=1,
            is_active=True,
            last_day_weakened=False,
            started_at=moment,
            ends_at=moment + timedelta(days=duration_days),
        )
        session.add(state)
        await session.flush()
        return state

    @classmethod
    async def claim_seed_loot(
        cls,
        session: AsyncSession,
        user_id: int,
        *,
        rng: Optional[random.Random] = None,
        now: Optional[datetime] = None,
    ) -> SeedClaimOutcome:
        """Claim 1–3 ``pumpkin_seed`` on a 4-hour per-user cooldown."""
        if not await cls.is_enabled(session):
            raise HalloweenDisabledError(EVENT_DISABLED_ALERT)

        moment = _utc(now or datetime.now(timezone.utc))
        rng = rng or random.Random()
        state = await cls.ensure_active_event(session, now=moment)
        cooldown_seconds = int(
            await ConfigService.get(session, "halloween_loot_cooldown_seconds")
            or 14_400
        )

        # Rank 0 lock: serializes concurrent clicks from the same player.
        await lock_user_row(session, user_id)

        cd = await session.get(HalloweenSeedCooldown, (state.id, user_id))
        if cd is not None:
            elapsed = (moment - _utc(cd.last_claimed_at)).total_seconds()
            if elapsed < cooldown_seconds:
                raise HalloweenCooldownError(int(cooldown_seconds - elapsed))

        granted = int(rng.randint(1, 3))
        if cd is None:
            cd = HalloweenSeedCooldown(
                event_id=state.id,
                user_id=user_id,
                last_claimed_at=moment,
                total_claims=1,
                total_seeds=granted,
            )
            session.add(cd)
        else:
            cd.last_claimed_at = moment
            cd.total_claims += 1
            cd.total_seeds += granted

        total_seeds = await ItemService.grant(
            session, user_id, "pumpkin_seed", granted
        )
        await session.flush()
        return SeedClaimOutcome(
            seeds_granted=granted,
            total_seeds=total_seeds,
            next_claim_at=moment + timedelta(seconds=cooldown_seconds),
        )

    @classmethod
    async def _resolve_player_attack_card(
        cls, session: AsyncSession, user_id: int
    ) -> Tuple[int, Optional[int], Optional[int], Optional[str]]:
        """Return ``(card_power, card_id, character_id, character_name_fa)``."""
        main_card = await PlayerCardService.get_main_card(session, user_id)
        if main_card is None:
            owned = await PlayerCardService.list_cards(session, user_id)
            main_card = owned[0] if owned else None
        if main_card is None:
            return BASE_UNARMED_POWER, None, None, None

        character = await session.get(Character, main_card.character_id)
        if character is None:
            return BASE_UNARMED_POWER, main_card.id, None, None

        power = max(1, int(card_attributes(main_card, character).power))
        return power, main_card.id, character.id, character.name_fa

    @classmethod
    async def attack_boss(
        cls,
        session: AsyncSession,
        user_id: int,
        *,
        group_id: Optional[int] = None,
        rng: Optional[random.Random] = None,
        now: Optional[datetime] = None,
    ) -> AttackOutcome:
        """Consume 1 ``pumpkin_seed``, deal Main Card damage to Sibomortis, log attack."""
        if not await cls.is_enabled(session):
            raise HalloweenDisabledError(EVENT_DISABLED_ALERT)

        moment = _utc(now or datetime.now(timezone.utc))
        rng = rng or random.Random()
        state_initial = await cls.ensure_active_event(session, now=moment)
        state_id = state_initial.id

        # Rank 0 lock: serialize per-player attacks first.
        await lock_user_row(session, user_id)

        have = await ItemService.get_total(session, user_id, "pumpkin_seed", for_update=True)
        if have < 1:
            raise HalloweenNoSeedsError()

        remaining_seeds = await ItemService.consume(session, user_id, "pumpkin_seed", 1)
        card_power, card_id, character_id, card_name_fa = await cls._resolve_player_attack_card(
            session, user_id
        )
        damage, is_crit = compute_damage(card_power, rng, crit_chance=0.10)
        damage = max(1, int(damage))

        # Rank 8 lock: acquire global boss row LAST for sub-millisecond hold.
        locked_state = await session.scalar(
            select(HalloweenEventState)
            .where(HalloweenEventState.id == state_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_state is None or not locked_state.is_active:
            raise HalloweenDisabledError(EVENT_DISABLED_ALERT)
        if locked_state.boss_hp <= 0:
            raise HalloweenBossDefeatedError("🎉 سیبومورتیس قبلاً شکست خورده است! از مسیر جوایز پاداش خود را بگیرید.")

        new_hp = max(0, int(locked_state.boss_hp) - damage)
        locked_state.boss_hp = new_hp
        locked_state.current_phase = phase_for_hp(new_hp, int(locked_state.max_hp))
        if new_hp == 0 and locked_state.finisher_user_id is None:
            locked_state.finisher_user_id = user_id

        session.add(
            HalloweenAttack(
                event_id=locked_state.id,
                user_id=user_id,
                group_id=group_id,
                card_id=card_id,
                character_id=character_id,
                damage=damage,
                is_crit=is_crit,
                boss_hp_after=new_hp,
                created_at=moment,
            )
        )
        await session.flush()
        return AttackOutcome(
            damage=damage,
            is_crit=is_crit,
            boss_hp_after=new_hp,
            max_hp=int(locked_state.max_hp),
            remaining_seeds=remaining_seeds,
            card_name_fa=card_name_fa,
        )

    @classmethod
    async def get_leaderboard(
        cls,
        session: AsyncSession,
        event_id: int,
        *,
        limit: int = 10,
    ) -> List[Tuple[int, str, int]]:
        """Return ``[(user_id, first_name, total_damage), ...]`` ordered deterministically."""
        stmt = (
            select(
                HalloweenAttack.user_id,
                User.first_name,
                func.sum(HalloweenAttack.damage).label("total_damage"),
            )
            .join(User, User.id == HalloweenAttack.user_id)
            .where(HalloweenAttack.event_id == event_id)
            .group_by(HalloweenAttack.user_id, User.first_name)
            .order_by(
                func.sum(HalloweenAttack.damage).desc(),
                func.min(HalloweenAttack.id).asc(),
            )
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(int(uid), name or "بازیکن", int(dmg)) for uid, name, dmg in rows]

    @classmethod
    async def get_reward_track(
        cls, session: AsyncSession, user_id: int, *, now: Optional[datetime] = None
    ) -> Tuple[HalloweenEventState, Tuple[MilestoneStatus, ...]]:
        """Return active event state + status of all 4 milestones for ``user_id``."""
        state = await cls.ensure_active_event(session, now=now)
        claimed = set(
            (
                await session.scalars(
                    select(HalloweenRewardClaim.milestone_pct).where(
                        HalloweenRewardClaim.event_id == state.id,
                        HalloweenRewardClaim.user_id == user_id,
                    )
                )
            ).all()
        )
        statuses: List[MilestoneStatus] = []
        for pct in MILESTONES:
            threshold = milestone_hp_threshold(int(state.max_hp), pct)
            if pct in claimed:
                st = "claimed"
            elif int(state.boss_hp) <= threshold:
                st = "unlocked"
            else:
                st = "locked"
            statuses.append(
                MilestoneStatus(
                    milestone_pct=pct, threshold_hp=threshold, state=st
                )
            )
        return state, tuple(statuses)

    @classmethod
    async def claim_milestone(
        cls,
        session: AsyncSession,
        user_id: int,
        milestone_pct: int,
        *,
        now: Optional[datetime] = None,
    ) -> MilestoneClaimOutcome:
        """Idempotently grant one unlocked milestone reward (75%, 50%, 25%, or 0%)."""
        if not await cls.is_enabled(session):
            raise HalloweenDisabledError(EVENT_DISABLED_ALERT)
        if milestone_pct not in MILESTONES:
            raise HalloweenError("مرحلهٔ جایزه معتبر نیست.")

        moment = _utc(now or datetime.now(timezone.utc))
        state = await cls.ensure_active_event(session, now=moment)
        threshold = milestone_hp_threshold(int(state.max_hp), milestone_pct)
        if int(state.boss_hp) > threshold:
            raise HalloweenMilestoneLockedError(
                "🔒 این مرحله هنوز باز نشده است! ابتدا باید جان سیبومورتیس به این مرز برسد."
            )

        # Rank 0 lock: serialize same-user concurrent clicks.
        await lock_user_row(session, user_id)

        existing = await session.scalar(
            select(HalloweenRewardClaim.id).where(
                HalloweenRewardClaim.event_id == state.id,
                HalloweenRewardClaim.user_id == user_id,
                HalloweenRewardClaim.milestone_pct == milestone_pct,
            )
        )
        if existing is not None:
            raise HalloweenAlreadyClaimedError("✅ شما این جایزه را قبلاً دریافت کرده‌اید.")

        # Insert durable claim row FIRST so UNIQUE (event_id, user_id, milestone_pct)
        # acts as the database-level single-winner gate across workers/retries.
        claim_row = HalloweenRewardClaim(
            event_id=state.id,
            user_id=user_id,
            milestone_pct=milestone_pct,
            reward_summary_json="{}",
            claimed_at=moment,
        )
        session.add(claim_row)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise HalloweenAlreadyClaimedError(
                "✅ شما این جایزه را قبلاً دریافت کرده‌اید."
            ) from exc

        summary: Dict[str, object] = {}
        unlocked_amber = False

        if milestone_pct == 75:
            await ItemService.grant(session, user_id, "pumpkin_seed", 2)
            await AppleWalletService.earn(
                session,
                user_id,
                150,
                reason="halloween_milestone",
                ref_type="halloween_claim",
                ref_id=claim_row.id,
                idempotency_key=f"hw_ms:{state.id}:{user_id}:75",
            )
            summary = {"apples": 150, "pumpkin_seed": 2}

        elif milestone_pct == 50:
            await ItemService.grant(session, user_id, "magic_powder", 3)
            await ItemService.grant(session, user_id, "apple_gold", 2)
            summary = {"magic_powder": 3, "apple_gold": 2}

        elif milestone_pct == 25:
            amber = await CharacterService.get_by_code(session, "amber")
            if amber is None:
                from sib.database.connection import seed_characters

                await seed_characters(session)
                amber = await CharacterService.get_by_code(session, "amber")
            if amber is not None:
                try:
                    await PlayerCardService.acquire(session, user_id, amber.id)
                    unlocked_amber = True
                    from sib.services.player_xp import PlayerXPService

                    await PlayerXPService.award_character_collection(
                        session, user_id=user_id, character_id=amber.id
                    )
                    summary["card"] = "amber"
                except DuplicateCardError:
                    summary["card_already_owned"] = "amber"
            await ItemService.grant(session, user_id, "apple_diamond", 1)
            summary["apple_diamond"] = 1

        elif milestone_pct == 0:
            await ItemService.grant(session, user_id, "apple_obsidian", 1)
            top3 = await cls.get_leaderboard(session, state.id, limit=3)
            top3_ids = {uid for uid, _name, _dmg in top3}
            is_top3 = user_id in top3_ids
            apples_total = 500 + (TOP3_VICTORY_BONUS_APPLES if is_top3 else 0)
            await AppleWalletService.earn(
                session,
                user_id,
                apples_total,
                reason="halloween_milestone",
                ref_type="halloween_claim",
                ref_id=claim_row.id,
                idempotency_key=f"hw_ms:{state.id}:{user_id}:0",
            )
            summary = {
                "apples": 500,
                "apple_obsidian": 1,
                "top3_bonus_apples": TOP3_VICTORY_BONUS_APPLES if is_top3 else 0,
            }

        claim_row.reward_summary_json = json.dumps(summary, ensure_ascii=False)
        await session.flush()
        return MilestoneClaimOutcome(
            milestone_pct=milestone_pct,
            summary=summary,
            unlocked_amber=unlocked_amber,
        )

    @classmethod
    async def claim_group_drop(
        cls,
        session: AsyncSession,
        drop_id: int,
        user_id: int,
        *,
        rng: Optional[random.Random] = None,
        now: Optional[datetime] = None,
    ) -> DropClaimOutcome:
        """Claim 1–3 ``pumpkin_seed`` from an active group chest (first 3 winners)."""
        if not await cls.is_enabled(session):
            raise HalloweenDisabledError(EVENT_DISABLED_ALERT)

        moment = _utc(now or datetime.now(timezone.utc))
        rng = rng or random.Random()

        # Rank -1 lock BEFORE lock_user_row (Rank 0).
        drop = await session.scalar(
            select(HalloweenLootDrop)
            .where(HalloweenLootDrop.id == drop_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if drop is None or drop.status != "ACTIVE" or _utc(drop.expires_at) <= moment:
            raise HalloweenError("⏳ مهلت این صندوق به پایان رسیده است.")
        if drop.claimed_count >= drop.max_claims:
            drop.status = "DEPLETED"
            await session.flush()
            raise HalloweenError("🎃 این صندوق کامل خالی شده است!")

        dup = await session.scalar(
            select(HalloweenLootDropClaim.id).where(
                HalloweenLootDropClaim.drop_id == drop.id,
                HalloweenLootDropClaim.user_id == user_id,
            )
        )
        if dup is not None:
            raise HalloweenAlreadyClaimedError(
                "✅ شما قبلاً از این صندوق دونه کدو گرفته‌اید."
            )

        await lock_user_row(session, user_id)

        seeds = int(rng.randint(1, 3))
        slot_no = int(drop.claimed_count) + 1
        drop.claimed_count = slot_no
        if drop.claimed_count >= drop.max_claims:
            drop.status = "DEPLETED"

        session.add(
            HalloweenLootDropClaim(
                drop_id=drop.id,
                user_id=user_id,
                slot_no=slot_no,
                seeds_granted=seeds,
                claimed_at=moment,
            )
        )
        try:
            await session.flush()
        except IntegrityError as exc:
            raise HalloweenAlreadyClaimedError(
                "✅ شما قبلاً از این صندوق دونه کدو گرفته‌اید."
            ) from exc

        await ItemService.grant(session, user_id, "pumpkin_seed", seeds)
        await session.flush()
        return DropClaimOutcome(
            drop_id=drop.id,
            seeds_granted=seeds,
            claimed_count=drop.claimed_count,
            max_claims=drop.max_claims,
            depleted=(drop.status == "DEPLETED"),
        )

    @classmethod
    async def run_maintenance_tick(
        cls,
        session: AsyncSession,
        *,
        rng: Optional[random.Random] = None,
        now: Optional[datetime] = None,
    ) -> MaintenanceOutcome:
        """Single atomic DB pass for Day-7 weakening, defeat stamp, and group drops."""
        if not await cls.is_enabled(session):
            return MaintenanceOutcome()

        moment = _utc(now or datetime.now(timezone.utc))
        rng = rng or random.Random()

        state = await cls.ensure_active_event(session, now=moment)
        locked = await session.scalar(
            select(HalloweenEventState)
            .where(HalloweenEventState.id == state.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None or not locked.is_active:
            return MaintenanceOutcome()

        weakened = False
        defeated = False

        # 1. Day-7 Weakening: >= 6 days since started_at, runs AT MOST ONCE.
        if (
            not locked.last_day_weakened
            and locked.boss_hp > 0
            and moment >= _utc(locked.started_at) + timedelta(days=6)
        ):
            locked.hp_before_weaken = int(locked.boss_hp)
            new_hp = max(1, (int(locked.boss_hp) * 20) // 100)
            locked.boss_hp = new_hp
            locked.current_phase = phase_for_hp(new_hp, int(locked.max_hp))
            locked.last_day_weakened = True
            locked.weakened_at = moment
            weakened = True

        # 2. Defeat detection: stamp defeated_at once so announcement never repeats.
        if int(locked.boss_hp) == 0 and locked.defeated_at is None:
            locked.defeated_at = moment
            locked.current_phase = 5
            defeated = True

        active_group_ids = list(
            (
                await session.scalars(
                    select(Group.id)
                    .where(Group.is_active == True)  # noqa: E712
                    .order_by(Group.id.asc())
                )
            ).all()
        )

        # 3. Expire stale active drops & spawn due group drops while boss is alive.
        expired_drops = list(
            (
                await session.scalars(
                    select(HalloweenLootDrop).where(
                        HalloweenLootDrop.status == "ACTIVE",
                        HalloweenLootDrop.expires_at <= moment,
                    )
                )
            ).all()
        )
        expired_ids: List[int] = []
        for drop in expired_drops:
            drop.status = "EXPIRED"
            expired_ids.append(drop.id)

        spawned_ids: List[int] = []
        if int(locked.boss_hp) > 0 and active_group_ids:
            interval_seconds = int(
                await ConfigService.get(
                    session, "halloween_group_drop_interval_seconds"
                )
                or 10_800
            )
            window_seconds = int(
                await ConfigService.get(
                    session, "halloween_group_drop_window_seconds"
                )
                or 300
            )
            max_claims = int(
                await ConfigService.get(
                    session, "halloween_group_drop_max_claims"
                )
                or 3
            )
            cutoff = moment - timedelta(seconds=interval_seconds)
            variants = tuple(LOOT_DROP_VARIANTS.keys())

            for gid in active_group_ids:
                recent = await session.scalar(
                    select(HalloweenLootDrop.id)
                    .where(
                        HalloweenLootDrop.group_id == gid,
                        HalloweenLootDrop.created_at > cutoff,
                    )
                    .limit(1)
                )
                if recent is not None:
                    continue
                variant_code = variants[int(rng.randint(0, len(variants) - 1))]
                new_drop = HalloweenLootDrop(
                    event_id=locked.id,
                    group_id=int(gid),
                    variant_code=variant_code,
                    status="ACTIVE",
                    max_claims=max_claims,
                    claimed_count=0,
                    expires_at=moment + timedelta(seconds=window_seconds),
                    created_at=moment,
                )
                session.add(new_drop)
                await session.flush()
                spawned_ids.append(new_drop.id)

        await session.flush()
        return MaintenanceOutcome(
            weakened=weakened,
            defeated=defeated,
            spawned_drop_ids=tuple(spawned_ids),
            expired_drop_ids=tuple(expired_ids),
            group_ids=tuple(int(gid) for gid in active_group_ids),
        )

    @classmethod
    async def publish_maintenance_notifications(
        cls, bot, outcome: MaintenanceOutcome
    ) -> None:
        """Best-effort Telegram delivery AFTER the DB transaction has committed."""
        if bot is None:
            return
        from aiogram.types import FSInputFile
        from sib.database.connection import get_session
        from sib.keyboards.halloween import get_loot_drop_keyboard
        from sib.services.telegram_ui import safe_edit_screen, safe_send_screen

        if outcome.weakened:
            for gid in outcome.group_ids:
                try:
                    await safe_send_screen(
                        bot, gid, DAY7_WEAKENING_ANNOUNCEMENT, parse_mode=None
                    )
                except Exception:
                    pass

        if outcome.defeated:
            for gid in outcome.group_ids:
                try:
                    await safe_send_screen(
                        bot, gid, DEFEAT_ANNOUNCEMENT, parse_mode=None
                    )
                except Exception:
                    pass

        for drop_id in outcome.spawned_drop_ids:
            try:
                async with get_session() as session:
                    drop = await session.get(HalloweenLootDrop, drop_id)
                    if drop is None or drop.status != "ACTIVE":
                        continue
                    caption, image_path = LOOT_DROP_VARIANTS.get(
                        drop.variant_code,
                        next(iter(LOOT_DROP_VARIANTS.values())),
                    )
                    photo = (
                        FSInputFile(str(image_path))
                        if image_path.is_file()
                        else None
                    )
                    kb = get_loot_drop_keyboard(
                        drop.id, drop.claimed_count, drop.max_claims
                    )
                sent = await safe_send_screen(
                    bot,
                    drop.group_id,
                    caption,
                    photo=photo,
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
                msg_id = getattr(sent, "message_id", None) if sent is not None else None
                if isinstance(msg_id, int):
                    async with get_session() as session:
                        live = await session.get(HalloweenLootDrop, drop_id)
                        if live is not None:
                            live.message_id = msg_id
            except Exception:
                pass

        for drop_id in outcome.expired_drop_ids:
            try:
                async with get_session() as session:
                    drop = await session.get(HalloweenLootDrop, drop_id)
                if drop is not None and drop.message_id:
                    await safe_edit_screen(
                        bot,
                        drop.group_id,
                        drop.message_id,
                        "⏳ مهلت این صندوقچهٔ هالووین به پایان رسید.",
                        reply_markup=None,
                        parse_mode=None,
                    )
            except Exception:
                pass
