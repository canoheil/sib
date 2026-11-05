"""Casino rules and transactional wager reservations.

Cooldowns use the existing wallet ledger, avoiding a separate migration and
remaining independent from Rocket's cooldown. The caller commits the session.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import AppleTransaction
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.lock_order import lock_user_row
from sib.services.rocket_service import _cooldown_remaining, _database_now

MIN_BET, MAX_BET, COOLDOWN = 10, 5000, 10
BET_REASON = "casino_bet"

#: Dice / darts / bowling only win on the single best face.
DICE_WINNING_VALUES = frozenset({6})
#: Slots pay only on a matching three-of-a-kind (Telegram's winning values).
SLOTS_WINNING_VALUES = frozenset({1, 22, 43, 64})
#: Payout multipliers, applied as ``bet * multiplier // 1`` so apples stay whole.
DICE_MULTIPLIER = 5
SLOTS_MULTIPLIER = 15

# ---------------------------------------------------------------------------
# «زنجیره سیبی» (Apple Chain) — CASINO-THRILL idea A
# ---------------------------------------------------------------------------
#: The whole chain is a table of ABSOLUTE multipliers on the original stake,
#: indexed by how many consecutive wins produced the pot. It is deliberately
#: not a ratio applied to the previous pot: 5 * 1.5 * 1.5 sneaks above the
#: first-round expectation, while a fixed table can be checked exactly.
#:
#: Two properties are enforced by ``tests/test_casino.py`` and both matter:
#:
#: 1. **The house is never beaten.** ``p(win) ** n * ladder[n] < p(win) *
#:    ladder[1]`` at every depth, ``p`` being 1/6 for the dice games and 4/64
#:    for slots. Pushing can therefore never be worth more than banking.
#: 2. **Pushing is a real bet, not a punishment.** Each rung keeps most of the
#:    previous rung's return, so a daring player is not ground down to nothing:
#:    the dice ladder holds ~62-93% of base RTP at the top rung. A ladder that
#:    paid only a few percent (an earlier draft did) would make the risk
#:    dishonest, which is worse than no feature at all.
#:
#: The rungs are quoted in the numbers the games actually show: dice win 1 in 6
#: and pay x5, so rung 2 is "x5 divided by a sixth, floored" (x28) and the rest
#: follow the same one-in-six discount, rounded down to a readable figure.
CHAIN_LADDERS = {
    "dice": (5, 28, 150, 800, 4000),
    "slots": (15, 220, 3000),
}
#: Longest chain the table can express. Each family may stop earlier (slots
#: pays 1 in 16, so its safe depth is shorter); ``chain_available`` clamps to
#: the family's own length and refuses a push past it.
CHAIN_MAX_STEPS = max(len(ladder) for ladder in CHAIN_LADDERS.values())
#: Seconds the bank/push prompt stays open. Also the client-side auto-bank.
CHAIN_DECISION_SECONDS = 10
#: A winning round is only offered the chain when its pot can actually grow.
CHAIN_MIN_POT = 50
#: One extra rung for a lucky player must not print a number nobody can pay:
#: the chain pot is capped at this multiple of the maximum wager.
CHAIN_POT_CAP = MAX_BET * 120


def chain_family(emoji: str) -> str:
    """Which ladder a game uses: the three dice games share one."""
    return "slots" if emoji == "🎰" else "dice"


def chain_pot(emoji: str, stake: int, streak: int) -> int:
    """The pot after ``streak`` consecutive wins on ``stake``.

    ``streak`` is 1 for the round that just won (the ladder's first rung), so
    a chain that never continues pays exactly what the plain payout would.
    Out-of-range depths are clamped: the ladder is the law, never the pot.
    Each game family may have a different depth, so the clamp uses its own.
    """
    ladder = CHAIN_LADDERS[chain_family(emoji)]
    step = max(1, min(int(streak), len(ladder)))
    return min(CHAIN_POT_CAP, int(stake) * ladder[step - 1])


def chain_available(emoji: str, stake: int, streak: int) -> bool:
    """True when the pot may be pushed one more rung within the EV-safe table."""
    family = chain_family(emoji)
    if family not in CHAIN_LADDERS:
        return False
    if not 1 <= int(streak) < len(CHAIN_LADDERS[family]):
        return False
    # A pot too small to bother with is banked silently instead of prompted.
    return chain_pot(emoji, stake, streak) >= CHAIN_MIN_POT


async def validate_bet(session: AsyncSession, user_id: int, bet_amount: int) -> dict:
    if isinstance(bet_amount, bool) or not isinstance(bet_amount, int) or not MIN_BET <= bet_amount <= MAX_BET:
        return {"status": "invalid_bet", "error": "مبلغ شرط باید بین 10 تا 5000 سیب باشد."}
    try:
        await lock_user_row(session, user_id)
    except ValueError:
        return {"status": "not_found", "error": "ابتدا با /start ثبت‌نام کن."}
    # A locking read is essential under MySQL's REPEATABLE READ isolation.
    last = await session.scalar(
        select(AppleTransaction).where(
            AppleTransaction.user_id == user_id, AppleTransaction.reason == BET_REASON
        ).order_by(AppleTransaction.id.desc()).limit(1).with_for_update()
    )
    if last:
        remaining = _cooldown_remaining(last.created_at, await _database_now(session), COOLDOWN)
        if remaining:
            return {"status": "cooldown", "error": f"⏳ {remaining} ثانیه دیگر تلاش کن."}
    balance = await AppleWalletService.get_balance(session, user_id, for_update=True)
    if balance < bet_amount:
        return {"status": "insufficient_funds", "error": "موجودی کافی نیست. 🍎"}
    game_id = uuid.uuid4().hex
    try:
        debit = await AppleWalletService.debit_once(
            session, user_id, bet_amount, reason=BET_REASON, ref_type="casino_round",
            idempotency_key=f"casino_bet:{game_id}",
        )
    except InsufficientFundsException:
        return {"status": "insufficient_funds", "error": "موجودی کافی نیست. 🍎"}
    if not debit.applied:
        return {"status": "error", "error": "بازی در دسترس نیست؛ دوباره تلاش کن."}
    return {"status": "started", "game_id": game_id}


def calculate_result(emoji: str, dice_value: int, bet_amount: int) -> dict:
    if emoji not in {"🎲", "🎯", "🎳", "🎰"}:
        raise ValueError("unknown casino game")
    if not isinstance(dice_value, int) or not 1 <= dice_value <= (64 if emoji == "🎰" else 6):
        raise ValueError("invalid dice value")
    if emoji == "🎰":
        won = dice_value in SLOTS_WINNING_VALUES
        multiplier = SLOTS_MULTIPLIER
    else:
        won = dice_value in DICE_WINNING_VALUES
        multiplier = DICE_MULTIPLIER
    # Integer maths only: a whole-apple bet can never pay a fractional apple.
    payout = bet_amount * multiplier if won else 0
    return {"is_win": won, "payout": payout, "value": dice_value}


class CasinoService:
    validate_bet = staticmethod(validate_bet)
    calculate_result = staticmethod(calculate_result)
    chain_family = staticmethod(chain_family)
    chain_pot = staticmethod(chain_pot)
    chain_available = staticmethod(chain_available)
