"""Rules and wallet settlement for the Apple Rocket crash game.

A round's crash point is chosen once, with a configurable house edge, when
its bet is accepted. The wager is debited in that transaction; winning rounds
are credited after the animation through :func:`settle_rocket`.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from io import BytesIO
import math
from pathlib import Path
import secrets
import uuid

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import UserRocketState
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.config_store import ConfigService
from sib.services.lock_order import lock_user_row

MIN_TARGET_MULTIPLIER = Decimal("1.1")
MAX_TARGET_MULTIPLIER = Decimal("100")
DEFAULT_MIN_BET = 50
DEFAULT_MAX_BET = 5000
DEFAULT_COOLDOWN_SECONDS = 30
DEFAULT_HOUSE_EDGE_PERCENT = 5
MAX_CRASH_MULTIPLIER = 100.0
BOOM_IMAGE_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "rocket" / "rocket_boom.jpg"
)

BET_REASON = "rocket_bet"
PAYOUT_REASON = "rocket_payout"


def generate_crash_point(house_edge_percent: float = 5, *, rng=None) -> float:
    """Roll a crash point using the standard ``(1-H)/(1-U)`` formula.

    ``house_edge_percent`` is configured as a percentage (5 means 5%). A
    draw below that edge crashes at 1.0x. Otherwise, the point is capped at
    100x and rounded down to one decimal. Before that cap/rounding,
    ``P(crash >= x) = (1-H)/x`` for ``x > 1``. ``U`` uses the OS RNG by
    default; an injected draw function keeps the formula deterministic in tests.
    """
    try:
        edge_percent = float(house_edge_percent)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("house edge must be a number from 0 to 100") from exc
    if not math.isfinite(edge_percent) or not 0 <= edge_percent <= 100:
        raise ValueError("house edge must be a number from 0 to 100")
    edge = edge_percent / 100.0

    draw = (rng if rng is not None else secrets.SystemRandom().random)()
    if not math.isfinite(draw) or not 0 <= draw < 1:
        raise ValueError("random draw must be in [0, 1)")
    if draw < edge:
        return 1.0

    point = min((1.0 - edge) / (1.0 - draw), MAX_CRASH_MULTIPLIER)
    return math.floor(point * 10.0) / 10.0


def generate_boom_image(crash_point: float) -> bytes:
    """Return the rocket crash artwork with its multiplier overlaid."""
    with Image.open(BOOM_IMAGE_PATH) as source:
        image = source.convert("RGB")

    text = f"{float(crash_point):.1f}x"
    draw = ImageDraw.Draw(image)
    font_size = max(24, int(image.height * 0.12))
    font = None
    for font_path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(font_path, font_size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    stroke_width = max(2, font_size // 18)
    bounds = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    text_width = bounds[2] - bounds[0]
    text_height = bounds[3] - bounds[1]
    position = (
        (image.width - text_width) / 2 - bounds[0],
        (image.height - text_height) / 2 - bounds[1],
    )
    draw.text(
        position,
        text,
        font=font,
        fill="white",
        stroke_width=stroke_width,
        stroke_fill="black",
    )

    output = BytesIO()
    image.save(output, format="JPEG", quality=92)
    return output.getvalue()


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _database_now(session: AsyncSession) -> datetime:
    """Read the database clock, normalizing SQLite/MariaDB values to UTC."""
    value = await session.scalar(select(func.now()))
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            value = None
    if not isinstance(value, datetime):  # pragma: no cover - SQL NOW() is non-null
        value = datetime.now(timezone.utc)
    return _as_utc(value)


async def _config_int(session: AsyncSession, key: str, fallback: int) -> int:
    try:
        return int(await ConfigService.get_int(session, key))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _cooldown_remaining(
    last_played_at: datetime, now: datetime, cooldown_seconds: int
) -> int:
    elapsed = (_as_utc(now) - _as_utc(last_played_at)).total_seconds()
    remaining = math.ceil(cooldown_seconds - elapsed)
    return min(cooldown_seconds, max(0, remaining))


async def play_rocket(
    session: AsyncSession,
    user_id: int,
    target_multiplier: float,
    bet_amount: int,
) -> dict:
    """Validate and start a round, atomically debiting its wager.

    Expected refusals are returned as statuses (``invalid_target``,
    ``invalid_bet``, ``cooldown``, ``insufficient_funds`` or ``not_found``).
    The caller owns the transaction and should commit before contacting
    Telegram. Successful results include a unique ``game_id`` for an
    idempotent payout.
    """
    target = _decimal(target_multiplier)
    if target is None or not MIN_TARGET_MULTIPLIER <= target <= MAX_TARGET_MULTIPLIER:
        return {
            "status": "invalid_target",
            "min_target": float(MIN_TARGET_MULTIPLIER),
            "max_target": float(MAX_TARGET_MULTIPLIER),
        }

    bet = _decimal(bet_amount)
    if bet is None or bet != bet.to_integral_value():
        return {"status": "invalid_bet"}
    bet_value = int(bet)

    try:
        await lock_user_row(session, int(user_id))
    except ValueError:
        return {"status": "not_found"}

    min_bet = await _config_int(session, "rocket_min_bet", DEFAULT_MIN_BET)
    max_bet = await _config_int(session, "rocket_max_bet", DEFAULT_MAX_BET)
    if min_bet < 1 or max_bet < min_bet:
        min_bet, max_bet = DEFAULT_MIN_BET, DEFAULT_MAX_BET
    if not min_bet <= bet_value <= max_bet:
        return {"status": "invalid_bet", "min_bet": min_bet, "max_bet": max_bet}

    cooldown_seconds = max(
        0,
        await _config_int(
            session, "rocket_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS
        ),
    )
    now = await _database_now(session)
    state = await session.scalar(
        select(UserRocketState)
        .where(UserRocketState.user_id == int(user_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is not None and cooldown_seconds:
        remaining = _cooldown_remaining(state.last_played_at, now, cooldown_seconds)
        if remaining > 0:
            return {"status": "cooldown", "remaining_seconds": remaining}

    balance = await AppleWalletService.get_balance(
        session, int(user_id), for_update=True
    )
    if balance < bet_value:
        return {"status": "insufficient_funds", "balance": balance}

    game_id = uuid.uuid4().hex
    try:
        debit = await AppleWalletService.debit_once(
            session,
            int(user_id),
            bet_value,
            reason=BET_REASON,
            ref_type="rocket_round",
            idempotency_key=f"rocket_bet:{game_id}",
        )
    except InsufficientFundsException:
        # Defensive: the user row lock should make the balance check above
        # authoritative, but retain the wallet service's no-negative guarantee.
        return {"status": "insufficient_funds", "balance": balance}
    if not debit.applied:  # UUID collision/replay must never start another round.
        return {"status": "already_started"}

    house_edge = await _config_int(
        session, "rocket_house_edge", DEFAULT_HOUSE_EDGE_PERCENT
    )
    if not 0 <= house_edge <= 100:
        house_edge = DEFAULT_HOUSE_EDGE_PERCENT
    crash_point = generate_crash_point(house_edge)

    if state is None:
        state = UserRocketState(user_id=int(user_id), last_played_at=now)
        session.add(state)
    else:
        state.last_played_at = now
    await session.flush()

    return {
        "status": "started",
        "crash_point": crash_point,
        "bet": bet_value,
        "target": target,
        "game_id": game_id,
    }


async def settle_rocket(
    session: AsyncSession,
    user_id: int,
    *,
    game_id: str,
    bet_amount: int,
    target_multiplier: float,
    crash_point: float,
) -> dict:
    """Settle a completed round and credit a winning payout exactly once.

    Apple wallets hold integer apples, so a fractional ``bet * target`` is
    floored to the nearest whole apple. Losses only return a result; the bet
    was already burned when the round started.
    """
    target = _decimal(target_multiplier)
    crash = _decimal(crash_point)
    bet = _decimal(bet_amount)
    if (
        not game_id
        or target is None
        or crash is None
        or bet is None
        or bet <= 0
        or bet != bet.to_integral_value()
        or not MIN_TARGET_MULTIPLIER <= target <= MAX_TARGET_MULTIPLIER
        or crash < Decimal("1")
    ):
        return {"status": "invalid"}

    won = crash >= target
    if not won:
        return {"status": "loss", "payout": 0, "crash_point": float(crash)}

    try:
        await lock_user_row(session, int(user_id))
    except ValueError:
        return {"status": "not_found"}

    payout = int((bet * target).to_integral_value(rounding=ROUND_FLOOR))
    balance = await AppleWalletService.earn(
        session,
        int(user_id),
        payout,
        reason=PAYOUT_REASON,
        ref_type="rocket_round",
        idempotency_key=f"rocket_payout:{game_id}",
    )
    return {
        "status": "win",
        "payout": payout,
        "balance": balance,
        "crash_point": float(crash),
    }
