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

MIN_BET, MAX_BET, COOLDOWN = 10, 5000, 30
BET_REASON = "casino_bet"


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
    won = dice_value in ({22, 43, 64} if emoji == "🎰" else {4, 5, 6})
    payout = (bet_amount * (3 if emoji == "🎰" else 18) // (1 if emoji == "🎰" else 10)) if won else 0
    return {"is_win": won, "payout": payout, "value": dice_value}


class CasinoService:
    validate_bet = staticmethod(validate_bet)
    calculate_result = staticmethod(calculate_result)
