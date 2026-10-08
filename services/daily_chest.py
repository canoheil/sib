"""Daily Sib Treasure Chest («صندوقچه گنج روزانه»).

One claim per player per UTC daily cycle, persisted in ``daily_chest_claims``
whose ``(user_id, claim_day)`` unique constraint is the database-level
arbiter against duplicate claims (concurrent presses, duplicate Telegram
updates, retries, multiple bot processes). The daily boundary is the
project's established UTC calendar day (same convention as trades_per_day,
flower_daily_limit_per_direction and the XP daily caps).

Reward pool (config ``daily_chest_weights``, RELATIVE weights normalized at
roll time) covers the six categories in rising rarity order: normal Sib
currency, seedlings, Golden/Diamond/Obsidian Apples and the collectible-card
jackpot. Delivery ALWAYS goes through the canonical services —
AppleWalletService (ledger), SeedlingService, ItemService and
PlayerCardService — inside the caller's transaction, so one successful
opening is exactly one claim row and one delivered reward, and any failure
rolls back both.

Selection is a single weighted roll over float weights (the repo's
``roll_drop``) with an injectable RNG — never a reroll loop, and tiny
weights (the 0.05% card) stay selectable without integer rounding.
"""

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import (
    Character,
    DailyChestClaim,
    PlayerCard,
    ReleaseStatus,
    User,
)
from sib.database.models.daily_chest import (
    REWARD_APPLES,
    REWARD_CARD,
    REWARD_DIAMOND,
    REWARD_GOLD,
    REWARD_OBSIDIAN,
    REWARD_SEEDLINGS,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import ItemService, roll_drop
from sib.services.sib_claim import fa_count, fa_digits

CLAIM_REASON = "daily_chest"

# Rising rarity order. Also the iteration order for every map built from
# the config, so presentation and cumulative-weight walks stay stable.
KNOWN_REWARDS: Tuple[str, ...] = (
    REWARD_APPLES,
    REWARD_SEEDLINGS,
    REWARD_GOLD,
    REWARD_DIAMOND,
    REWARD_OBSIDIAN,
    REWARD_CARD,
)

REWARD_EMOJI: Dict[str, str] = {
    REWARD_APPLES: "🍎",
    REWARD_SEEDLINGS: "🌱",
    REWARD_GOLD: "🪙",
    REWARD_DIAMOND: "💎",
    REWARD_OBSIDIAN: "🌑",
    REWARD_CARD: "🎴",
}

REWARD_LABEL: Dict[str, str] = {
    REWARD_APPLES: "سیب معمولی",
    REWARD_SEEDLINGS: "نهال",
    REWARD_GOLD: "سیب طلایی",
    REWARD_DIAMOND: "سیب الماسی",
    REWARD_OBSIDIAN: "سیب ابسیدینی",
    REWARD_CARD: "کارت شخصیت",
}

MAX_LEVEL = 30  # mirrors services.player_xp.MAX_LEVEL (one leveling system)


class DailyChestError(ValueError):
    """Raised for invalid daily-chest operations (player-facing Persian)."""


class ChestDisabledError(DailyChestError):
    """Raised when the chest is switched off via configuration."""


class ChestConfigError(ValueError):
    """Raised when the configured reward pool is invalid.

    Operator-facing (English) on purpose: a broken distribution must fail
    clearly at roll time instead of silently changing reward probabilities.
    """


# ---------------------------------------------------------------------------
# Daily boundary (project convention: UTC calendar day)
# ---------------------------------------------------------------------------
def utc_day(now: Optional[datetime] = None) -> str:
    """The UTC daily cycle key, ``YYYY-MM-DD`` (naive input = UTC)."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def next_reset_at(now: Optional[datetime] = None) -> datetime:
    """Next UTC midnight (the moment the next daily cycle starts)."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    tomorrow = (moment + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tomorrow


# ---------------------------------------------------------------------------
# Configuration (validated once per claim)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChestConfig:
    """Validated chest configuration. ``weights`` are normalized to 1.0."""

    enabled: bool
    weights: Dict[str, float]
    quantities: Dict[str, Tuple[int, int]]
    progression_enabled: bool
    apples_per_tier: int
    tier_levels: Tuple[int, ...]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_quantities(raw: Any) -> Dict[str, Tuple[int, int]]:
    if not isinstance(raw, dict):
        raise ChestConfigError("daily_chest_quantities must be a mapping")
    unknown = set(raw) - set(KNOWN_REWARDS)
    if unknown:
        raise ChestConfigError(f"unknown reward identifiers in quantities: {sorted(unknown)}")
    quantities: Dict[str, Tuple[int, int]] = {}
    for key in KNOWN_REWARDS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, dict):
            low, high = value.get("min"), value.get("max")
            if (
                not isinstance(low, int)
                or isinstance(low, bool)
                or not isinstance(high, int)
                or isinstance(high, bool)
            ):
                raise ChestConfigError(
                    f"quantity range for {key!r} needs integer min/max"
                )
            if low < 1 or high < low:
                raise ChestConfigError(
                    f"invalid quantity range for {key!r}: min={low} max={high}"
                )
            quantities[key] = (low, high)
        else:
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ChestConfigError(
                    f"quantity for {key!r} must be an integer >= 1 or a min/max range"
                )
            quantities[key] = (value, value)
    return quantities


def _validate_progression(raw: Any) -> Tuple[bool, int, Tuple[int, ...]]:
    if raw is None:
        return False, 0, ()
    if not isinstance(raw, dict):
        raise ChestConfigError("daily_chest_progression must be a mapping")
    enabled = bool(raw.get("enabled", True))
    per_tier = raw.get("apples_per_tier", 0)
    if not isinstance(per_tier, int) or isinstance(per_tier, bool) or per_tier < 0:
        raise ChestConfigError("apples_per_tier must be an integer >= 0")
    tiers_raw = raw.get("tier_levels", [])
    if not isinstance(tiers_raw, (list, tuple)):
        raise ChestConfigError("tier_levels must be a list of levels")
    tiers: List[int] = []
    for tier in tiers_raw:
        if not isinstance(tier, int) or isinstance(tier, bool) or not 1 <= tier <= MAX_LEVEL:
            raise ChestConfigError(
                f"tier level {tier!r} must be an integer in 1..{MAX_LEVEL}"
            )
        tiers.append(tier)
    return enabled, per_tier, tuple(sorted(set(tiers)))


def validate_chest_config(
    raw_weights: Any,
    raw_quantities: Any,
    raw_progression: Any = None,
    *,
    enabled: bool = True,
) -> ChestConfig:
    """Validate the raw config maps into a normalized ``ChestConfig``.

    Rules: known reward identifiers only; finite weights >= 0 with a
    positive total (negatives, NaN and infinities are rejected, never
    clamped); every positive-weight reward needs a quantity (integer >= 1
    or an inclusive min/max range); the normalized weights must sum to 1.
    Raises ``ChestConfigError`` on every violation.
    """
    if not isinstance(raw_weights, dict):
        raise ChestConfigError("daily_chest_weights must be a mapping")
    unknown = set(raw_weights) - set(KNOWN_REWARDS)
    if unknown:
        raise ChestConfigError(f"unknown reward identifiers: {sorted(unknown)}")

    weights: Dict[str, float] = {}
    for key in KNOWN_REWARDS:
        if key not in raw_weights:
            continue
        value = raw_weights[key]
        if not _is_number(value) or not math.isfinite(float(value)):
            raise ChestConfigError(f"weight for {key!r} must be a finite number")
        if float(value) < 0:
            raise ChestConfigError(f"weight for {key!r} must not be negative")
        weights[key] = float(value)

    total = sum(weights.values())
    if not weights or total <= 0:
        raise ChestConfigError("daily_chest_weights must have a positive total")

    quantities = _validate_quantities(raw_quantities)
    for key in weights:
        if key not in quantities:
            raise ChestConfigError(f"missing quantity for reward {key!r}")

    normalized = {key: value / total for key, value in weights.items()}
    if not math.isclose(sum(normalized.values()), 1.0, rel_tol=1e-9, abs_tol=1e-9):
        raise ChestConfigError("normalized weights do not sum to 1")

    progression_enabled, per_tier, tiers = _validate_progression(raw_progression)
    return ChestConfig(
        enabled=bool(enabled),
        weights=normalized,
        quantities=quantities,
        progression_enabled=progression_enabled,
        apples_per_tier=per_tier,
        tier_levels=tiers,
    )


def progression_apples_bonus(level: int, config: ChestConfig) -> int:
    """Extra Apples from the ONE existing Account Level (applies to the
    apples outcome only; odds and every other outcome are untouched)."""
    if not config.progression_enabled:
        return 0
    tiers_hit = sum(1 for tier in config.tier_levels if int(level) >= tier)
    return config.apples_per_tier * tiers_hit


def roll_outcome(
    config: ChestConfig,
    rng: Optional[random.Random] = None,
    *,
    deliverable: Optional[Tuple[str, ...]] = None,
) -> Tuple[str, int]:
    """ONE weighted roll: ``(reward_type, quantity)``.

    ``rng`` needs ``.random()`` and ``.randint(a, b)`` (``random.Random``
    conforms; tests inject deterministic stubs). ``deliverable`` optionally
    restricts the pool to outcomes that can actually be granted right now
    (weights renormalize over the restriction — the pool is built BEFORE
    the single roll, never rerolled after it).
    """
    allowed = set(deliverable) if deliverable is not None else set(KNOWN_REWARDS)
    pool = {
        key: config.weights[key]
        for key in KNOWN_REWARDS
        if key in config.weights and key in allowed and config.weights[key] > 0
    }
    if not pool:
        raise ChestConfigError("no deliverable reward in the configured pool")

    rng = rng or random.Random()
    rolled = roll_drop(pool, rng=rng)
    if rolled is None:  # pragma: no cover - pool validation guarantees a hit
        raise ChestConfigError("reward pool is empty after normalization")
    reward_type = rolled[0]
    low, high = config.quantities[reward_type]
    quantity = int(rng.randint(low, high)) if high > low else low
    return reward_type, quantity


# ---------------------------------------------------------------------------
# Result + Persian presentation
# ---------------------------------------------------------------------------
@dataclass
class ChestClaimResult:
    """Outcome of one open attempt (claimed / already claimed / disabled)."""

    status: str  # "claimed" | "already_claimed" | "disabled"
    next_reset: datetime
    reward_type: Optional[str] = None
    quantity: int = 0
    level_bonus: int = 0
    player_level: Optional[int] = None
    claim: Optional[DailyChestClaim] = None
    character: Optional[Character] = None
    card: Optional[PlayerCard] = None


def format_remaining(until: datetime, now: datetime) -> str:
    """Persian 'X hours and Y minutes' until ``until`` (never negative)."""
    seconds = max(0, int((until - now).total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes = (rest + 59) // 60  # never advertise a zero wait mid-hour
    if hours and minutes:
        return f"{fa_digits(hours)} ساعت و {fa_digits(minutes)} دقیقه"
    if hours:
        return f"{fa_count(hours)} ساعت"
    if minutes:
        return f"{fa_count(minutes)} دقیقه"
    return "کمتر از یک دقیقه"


def format_next_reset(next_reset: datetime, now: datetime) -> str:
    return (
        f"باز شدن بعدی: فردا {fa_digits('00:00')} به‌وقت گرینویچ "
        f"({format_remaining(next_reset, now)} دیگه)"
    )


def format_card_line(character: Character, card: PlayerCard) -> str:
    """Granted-card identity in the existing card presentation format."""
    from sib.services.card_progression import card_attributes

    stats = card_attributes(card, character)
    return (
        f"**{character.name_fa}** — سطح {fa_digits(card.level)} "
        f"| 💪{stats.power} ⚡{stats.speed} 🧠{stats.intelligence}"
    )


def format_reward_line(
    reward_type: str,
    quantity: int,
    *,
    level_bonus: int = 0,
    player_level: Optional[int] = None,
    character: Optional[Character] = None,
    card: Optional[PlayerCard] = None,
) -> str:
    """One-line (or card block) Persian description of the granted reward."""
    emoji = REWARD_EMOJI.get(reward_type, "🎁")
    if reward_type == REWARD_CARD and character is not None and card is not None:
        return (
            f"{emoji} جکپات! کارت شخصیت مال تو شد! 🎉\n\n"
            f"{format_card_line(character, card)}"
        )
    label = REWARD_LABEL.get(reward_type, reward_type)
    count = fa_count(quantity) if quantity <= 10 else fa_digits(quantity)
    line = f"{emoji} {count} تا {label} مال تو شد!"
    if reward_type == REWARD_GOLD:
        line += " ✨"
    elif reward_type == REWARD_DIAMOND:
        line += " درخشان... ✨"
    elif reward_type == REWARD_OBSIDIAN:
        line += " 🤯"
    if level_bonus > 0 and player_level is not None:
        line += (
            f"\n🎖 پاداش سطح {fa_digits(player_level)}: "
            f"+{fa_digits(level_bonus)} سیب!"
        )
    return line


def format_open_result(result: ChestClaimResult, now: datetime) -> str:
    """Full result screen after a successful open."""
    reward_line = format_reward_line(
        result.reward_type or "",
        result.quantity,
        level_bonus=result.level_bonus,
        player_level=result.player_level,
        character=result.character,
        card=result.card,
    )
    return (
        "🎁 صندوقچه گنج روزانه سیب\n\n"
        "صندوقچه باز شد و این چیزی بود که توش بود:\n"
        f"{reward_line}\n\n"
        f"{format_next_reset(result.next_reset, now)}"
    )


def format_claimed_line(claim: DailyChestClaim) -> str:
    """Compact summary of today's already-claimed reward (audit columns)."""
    emoji = REWARD_EMOJI.get(claim.reward_type, "🎁")
    label = REWARD_LABEL.get(claim.reward_type, claim.reward_type)
    if claim.reward_type == REWARD_CARD:
        return f"{emoji} {label}"
    count = fa_count(claim.quantity) if claim.quantity <= 10 else fa_digits(claim.quantity)
    return f"{emoji} {count} تا {label}"


class DailyChestService:
    """Persistent daily chest: eligibility, one roll, canonical delivery."""

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @classmethod
    async def load_config(cls, session: AsyncSession) -> ChestConfig:
        return validate_chest_config(
            await ConfigService.get(session, "daily_chest_weights"),
            await ConfigService.get(session, "daily_chest_quantities"),
            await ConfigService.get(session, "daily_chest_progression"),
            enabled=bool(await ConfigService.get(session, "daily_chest_enabled")),
        )

    # ------------------------------------------------------------------
    # Status (screen rendering)
    # ------------------------------------------------------------------
    @classmethod
    async def get_today_claim(
        cls, session: AsyncSession, user_id: int, *, now: Optional[datetime] = None
    ) -> Optional[DailyChestClaim]:
        day = utc_day(now)
        return await session.scalar(
            select(DailyChestClaim)
            .where(DailyChestClaim.user_id == user_id)
            .where(DailyChestClaim.claim_day == day)
        )

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------
    @classmethod
    async def claim(
        cls,
        session: AsyncSession,
        user_id: int,
        *,
        rng: Optional[random.Random] = None,
        now: Optional[datetime] = None,
    ) -> ChestClaimResult:
        """Open today's chest. Returns one reward outcome per daily cycle.

        Concurrent presses / duplicate updates / retries are safe: a per-
        user row lock serializes attempts and the ``(user_id, claim_day)``
        unique constraint lets exactly one claim through; lock/unique races
        are rolled back and retried once on a fresh snapshot (the same
        pattern as ``SibClaimService``), and the loser gets the friendly
        already-claimed answer. Everything (claim row + reward delivery) is
        one transaction owned by the caller: a failure anywhere rolls back
        both, so a consumed claim can never outlive its reward.
        """
        now = now or datetime.now(timezone.utc)
        config = await cls.load_config(session)
        if not config.enabled:
            raise ChestDisabledError("صندوقچه گنج روزانه فعال نیست. 🎁")

        for attempt in range(2):
            try:
                return await cls._claim_once(session, user_id, config, rng=rng, now=now)
            except (IntegrityError, OperationalError) as exc:
                logger.warning(
                    f"daily chest race for user {user_id} (attempt {attempt}): {exc}"
                )
                await session.rollback()
        # Two lost races in a row: someone else's claim is committed for
        # today. Answer with the already-claimed screen; never grant here.
        return await cls._already_claimed(session, user_id, now=now)

    @classmethod
    async def _already_claimed(
        cls, session: AsyncSession, user_id: int, *, now: datetime
    ) -> ChestClaimResult:
        claim = await cls.get_today_claim(session, user_id, now=now)
        return ChestClaimResult(
            status="already_claimed",
            next_reset=next_reset_at(now),
            claim=claim,
        )

    @classmethod
    async def _claim_once(
        cls,
        session: AsyncSession,
        user_id: int,
        config: ChestConfig,
        *,
        rng: Optional[random.Random],
        now: datetime,
    ) -> ChestClaimResult:
        day = utc_day(now)
        reset = next_reset_at(now)
        rng = rng or random.Random()

        # Serialize claims per user (the established per-user mutex).
        await session.execute(select(User.id).where(User.id == user_id).with_for_update())

        existing = await cls.get_today_claim(session, user_id, now=now)
        if existing is not None:
            return ChestClaimResult(
                status="already_claimed", next_reset=reset, claim=existing
            )

        # Build the deliverable pool BEFORE the single roll. When the card
        # jackpot cannot be delivered (the player already owns every
        # eligible character), the card weight is excluded and the rest
        # renormalizes - the pool reflects what can actually be granted, so
        # no reroll and no silently-downgraded rare reward can happen.
        deliverable = tuple(KNOWN_REWARDS)
        eligible_characters: List[Character] = []
        if config.weights.get(REWARD_CARD, 0.0) > 0:
            eligible_characters = await cls._eligible_card_characters(session, user_id)
            if not eligible_characters:
                deliverable = tuple(k for k in KNOWN_REWARDS if k != REWARD_CARD)

        reward_type, quantity = roll_outcome(
            config, rng, deliverable=deliverable
        )

        # Progression: the ONE existing Account Level adds a modest Apples
        # bonus to the apples outcome only (never odds, never rarer tiers).
        level_bonus = 0
        player_level: Optional[int] = None
        if reward_type == REWARD_APPLES:
            from sib.services.player_xp import PlayerXPService

            progress = await PlayerXPService.get_progress(session, user_id)
            player_level = progress.level
            level_bonus = progression_apples_bonus(progress.level, config)

        # The durable claim row: inserting it is the award gate (unique on
        # user_id+claim_day). Written first so delivery can reference it;
        # any delivery failure rolls the insert back with the reward.
        claim = DailyChestClaim(
            user_id=user_id,
            claim_day=day,
            reward_type=reward_type,
            quantity=quantity + level_bonus if reward_type == REWARD_APPLES else quantity,
        )
        session.add(claim)
        await session.flush()

        character: Optional[Character] = None
        card: Optional[PlayerCard] = None
        if reward_type == REWARD_APPLES:
            await AppleWalletService.earn(
                session,
                user_id,
                quantity + level_bonus,
                reason=CLAIM_REASON,
                ref_type="daily_chest",
                ref_id=claim.id,
                idempotency_key=f"{CLAIM_REASON}:{user_id}:{day}",
            )
        elif reward_type == REWARD_SEEDLINGS:
            from sib.services.seedling import SeedlingService

            await SeedlingService.award_daily_chest(
                session, user_id, quantity, claim_id=claim.id
            )
        elif reward_type in (REWARD_GOLD, REWARD_DIAMOND, REWARD_OBSIDIAN):
            await ItemService.grant(session, user_id, reward_type, quantity)
        elif reward_type == REWARD_CARD:
            # The rarity moment: pick one unowned eligible character and
            # grant real ownership through the canonical card service.
            if not eligible_characters:  # pragma: no cover - pool already excluded it
                raise DailyChestError("کارت قابل اهدایی پیدا نشد؛ بعداً دوباره تلاش کن. 🎴")
            character = eligible_characters[
                int(rng.randint(0, len(eligible_characters) - 1))
            ]
            from sib.services.player_cards import DuplicateCardError, PlayerCardService

            try:
                card = await PlayerCardService.acquire(session, user_id, character.id)
            except DuplicateCardError as exc:
                # Race backstop (e.g. a concurrent trade/shop purchase won
                # the same character): fail the WHOLE opening so the claim
                # rolls back with it. Never swap the rare reward for a
                # common one while reporting the card as granted.
                raise DailyChestError(
                    "این کارت همزمان جای دیگه‌ای ثبت شد؛ صندوقچه باز نشد و سهمیه‌ات محفوظه. 🎴"
                ) from exc
            claim.ref_id = character.id
            await session.flush()
            # Same one-time collection XP milestone as shop/trade card
            # acquisition (idempotent per user+character).
            from sib.services.player_xp import PlayerXPService

            await PlayerXPService.award_character_collection(
                session, user_id=user_id, character_id=character.id
            )

        log_event(
            Events.DAILY_CHEST_CLAIMED,
            {
                "user_id": user_id,
                "day": day,
                "reward_type": reward_type,
                "quantity": claim.quantity,
                "level_bonus": level_bonus,
            },
        )
        return ChestClaimResult(
            status="claimed",
            next_reset=reset,
            reward_type=reward_type,
            quantity=claim.quantity,
            level_bonus=level_bonus,
            player_level=player_level,
            claim=claim,
            character=character,
            card=card,
        )

    # ------------------------------------------------------------------
    # Card jackpot eligibility (existing collection rules)
    # ------------------------------------------------------------------
    @staticmethod
    async def _eligible_card_characters(
        session: AsyncSession, user_id: int
    ) -> List[Character]:
        """Released, shop-enabled characters the player does not own yet.

        Mirrors the weekly shop's eligibility base (active + RELEASED +
        shop_enabled) plus the game's one-card-per-character rule. The
        weekly rotation's ``shop_pool_character_ids`` is a shop-rotation
        scope and is deliberately not applied here.
        """
        stmt = (
            select(Character)
            .where(Character.is_active == True)  # noqa: E712
            .where(Character.release_status == ReleaseStatus.RELEASED)
            .where(Character.shop_enabled == True)  # noqa: E712
            .order_by(Character.id.asc())
        )
        characters = list((await session.scalars(stmt)).all())
        owned_ids = set(
            (
                await session.scalars(
                    select(PlayerCard.character_id).where(PlayerCard.user_id == user_id)
                )
            ).all()
        )
        return [c for c in characters if c.id not in owned_ids]
