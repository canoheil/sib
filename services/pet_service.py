"""Pet hunting, naming rules and limits for the pet system (سیستم پت).

The «سیب شکار» hunt is a multi-step encounter:

1. :func:`get_encounter` validates the player (cooldown, pet caps) and rolls
   which wild pet they meet — but not whether the hunt succeeds.
2. The handler runs the *tracking mini-game* (Phase 4): the player answers
   three tracking questions and the hidden correct path (:func:`roll_tracking_path`)
   adjusts the base chance (:func:`tracking_adjusted_chance`).
3. :func:`execute_hunt` resolves the roll with the adjusted chance when the
   player makes the final choice, creating the nameless baby on success.

:func:`name_pet` is the «اسم» step that gives the new baby a custom name.
Like the rest of ``services`` no Telegram call is made here; the caller owns
the transaction and commits.

Hunting cooldown
----------------
``user_pets.last_hunted_at`` (created in migration 076) is the hunt marker: a
player's cooldown is the newest ``last_hunted_at`` across *all* of their pets,
so a failed attempt postpones the next try exactly like a successful one does.
A player who owns no pet yet has no row to stamp, so that single case (the
first-ever hunt) is remembered in :data:`_PENDING_HUNT_ATTEMPTS`. That dict is
process-local and only matters until the player's first pet exists; it assumes
the documented single polling process, like the in-memory states elsewhere in
the project. A successful hunt always leaves a database timestamp behind.
"""

import logging
import math
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.bidi import wrap_ltr
from sib.database.models import PET_TYPES, UserPet
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.lock_order import lock_user_row

logger = logging.getLogger(__name__)

#: system_config key seeded by migration 076.
COOLDOWN_KEY = "pet_hunt_cooldown_hours"
DEFAULT_COOLDOWN_HOURS = 2

#: Housekeeping caps: at most 3 pets in total and at most one baby at a time,
#: so a player has to name and raise a baby before hunting again.
MAX_PETS_TOTAL = 3
MAX_BABY_PETS = 1

#: Chance = min(90, 30 + card_power / 5).
BASE_HUNT_CHANCE = 30
CARD_POWER_DIVISOR = 5
MAX_HUNT_CHANCE = 90

# ---------------------------------------------------------------------------
# Tracking mini-game (Phase 4)
# ---------------------------------------------------------------------------
#: The choices each tracking step offers, in display order. The handler maps
#: them to button labels; the service rolls the hidden correct path from them
#: and never exposes it to the client.
TRACK_STEP_CHOICES: dict[int, tuple[str, ...]] = {
    1: ("left", "right"),
    2: ("cave", "river"),
    3: ("now", "wait"),
}

#: A correct tracking choice adds 10 % to the base chance, a wrong one
#: subtracts 10 % (three steps → at most ±30 %).
TRACK_CORRECT_BONUS = 10
TRACK_WRONG_PENALTY = 10


def roll_tracking_path(*, rng: random.Random | None = None) -> list[str]:
    """Roll the hidden correct path of the tracking mini-game.

    One correct choice per step, e.g. ``["right", "cave", "now"]``. The path
    only ever lives in the server-side FSM: the roll itself uses
    :func:`tracking_adjusted_chance`, never a client-visible number.
    """
    rng = rng or random.Random()
    return [
        rng.choice(TRACK_STEP_CHOICES[step]) for step in sorted(TRACK_STEP_CHOICES)
    ]


def correct_choice(step: int, choice: str, path: list[str]) -> bool:
    """``True`` when ``choice`` is the correct answer of ``step``."""
    try:
        index = int(step) - 1
    except (TypeError, ValueError):
        return False
    return 0 <= index < len(path) and path[index] == choice


def tracking_adjusted_chance(
    base_chance: int, choices: list[str], path: list[str]
) -> int:
    """The hunt chance after the tracking mini-game.

    Every correct choice adds :data:`TRACK_CORRECT_BONUS` percent and every
    wrong one subtracts :data:`TRACK_WRONG_PENALTY`; the result is clamped
    into ``[0, MAX_HUNT_CHANCE]``. Choices beyond the rolled path length are
    treated as wrong, so a truncated state can never inflate the chance.
    """
    try:
        base = max(0, min(MAX_HUNT_CHANCE, int(base_chance)))
    except (TypeError, ValueError):
        base = BASE_HUNT_CHANCE
    adjusted = base
    for index, choice in enumerate(choices):
        if correct_choice(index + 1, choice, path):
            adjusted += TRACK_CORRECT_BONUS
        else:
            adjusted -= TRACK_WRONG_PENALTY
    return max(0, min(MAX_HUNT_CHANCE, adjusted))

#: Persian display name per species (artwork lives in ``assets/pets/``).
PET_TYPE_FA = {"dog": "سگ", "squirrel": "سنجاب", "cat": "گربه", "owl": "جغد"}

#: Feeding (Phase 3). The inventory item and its config keys; the XP and adult
#: threshold keys are seeded by migration 076, while ``pet_feed_cooldown_hours``
#: is default-only (see ``services.config_store.CONFIG_DEFAULTS``).
PET_FOOD_ITEM = "pet_food"
FEED_XP_KEY = "pet_feed_xp_amount"
FEED_XP_DEFAULT = 50
ADULT_XP_KEY = "pet_adult_xp_required"
ADULT_XP_DEFAULT = 500
FEED_COOLDOWN_KEY = "pet_feed_cooldown_hours"
FEED_COOLDOWN_HOURS_DEFAULT = 1
#: Skill upgrades (both keys seeded by migration 076): an adult's skill level
#: is raised by spending Diamond Apples from the inventory.
SKILL_UPGRADE_ITEM = "apple_diamond"
SKILL_UPGRADE_COST_KEY = "pet_skill_upgrade_diamond_cost"
SKILL_UPGRADE_COST_DEFAULT = 10
SKILL_MAX_LEVEL_KEY = "pet_skill_max_level"
SKILL_MAX_LEVEL_DEFAULT = 3
#: Hunger: a pet not fed for more than this many hours is «گشنه» and its
#: skill is temporarily disabled until the next meal resets ``last_fed_at``.
SATIETY_HUNGRY_AFTER_HOURS = 24
SATIETY_FULL = "سیر"
SATIETY_HUNGRY = "گشنه"
#: Name shown for a pet that has not been named yet.
UNNAMED_PET_FA = "بی‌نام"

NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 20
#: ZWNJ is part of ordinary Persian words (e.g. «سیب‌ک»), so it may appear
#: inside a name but never on its own.
_ZWNJ = "\u200c"
_NAME_SEPARATORS = (" ", _ZWNJ)

#: ``user_id -> moment`` for players who have no pet row to stamp yet.
_PENDING_HUNT_ATTEMPTS: dict[int, datetime] = {}


# ---------------------------------------------------------------------------
# Clock and config helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Interpret naive database DATETIMEs as UTC (SQLite drops the tzinfo)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _config_int(session: AsyncSession, key: str, default: int) -> int:
    try:
        return int(await ConfigService.get_int(session, key))
    except (TypeError, ValueError, OverflowError):
        return int(default)


async def cooldown_hours(session: AsyncSession) -> int:
    """Configured hunt cooldown in hours (never negative)."""
    return max(0, await _config_int(session, COOLDOWN_KEY, DEFAULT_COOLDOWN_HOURS))


def hunt_chance(card_power: int) -> int:
    """Success percent for a hunt with ``card_power``, capped at 90 %."""
    try:
        power = max(0, int(card_power))
    except (TypeError, ValueError):
        power = 0
    return int(min(MAX_HUNT_CHANCE, BASE_HUNT_CHANCE + power / CARD_POWER_DIVISOR))


def pet_type_fa(pet_type: str | None) -> str:
    """Persian species name for user-facing text."""
    return PET_TYPE_FA.get(str(pet_type or ""), "پت")


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

async def last_hunt_at(
    session: AsyncSession, user_id: int
) -> datetime | None:
    """Newest hunt attempt by ``user_id`` (pets or the pet-less fallback)."""
    newest = await session.scalar(
        select(func.max(UserPet.last_hunted_at)).where(
            UserPet.user_id == int(user_id)
        )
    )
    candidates = []
    if newest is not None:
        candidates.append(_as_utc(newest))
    pending = _PENDING_HUNT_ATTEMPTS.get(int(user_id))
    if pending is not None:
        candidates.append(_as_utc(pending))
    return max(candidates) if candidates else None


async def cooldown_remaining_seconds(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
) -> int:
    """Seconds left before the next hunt, or ``0`` when the player may hunt."""
    hours = await cooldown_hours(session)
    if hours <= 0:
        return 0
    moment = _as_utc(now) if now is not None else _now()
    last = await last_hunt_at(session, user_id)
    if last is None:
        return 0
    ready_at = last + timedelta(hours=hours)
    remaining = (ready_at - moment).total_seconds()
    if remaining <= 0:
        # Drop an expired pet-less fallback so the map only holds active players.
        _PENDING_HUNT_ATTEMPTS.pop(int(user_id), None)
        return 0
    return int(math.ceil(remaining))


async def check_hunt_cooldown(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    """``True`` while the player is still waiting for their next hunt."""
    return await cooldown_remaining_seconds(session, user_id, now=now) > 0


async def _mark_hunt_attempt(
    session: AsyncSession, user_id: int, moment: datetime, owned_pets: int
) -> None:
    """Stamp the attempt so the cooldown starts even when the hunt failed.

    Pets carry the timestamp; a player with no pet row yet falls back to the
    process-local map (see the module docstring).
    """
    if owned_pets:
        await session.execute(
            update(UserPet)
            .where(UserPet.user_id == int(user_id))
            .values(last_hunted_at=moment)
        )
        _PENDING_HUNT_ATTEMPTS.pop(int(user_id), None)
    else:
        _PENDING_HUNT_ATTEMPTS[int(user_id)] = moment
    await session.flush()


# ---------------------------------------------------------------------------
# Hunting
# ---------------------------------------------------------------------------

async def pet_counts(session: AsyncSession, user_id: int) -> dict[str, int]:
    """``{"total": n, "babies": m}`` owned by ``user_id``."""
    total = await session.scalar(
        select(func.count())
        .select_from(UserPet)
        .where(UserPet.user_id == int(user_id))
    )
    babies = await session.scalar(
        select(func.count())
        .select_from(UserPet)
        .where(
            UserPet.user_id == int(user_id),
            UserPet.is_adult.is_(False),
        )
    )
    return {"total": int(total or 0), "babies": int(babies or 0)}


def _limit_reason(counts: dict[str, int]) -> str | None:
    """Which cap blocks a hunt, or ``None`` when the player may hunt."""
    if counts["total"] >= MAX_PETS_TOTAL:
        return "total"
    if counts["babies"] >= MAX_BABY_PETS:
        return "baby"
    return None


async def get_encounter(
    session: AsyncSession,
    user_id: int,
    card_power: int = 0,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict:
    """Open the interactive hunt: validate the player and pick a wild pet.

    This is step one of the two-step flow used by the inline-keyboard
    encounter. It only *rolls the species* — the success roll happens later,
    in :func:`execute_hunt`, when the player presses the hunt button.

    ``card_power`` is the caller's Main Card power (``0`` gives the base
    chance). Returns ``{"status": "ok", "pet_type": str, "chance": int}`` or
    the same ``cooldown`` / ``limit`` refusals the one-step hunt used to return.
    """
    moment = _as_utc(now) if now is not None else _now()
    rng = rng or random.Random()

    remaining = await cooldown_remaining_seconds(session, user_id, now=moment)
    if remaining > 0:
        return {"status": "cooldown", "remaining_seconds": remaining}

    counts = await pet_counts(session, user_id)
    reason = _limit_reason(counts)
    if reason is not None:
        return {
            "status": "limit",
            "reason": reason,
            "counts": counts,
            "max_total": MAX_PETS_TOTAL,
            "max_babies": MAX_BABY_PETS,
        }

    return {
        "status": "ok",
        "pet_type": rng.choice(list(PET_TYPES)),
        "chance": hunt_chance(card_power),
    }


async def execute_hunt(
    session: AsyncSession,
    user_id: int,
    pet_type: str,
    chance: int,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict:
    """Resolve an encounter opened by :func:`get_encounter`.

    Rolls ``chance`` percent. On success a nameless baby :class:`UserPet` is
    created; on a miss nothing is created. Either way the hunt cooldown starts.

    ``pet_type`` and ``chance`` are re-validated here because they arrive from
    inline-keyboard callback data, which a client can forge: an unknown species
    is rejected and the chance is clamped into ``[0, MAX_HUNT_CHANCE]``.

    The cooldown and the pet caps are re-checked as well, so pressing the
    button twice can never produce two pets or skip the wait.
    """
    moment = _as_utc(now) if now is not None else _now()
    rng = rng or random.Random()

    if pet_type not in PET_TYPES:
        return {"status": "invalid", "pet": None}

    remaining = await cooldown_remaining_seconds(session, user_id, now=moment)
    if remaining > 0:
        return {"status": "cooldown", "remaining_seconds": remaining, "pet": None}

    counts = await pet_counts(session, user_id)
    reason = _limit_reason(counts)
    if reason is not None:
        return {"status": "limit", "reason": reason, "pet": None}

    try:
        percent = max(0, min(MAX_HUNT_CHANCE, int(chance)))
    except (TypeError, ValueError):
        percent = 0

    if rng.random() * 100 >= percent:
        await _mark_hunt_attempt(session, user_id, moment, counts["total"])
        return {"status": "failed", "pet": None, "pet_type": pet_type, "chance": percent}

    pet = UserPet(
        user_id=int(user_id),
        pet_type=pet_type,
        custom_name=None,  # named in the follow-up flow
        is_adult=False,
        xp=0,
        skill_level=1,
        last_hunted_at=moment,
    )
    session.add(pet)
    _PENDING_HUNT_ATTEMPTS.pop(int(user_id), None)
    await session.flush()
    return {
        "status": "success",
        "pet": pet,
        "pet_type": pet_type,
        "chance": percent,
    }


# ---------------------------------------------------------------------------
# Reading pets (menu, profile)
# ---------------------------------------------------------------------------

async def list_pets(session: AsyncSession, user_id: int) -> list[UserPet]:
    """Every pet owned by ``user_id``, oldest first (stable menu order)."""
    rows = await session.scalars(
        select(UserPet).where(UserPet.user_id == int(user_id)).order_by(UserPet.id)
    )
    return list(rows.all())


async def get_pet(
    session: AsyncSession,
    user_id: int,
    pet_id: int | str | None,
    *,
    for_update: bool = False,
) -> UserPet | None:
    """One pet, but only when it belongs to ``user_id`` (else ``None``)."""
    try:
        key = int(pet_id)
    except (TypeError, ValueError):
        return None
    stmt = select(UserPet).where(UserPet.id == key)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    pet = await session.scalar(stmt)
    if pet is None or int(pet.user_id) != int(user_id):
        return None
    return pet


def pet_display_name(pet: UserPet) -> str:
    """The custom name, or a friendly placeholder for an unnamed pet.

    A player can name a pet anything, including a Latin name. The name is
    fenced with ``LRM`` (:func:`sib.core.bidi.wrap_ltr`) so it stays one
    left-to-right block inside the Persian pet captions instead of dragging
    their punctuation to the wrong side.
    """
    return wrap_ltr((pet.custom_name or "").strip() or UNNAMED_PET_FA)


async def feed_xp_amount(session: AsyncSession) -> int:
    """XP granted per feeding (``pet_feed_xp_amount``, default 50)."""
    return max(0, await _config_int(session, FEED_XP_KEY, FEED_XP_DEFAULT))


async def adult_xp_required(session: AsyncSession) -> int:
    """XP needed to evolve (``pet_adult_xp_required``, default 500)."""
    return max(1, await _config_int(session, ADULT_XP_KEY, ADULT_XP_DEFAULT))


async def feed_cooldown_hours(session: AsyncSession) -> int:
    """Hours between feedings; ``0`` disables the wait."""
    return max(0, await _config_int(session, FEED_COOLDOWN_KEY, FEED_COOLDOWN_HOURS_DEFAULT))


def feed_remaining_seconds(
    pet: UserPet, cooldown_hours: int, *, now: datetime | None = None
) -> int:
    """Seconds until ``pet`` may eat again (``0`` when it is hungry)."""
    if cooldown_hours <= 0 or pet.last_fed_at is None:
        return 0
    moment = _as_utc(now) if now is not None else _now()
    ready_at = _as_utc(pet.last_fed_at) + timedelta(hours=cooldown_hours)
    remaining = (ready_at - moment).total_seconds()
    return int(math.ceil(remaining)) if remaining > 0 else 0


def get_satiety_status(
    last_fed_at: datetime | None, *, now: datetime | None = None
) -> str:
    """«سیر» (full) or «گشنه» (hungry) from the last meal's timestamp.

    More than :data:`SATIETY_HUNGRY_AFTER_HOURS` hours since ``last_fed_at``
    means hungry; a pet that has never been fed (``None``) is hungry too.
    While hungry the pet's skill is disabled (see :func:`is_skill_active`).
    """
    if last_fed_at is None:
        return SATIETY_HUNGRY
    moment = _as_utc(now) if now is not None else _now()
    elapsed = moment - _as_utc(last_fed_at)
    if elapsed > timedelta(hours=SATIETY_HUNGRY_AFTER_HOURS):
        return SATIETY_HUNGRY
    return SATIETY_FULL


def is_skill_active(pet: UserPet, *, now: datetime | None = None) -> bool:
    """``True`` when the pet's skill works: an adult that is still «سیر».

    Babies have no usable skill yet, and a hungry adult's skill is off until
    it is fed again — gameplay effects consult this instead of ``is_adult``.
    """
    return bool(pet.is_adult) and (
        get_satiety_status(getattr(pet, "last_fed_at", None), now=now) == SATIETY_FULL
    )


def xp_progress(pet: UserPet, required: int) -> dict:
    """``{"xp", "required", "percent"}`` for the profile screen."""
    xp = max(0, int(pet.xp or 0))
    needed = max(1, int(required))
    return {
        "xp": xp,
        "required": needed,
        "percent": min(100, int(xp * 100 / needed)),
    }


# ---------------------------------------------------------------------------
# Feeding and evolution
# ---------------------------------------------------------------------------

async def feed_pet(
    session: AsyncSession,
    user_id: int,
    pet_id: int | str | None,
    *,
    now: datetime | None = None,
) -> dict:
    """Feed one owned pet with one ``pet_food``.

    Returns a dict whose ``status`` is one of:

    * ``"not_found"`` — no such pet, or it belongs to another player.
    * ``"too_soon"`` — fed less than ``pet_feed_cooldown_hours`` ago;
      ``remaining_seconds`` is set.
    * ``"no_food"`` — the inventory holds no ``pet_food``.
    * ``"fed"`` — XP was added; ``pet`` carries the new totals.
    * ``"evolved"`` — the XP reached ``pet_adult_xp_required`` and the pet is
      now an adult.
    * ``"satiated"`` — an adult was fed: ``last_fed_at`` is reset (the pet is
      «سیر» again and its skill re-enabled) but no XP is granted.

    Locks: the user row first (rank 0, which also serializes the pet row for
    this user), then the inventory row (rank 5) via ``ItemService``.
    """
    try:
        await lock_user_row(session, int(user_id))
    except (ValueError, TypeError):
        return {"status": "not_found", "pet": None}

    pet = await get_pet(session, user_id, pet_id, for_update=True)
    if pet is None:
        return {"status": "not_found", "pet": None}

    moment = _as_utc(now) if now is not None else _now()
    remaining = feed_remaining_seconds(
        pet, await feed_cooldown_hours(session), now=moment
    )
    if remaining > 0:
        return {
            "status": "too_soon",
            "pet": pet,
            "remaining_seconds": remaining,
        }

    owned_food = await ItemService.get_total(
        session, user_id, PET_FOOD_ITEM, for_update=True
    )
    if owned_food < 1:
        return {"status": "no_food", "pet": pet}

    await ItemService.consume(session, user_id, PET_FOOD_ITEM, 1)

    required = await adult_xp_required(session)
    pet.last_fed_at = moment
    if pet.is_adult:
        # A grown pet still eats: the meal only resets the hunger clock, so
        # XP never climbs past the adult threshold (see get_satiety_status).
        amount = 0
        status = "satiated"
    else:
        amount = await feed_xp_amount(session)
        pet.xp = max(0, int(pet.xp or 0)) + amount
        evolved = pet.xp >= required
        if evolved:
            pet.is_adult = True
        status = "evolved" if evolved else "fed"
    await session.flush()

    progress = xp_progress(pet, required)
    return {
        "status": status,
        "pet": pet,
        "xp": progress["xp"],
        "required": progress["required"],
        "percent": progress["percent"],
        "xp_added": amount,
        "remaining_food": max(0, owned_food - 1),
    }


# ---------------------------------------------------------------------------
# Skill upgrades (Diamond Apples)
# ---------------------------------------------------------------------------

async def skill_max_level(session: AsyncSession) -> int:
    """Configured skill cap (``pet_skill_max_level``, default 3)."""
    return max(1, await _config_int(session, SKILL_MAX_LEVEL_KEY, SKILL_MAX_LEVEL_DEFAULT))


async def skill_upgrade_cost(session: AsyncSession) -> int:
    """Diamond Apples per skill level (``pet_skill_upgrade_diamond_cost``)."""
    return max(0, await _config_int(session, SKILL_UPGRADE_COST_KEY, SKILL_UPGRADE_COST_DEFAULT))


async def upgrade_skill(
    session: AsyncSession,
    user_id: int,
    pet_id: int | str | None,
) -> dict:
    """Raise one owned adult pet's skill level by one, for Diamond Apples.

    Returns a dict whose ``status`` is one of:

    * ``"not_found"`` — no such pet, or it belongs to another player.
    * ``"not_adult"`` — a baby's skill is still locked; it must evolve first.
    * ``"max_level"`` — the skill already sits at ``pet_skill_max_level``.
    * ``"no_diamonds"`` — the inventory holds fewer than
      ``pet_skill_upgrade_diamond_cost`` Diamond Apples; ``cost`` and
      ``owned`` are set.
    * ``"upgraded"`` — the cost was consumed and ``skill_level`` raised by one
      (capped); ``pet``, ``cost`` and ``remaining_diamonds`` carry the result.

    Locks: the user row first (rank 0, which also serializes the pet row for
    this user), then the inventory row (rank 5) via ``ItemService`` — the same
    order as :func:`feed_pet`. Nothing is committed here; the caller owns the
    transaction.
    """
    try:
        await lock_user_row(session, int(user_id))
    except (ValueError, TypeError):
        return {"status": "not_found", "pet": None}

    pet = await get_pet(session, user_id, pet_id, for_update=True)
    if pet is None:
        return {"status": "not_found", "pet": None}
    if not pet.is_adult:
        return {"status": "not_adult", "pet": pet}

    cap = await skill_max_level(session)
    level = max(1, int(pet.skill_level or 1))
    if level >= cap:
        return {"status": "max_level", "pet": pet, "max_level": cap}

    cost = await skill_upgrade_cost(session)
    owned = await ItemService.get_total(
        session, user_id, SKILL_UPGRADE_ITEM, for_update=True
    )
    if owned < cost:
        return {"status": "no_diamonds", "pet": pet, "cost": cost, "owned": owned}

    if cost > 0:
        await ItemService.consume(session, user_id, SKILL_UPGRADE_ITEM, cost)
    pet.skill_level = min(cap, level + 1)
    await session.flush()
    return {
        "status": "upgraded",
        "pet": pet,
        "skill_level": pet.skill_level,
        "cost": cost,
        "remaining_diamonds": max(0, owned - cost),
    }


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def validate_pet_name(raw: str | None) -> tuple[bool, str]:
    """Validate a pet name.

    Returns ``(True, cleaned_name)`` or ``(False, reason)`` where ``reason`` is
    one of ``empty``, ``too_short``, ``too_long``, ``invalid_chars`` or
    ``digits_only``. Letters (any script), digits, spaces and ZWNJ are
    accepted, but the name must contain at least one letter — so «۱۲۳» and
    emoji/punctuation are rejected while «سیب‌ک ۲» passes.
    """
    if not isinstance(raw, str):
        return False, "empty"
    name = " ".join(raw.strip().split())
    if not name:
        return False, "empty"

    # Character checks come before the length checks so a single emoji reads as
    # "invalid characters" rather than a confusing "too short".
    letters = 0
    for char in name:
        if char.isalpha():
            letters += 1
        elif char.isdigit() or char in _NAME_SEPARATORS:
            continue
        else:
            return False, "invalid_chars"
    if letters == 0:
        return False, "digits_only"
    if len(name) < NAME_MIN_LENGTH:
        return False, "too_short"
    if len(name) > NAME_MAX_LENGTH:
        return False, "too_long"
    return True, name


async def name_pet(
    session: AsyncSession, user_id: int, pet_id: int, raw: str | None
) -> dict:
    """Store ``raw`` as the custom name of one of the player's pets.

    Returns ``{"status": "named", "pet": pet, "name": cleaned}`` or
    ``{"status": <validation reason | "not_found">}``.
    """
    valid, value = validate_pet_name(raw)
    if not valid:
        return {"status": value}

    try:
        pet = await session.get(UserPet, int(pet_id))
    except (TypeError, ValueError):
        return {"status": "not_found"}
    if pet is None or int(pet.user_id) != int(user_id):
        return {"status": "not_found"}

    pet.custom_name = value
    await session.flush()
    return {"status": "named", "pet": pet, "name": value}


# ---------------------------------------------------------------------------
# Renaming (one paid rename per pet)
# ---------------------------------------------------------------------------
#: A rename costs a flat amount of apples and is allowed once per pet.
RENAME_COST_APPLES = 500
RENAME_LEDGER_REASON = "pet_rename"


async def rename_pet(
    session: AsyncSession,
    user_id: int,
    pet_id: int | str | None,
    new_name: str | None,
) -> dict:
    """Rename one of the player's pets, once, for :data:`RENAME_COST_APPLES`.

    Returns a dict whose ``status`` is one of:

    * ``"not_found"`` — no such pet, or it belongs to another player.
    * a validation reason from :func:`validate_pet_name`
      (``empty``/``too_short``/``too_long``/``invalid_chars``/``digits_only``).
    * ``"already_renamed"`` — the single paid rename was already used.
    * ``"insufficient_funds"`` — the wallet holds less than the cost.
    * ``"renamed"`` — the 500 apples were debited and ``custom_name`` updated;
      ``pet`` and ``name`` carry the result.

    Locks: the user row first (rank 0, which also serializes the pet row), then
    the wallet row (rank 6) through :class:`AppleWalletService`. Nothing is
    committed here — the caller owns the transaction.
    """
    valid, value = validate_pet_name(new_name)
    if not valid:
        return {"status": value}

    try:
        await lock_user_row(session, int(user_id))
    except (ValueError, TypeError):
        return {"status": "not_found", "pet": None}

    pet = await get_pet(session, user_id, pet_id, for_update=True)
    if pet is None:
        return {"status": "not_found", "pet": None}
    if pet.has_renamed:
        return {"status": "already_renamed", "pet": pet}

    balance = await AppleWalletService.get_balance(session, user_id, for_update=True)
    if balance < RENAME_COST_APPLES:
        return {
            "status": "insufficient_funds",
            "pet": pet,
            "cost": RENAME_COST_APPLES,
            "balance": balance,
        }

    try:
        debit = await AppleWalletService.debit_once(
            session,
            int(user_id),
            RENAME_COST_APPLES,
            reason=RENAME_LEDGER_REASON,
            ref_type="user_pet",
            ref_id=int(pet.id),
            idempotency_key=f"{RENAME_LEDGER_REASON}:{int(pet.id)}",
        )
    except InsufficientFundsException:
        return {
            "status": "insufficient_funds",
            "pet": pet,
            "cost": RENAME_COST_APPLES,
        }
    if not debit.applied:
        # The one-and-only keyed debit for this pet was already spent: the
        # rename was used, even if the flag read stale.
        return {"status": "already_renamed", "pet": pet}

    pet.custom_name = value
    pet.has_renamed = True
    await session.flush()
    return {
        "status": "renamed",
        "pet": pet,
        "name": value,
        "cost": RENAME_COST_APPLES,
        "balance": debit.balance_after,
    }
