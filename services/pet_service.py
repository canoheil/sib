"""Pet hunting, naming rules and limits for the pet system (سیستم پت).

The «سیب شکار» hunt is a two-step encounter (Phase 2.5):

1. :func:`get_encounter` validates the player (cooldown, pet caps) and rolls
   which wild pet they meet — but not whether the hunt succeeds.
2. :func:`execute_hunt` resolves the roll when the player presses the hunt
   button, creating the nameless baby on success.

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

from sib.database.models import PET_TYPES, UserPet
from sib.services.config_store import ConfigService

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

#: Persian display name per species (artwork lives in ``assets/pets/``).
PET_TYPE_FA = {"dog": "سگ", "squirrel": "سنجاب", "cat": "گربه", "owl": "جغد"}

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
