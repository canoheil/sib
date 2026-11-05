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
