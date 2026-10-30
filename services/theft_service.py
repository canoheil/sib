"""GTA-style apple theft and wanted-level rules; backend only.

The caller owns the transaction: mutations flush but never commit, and no
Telegram call is made here. Timestamps are stored in UTC (the project's
convention) while every clock read and duration is computed in Asia/Tehran.
SQLite and MariaDB can return naive DATETIMEs even for timezone-aware columns,
so those are interpreted as UTC on read.

Every function row-locks the involved ``users`` rows first (rank 0, ascending
id) before touching ``user_wanted_states`` or wallets, so the wanted row and
the apple wallet are serialized by the user mutex like every other economy
path. ``user_wanted_states`` therefore needs no rank of its own in
``services/lock_order.py``.

Lifecycle
---------
* Wanted level 0 (a clean thief): the FIRST theft settles instantly, 100 %
  guaranteed, and raises the level to one star.
* Wanted level 1-5: a theft is *pending*; the caller opens the arrest window
  and later calls :func:`resolve_pending_theft`. The chance drops by 10 % per
  star (L1 90 %, L2 80 %, L3 70 %, L4 60 %, L5 50 %).
* Being arrested pays the arrester a bot-funded bounty per star and jails the
  thief for ``theft_jail_hours``; the victim keeps their apples.
"""

import logging
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import UserWantedState
from sib.services.apple_wallet import (
    AppleWalletService,
    InsufficientFundsException,
)
from sib.services.config_store import ConfigService
from sib.services.lock_order import lock_user_rows_in_order

TEHRAN = ZoneInfo("Asia/Tehran")
logger = logging.getLogger(__name__)

MAX_WANTED_LEVEL = 5
# Only a CLEAN thief (0 stars) settles instantly. From one star on, every theft
# is a pending heist whose arrest window must open first.
INSTANT_THEFT_MAX_LEVEL = 0
# Wanted stars reset this long after the last successful theft.
WANTED_EXPIRY_HOURS = 24
# Chance a pending theft succeeds when nobody arrests the thief in time:
# one star destroys 10 % of the odds, L1 90 % ... L5 50 %.
SUCCESS_PERCENT_BY_LEVEL = {1: 90, 2: 80, 3: 70, 4: 60, 5: 50}
# Wallet ledger reasons. Neither is golden-hour eligible.
REASON_STOLEN = "theft_stolen"
REASON_ARREST_REWARD = "theft_arrest_reward"


def _now() -> datetime:
    return datetime.now(TEHRAN)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _roll(percent: int) -> bool:
    return random.random() * 100 < percent


def success_percent(level: int) -> int:
    """Payout chance of a pending theft at ``level`` wanted stars.

    A clean thief (0 stars) never waits for the window and always succeeds, so
    the rate above ``MAX_WANTED_LEVEL`` never drops below 50 %.
    """
    if int(level) <= 0:
        return 100
    return SUCCESS_PERCENT_BY_LEVEL[min(int(level), MAX_WANTED_LEVEL)]


async def _config_int(session: AsyncSession, key: str) -> int:
    try:
        return await ConfigService.get_int(session, key)
    except (TypeError, ValueError, OverflowError):
        return int(ConfigService.default(key))


async def _percent(session: AsyncSession, key: str) -> int:
    return min(100, max(0, await _config_int(session, key)))


def _effective_level(state: UserWantedState | None, now: datetime) -> int:
    """Wanted stars as of ``now``, clearing an expired sentence in place."""
    if state is None:
        return 0
    expires_at = state.wanted_expires_at
    if expires_at is not None and _as_utc(now) > _as_utc(expires_at):
        state.wanted_level = 0
        state.wanted_expires_at = None
        return 0
    return int(state.wanted_level or 0)


def _is_jailed(state: UserWantedState | None, now: datetime) -> bool:
    return bool(
        state is not None
        and state.jailed_until is not None
        and _as_utc(now) < _as_utc(state.jailed_until)
    )


def _in_cooldown(
    state: UserWantedState | None, now: datetime, cooldown_minutes: int
) -> bool:
    if state is None or state.last_stolen_at is None or cooldown_minutes <= 0:
        return False
    ready_at = _as_utc(state.last_stolen_at) + timedelta(minutes=cooldown_minutes)
    return _as_utc(now) < ready_at


async def _state_for_update(
    session: AsyncSession, user_id: int
) -> UserWantedState | None:
    """Lock the user (already held) then the wanted row, creating it lazily."""
    return await session.scalar(
        select(UserWantedState)
        .where(UserWantedState.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _lock_wallets(session: AsyncSession, user_ids) -> dict[int, int]:
    """Row-lock the wallets in ascending user-id order and return balances.

    Locking before the ledger writes keeps the rank-6 acquisitions ordered even
    when the thief has the larger id; a missing wallet reads as 0 and is
    created later under the same user mutex.
    """
    balances: dict[int, int] = {}
    for user_id in sorted({int(uid) for uid in user_ids}):
        balances[user_id] = await AppleWalletService.get_balance(
            session, user_id, for_update=True
        )
    return balances


def _theft_amount(balance: int, percent: int, minimum: int, maximum: int) -> int:
    low, high = sorted((max(0, minimum), max(0, maximum)))
    share = balance * percent // 100
    return max(low, min(high, share))


async def _transfer_apples(
    session: AsyncSession, *, thief_id: int, victim_id: int, amount: int
) -> bool:
    """Move ``amount`` from the victim to the thief; False when short on funds."""
    try:
        await AppleWalletService.spend(
            session,
            victim_id,
            amount,
            REASON_STOLEN,
            ref_type="theft",
            ref_id=thief_id,
        )
    except InsufficientFundsException:
        return False
    await AppleWalletService.earn(
        session,
        thief_id,
        amount,
        REASON_STOLEN,
        ref_type="theft",
        ref_id=victim_id,
    )
    return True


async def get_status(session: AsyncSession, user_id: int) -> dict:
    """Current wanted state; clears an expired wanted level on the way out."""
    now = _now()
    state = await session.get(UserWantedState, user_id)
    level = _effective_level(state, now)
    cooldown_minutes = await _config_int(session, "theft_cooldown_minutes")
    jail_remaining = 0
    if _is_jailed(state, now):
        jail_remaining = int(
            (_as_utc(state.jailed_until) - _as_utc(now)).total_seconds()
        )
    # Persist a reset that _effective_level just applied.
    await session.flush()
    return {
        "wanted_level": level,
        "is_jailed": jail_remaining > 0,
        "jail_remaining_seconds": jail_remaining,
        "is_cooldown": _in_cooldown(state, now, cooldown_minutes),
    }


async def initiate_theft(
    session: AsyncSession, thief_id: int, victim_id: int
) -> dict:
    """Start a theft; level 0-1 pays out now, level 2+ asks for a window."""
    if int(thief_id) == int(victim_id):
        return {"status": "invalid"}

    try:
        await lock_user_rows_in_order(session, (thief_id, victim_id))
    except ValueError:
        return {"status": "not_found"}

    now = _now()
    cooldown_minutes = await _config_int(session, "theft_cooldown_minutes")
    minimum = max(0, await _config_int(session, "theft_min_amount"))
    maximum = max(minimum, await _config_int(session, "theft_max_amount"))
    percent = await _percent(session, "theft_amount_percent")

    state = await _state_for_update(session, thief_id)
    level = _effective_level(state, now)

    if _is_jailed(state, now):
        return {"status": "jailed"}
    if _in_cooldown(state, now, cooldown_minutes):
        return {"status": "cooldown"}

    balances = await _lock_wallets(session, (thief_id, victim_id))
    victim_balance = balances[int(victim_id)]
    if victim_balance < minimum:
        return {"status": "insufficient"}

    amount = _theft_amount(victim_balance, percent, minimum, maximum)

    if level <= INSTANT_THEFT_MAX_LEVEL:
        if not await _transfer_apples(
            session, thief_id=thief_id, victim_id=victim_id, amount=amount
        ):
            return {"status": "insufficient"}
        if state is None:
            state = UserWantedState(user_id=thief_id)
            session.add(state)
        state.wanted_level = min(level + 1, MAX_WANTED_LEVEL)
        state.last_stolen_at = _as_utc(now)
        state.wanted_expires_at = _as_utc(now) + timedelta(hours=WANTED_EXPIRY_HOURS)
        await session.flush()
        logger.info(
            "theft settled instantly: thief=%s victim=%s amount=%s level=%s",
            thief_id, victim_id, amount, state.wanted_level,
        )
        return {"status": "success", "amount": amount, "wanted_level": state.wanted_level}

    # No money moves until resolve_pending_theft decides the outcome.
    window_seconds = await _config_int(session, "theft_arrest_window_seconds")
    return {"status": "pending", "amount": amount, "window_seconds": window_seconds}


async def resolve_pending_theft(
    session: AsyncSession,
    thief_id: int,
    victim_id: int,
    amount: int,
    caught_by_id: int | None,
) -> dict:
    """Settle a pending heist: caught by a player, or a level-based RNG roll."""
    amount = int(amount)
    if amount <= 0:
        return {"status": "failed"}

    locked = [thief_id, victim_id]
    if caught_by_id is not None:
        locked.append(caught_by_id)
    try:
        await lock_user_rows_in_order(session, locked)
    except ValueError:
        return {"status": "not_found"}

    now = _now()
    state = await _state_for_update(session, thief_id)
    level = _effective_level(state, now)
    if state is None:
        state = UserWantedState(user_id=thief_id)
        session.add(state)

    if caught_by_id is not None:
        # The victim keeps their apples; the bot pays the bounty instead.
        per_star = await _config_int(session, "theft_arrest_reward_per_star")
        reward = max(0, per_star) * level
        if reward:
            await _lock_wallets(session, (caught_by_id,))
            await AppleWalletService.earn(
                session,
                caught_by_id,
                reward,
                REASON_ARREST_REWARD,
                ref_type="theft",
                ref_id=thief_id,
            )
        jail_hours = await _config_int(session, "theft_jail_hours")
        state.jailed_until = _as_utc(now) + timedelta(hours=max(0, jail_hours))
        state.wanted_level = 0
        state.wanted_expires_at = None
        await session.flush()
        logger.info(
            "theft arrest: thief=%s arrester=%s level=%s reward=%s",
            thief_id, caught_by_id, level, reward,
        )
        return {"status": "caught", "reward": reward}

    if not _roll(success_percent(level)):
        # Nothing moves: the victim keeps their apples and the level stands.
        return {"status": "failed"}

    balances = await _lock_wallets(session, (thief_id, victim_id))
    if balances[int(victim_id)] < amount:
        # The victim already spent it; a failed heist, never a negative wallet.
        return {"status": "failed"}
    if not await _transfer_apples(
        session, thief_id=thief_id, victim_id=victim_id, amount=amount
    ):
        return {"status": "failed"}

    state.wanted_level = min(level + 1, MAX_WANTED_LEVEL)
    state.last_stolen_at = _as_utc(now)
    state.wanted_expires_at = _as_utc(now) + timedelta(hours=WANTED_EXPIRY_HOURS)
    await session.flush()
    logger.info(
        "theft succeeded: thief=%s victim=%s amount=%s level=%s",
        thief_id, victim_id, amount, state.wanted_level,
    )
    return {"status": "success", "amount": amount, "wanted_level": state.wanted_level}
