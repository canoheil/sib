"""Apple wallet + transaction ledger service (Phases 7/8).

Every balance-changing operation:
- is transactional (row-locked wallet),
- writes exactly one auditable ledger entry,
- can be made idempotent via a unique idempotency key,
- can never drive the balance below zero.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger, log_event, Events
from sib.database.models import AppleTransaction, AppleWallet


class InsufficientFundsException(Exception):
    """Raised when a spend would drive the wallet negative."""

    def __init__(self, message: str = "سیب‌های شما کافی نیست 🍎"):
        self.message = message
        super().__init__(message)


class AppleWalletService:
    """Wallet operations with full ledger auditability."""

    @staticmethod
    async def get_balance(
        session: AsyncSession, user_id: int, for_update: bool = False
    ) -> int:
        """Current balance. ``for_update`` row-locks an existing wallet.

        Used by multi-resource checks (card upgrades) that must read the
        balance and spend it inside one protected transaction. A missing
        wallet is NOT created: it simply reads as 0.
        """
        stmt = select(AppleWallet).where(AppleWallet.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        wallet = await session.scalar(stmt)
        return wallet.balance if wallet else 0

    @staticmethod
    async def _get_or_create(
        session: AsyncSession, user_id: int
    ) -> AppleWallet:
        wallet = await session.scalar(
            select(AppleWallet)
            .where(AppleWallet.user_id == user_id)
            .with_for_update()
        )
        if not wallet:
            wallet = AppleWallet(user_id=user_id, balance=0)
            session.add(wallet)
            await session.flush()
        return wallet

    @classmethod
    async def _ledger_already_applied(
        cls, session: AsyncSession, idempotency_key: Optional[str]
    ) -> bool:
        if not idempotency_key:
            return False
        existing = await session.scalar(
            select(AppleTransaction.id).where(
                AppleTransaction.idempotency_key == idempotency_key
            )
        )
        return existing is not None

    @classmethod
    async def earn(
        cls,
        session: AsyncSession,
        user_id: int,
        amount: int,
        reason: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> int:
        """Credit apples. Returns the new balance. Idempotent when keyed."""
        if amount < 0:
            raise ValueError("earn amount must be positive")
        if await cls._ledger_already_applied(session, idempotency_key):
            return await cls.get_balance(session, user_id)

        async def credit_and_flush() -> AppleWallet:
            wallet = await cls._get_or_create(session, user_id)
            wallet.balance += amount
            session.add(
                AppleTransaction(
                    user_id=user_id,
                    amount=amount,
                    reason=reason,
                    ref_type=ref_type,
                    ref_id=ref_id,
                    idempotency_key=idempotency_key,
                    balance_after=wallet.balance,
                )
            )
            await session.flush()
            return wallet

        if idempotency_key and session.get_bind().dialect.name != "sqlite":
            # Keep a concurrent unique-key collision inside a savepoint. A
            # wallet reward service must not roll back unrelated work owned by
            # its caller; the unique Apple ledger key still decides the winner.
            # SQLite's legacy transaction mode can release a savepoint as the
            # outermost transaction, so use its ordinary caller-owned
            # transaction instead (SQLite row-lock races are not supported).
            try:
                async with session.begin_nested():
                    wallet = await credit_and_flush()
            except IntegrityError:
                if not await cls._ledger_already_applied(session, idempotency_key):
                    raise
                logger.warning(f"Duplicate apple transaction key={idempotency_key}")
                wallet = await session.scalar(
                    select(AppleWallet)
                    .where(AppleWallet.user_id == user_id)
                    .execution_options(populate_existing=True)
                )
                return wallet.balance if wallet is not None else 0
        elif idempotency_key:
            try:
                wallet = await credit_and_flush()
            except IntegrityError:
                # Never roll back the caller's transaction to recover a
                # SQLite/unsupported-dialect keyed earn conflict.
                raise
        else:
            # Preserve the existing unkeyed operation path; idempotent reward
            # callers use the savepoint/caller-owned path above.
            wallet = await cls._get_or_create(session, user_id)
            wallet.balance += amount
            session.add(
                AppleTransaction(
                    user_id=user_id,
                    amount=amount,
                    reason=reason,
                    ref_type=ref_type,
                    ref_id=ref_id,
                    idempotency_key=None,
                    balance_after=wallet.balance,
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                logger.warning(f"Duplicate apple transaction key={idempotency_key}")
                return await cls.get_balance(session, user_id)

        log_event(
            Events.APPLE_TRANSACTION,
            {"user_id": user_id, "amount": amount, "reason": reason, "balance": wallet.balance},
        )
        return wallet.balance

    @classmethod
    async def spend(
        cls,
        session: AsyncSession,
        user_id: int,
        amount: int,
        reason: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> int:
        """Debit apples atomically. Raises when funds are insufficient."""
        if amount < 0:
            raise ValueError("spend amount must be positive")
        if await cls._ledger_already_applied(session, idempotency_key):
            return await cls.get_balance(session, user_id)

        wallet = await cls._get_or_create(session, user_id)
        if wallet.balance < amount:
            raise InsufficientFundsException()

        wallet.balance -= amount
        session.add(
            AppleTransaction(
                user_id=user_id,
                amount=-amount,
                reason=reason,
                ref_type=ref_type,
                ref_id=ref_id,
                idempotency_key=idempotency_key,
                balance_after=wallet.balance,
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            logger.warning(f"Duplicate apple transaction key={idempotency_key}")
            return await cls.get_balance(session, user_id)

        log_event(
            Events.APPLE_TRANSACTION,
            {"user_id": user_id, "amount": -amount, "reason": reason, "balance": wallet.balance},
        )
        return wallet.balance

    @classmethod
    async def get_history(
        cls,
        session: AsyncSession,
        user_id: int,
        limit: int = 20,
    ):
        stmt = (
            select(AppleTransaction)
            .where(AppleTransaction.user_id == user_id)
            .order_by(AppleTransaction.id.desc())
            .limit(limit)
        )
        return list((await session.scalars(stmt)).all())
