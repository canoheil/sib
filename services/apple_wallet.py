"""Apple wallet + transaction ledger service (Phases 7/8).

Every balance-changing operation:
- is transactional (row-locked wallet),
- writes exactly one auditable ledger entry,
- can be made idempotent via a unique idempotency key,
- can never drive the balance below zero.
"""

from dataclasses import dataclass
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


class LedgerKeyError(Exception):
    """Base class for idempotency-key refusals on the apple ledger."""


class LedgerKeyConflictError(LedgerKeyError):
    """An idempotency key was reused with different operation parameters.

    Never silently accepted: a conflicting reuse is a caller bug or a forged
    request, and must not look like a successful replay.
    """


class LedgerKeyReplayError(LedgerKeyError):
    """A keyed debit was already applied; the new request did NOT debit.

    A consumed key never authorizes a new purchase. Callers that want an
    idempotent success use :meth:`AppleWalletService.debit_once` and read
    ``LedgerDebit.applied`` instead.
    """


@dataclass(frozen=True)
class LedgerDebit:
    """Outcome of :meth:`AppleWalletService.debit_once`.

    ``applied`` is True only when this call wrote the debit. On a replay it
    is False and ``balance_after`` is the balance recorded right after the
    original operation (not a fresh read, so it cannot be stale).
    """

    applied: bool
    balance_after: int


def savepoints_supported(session: AsyncSession) -> bool:
    # SQLite's legacy transaction mode can release a savepoint issued before
    # any DML as the outermost transaction (RELEASE then commits the caller's
    # work), so savepoints are used on MariaDB/MySQL only. SQLite is a test
    # engine here and has no concurrent writers to race.
    return session.get_bind().dialect.name != "sqlite"


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
            .execution_options(populate_existing=True)
        )
        if not wallet:
            wallet = AppleWallet(user_id=user_id, balance=0)
            session.add(wallet)
            await session.flush()
        return wallet

    @staticmethod
    async def _find_ledger(
        session: AsyncSession, idempotency_key: str, *, lock: bool = False
    ) -> Optional[AppleTransaction]:
        """The ledger row that owns ``idempotency_key``, if any.

        ``lock=True`` is a locking read: it returns the latest COMMITTED row
        even under REPEATABLE READ, which is what a duplicate-key race needs.
        """
        stmt = select(AppleTransaction).where(
            AppleTransaction.idempotency_key == idempotency_key
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return await session.scalar(stmt)

    @staticmethod
    def _assert_same_operation(
        existing: AppleTransaction,
        *,
        user_id: int,
        signed_amount: int,
        reason: str,
        ref_type: Optional[str],
        ref_id: Optional[int],
    ) -> None:
        """Refuse a reused key whose parameters differ from the original.

        ``signed_amount`` is +amount for a credit and -amount for a debit, so
        a credit key reused as a debit (or the reverse) is also a conflict.
        """
        original = (
            int(existing.user_id),
            int(existing.amount),
            existing.reason,
            existing.ref_type,
            existing.ref_id,
        )
        requested = (int(user_id), int(signed_amount), reason, ref_type, ref_id)
        if original != requested:
            raise LedgerKeyConflictError(
                f"idempotency key {existing.idempotency_key!r} was already used "
                "for a different operation"
            )

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
        """Credit apples. Returns the new balance.

        Keyed calls are idempotent with the same strictness as
        :meth:`debit_once`: an identical operation (same user, signed amount,
        reason and reference) replays without writing, while a reused key with
        any different parameter raises :class:`LedgerKeyConflictError`. A key
        owned by another user is a conflict, never a silent no-op.

        The ledger lookup is by key only, because ``uq_apple_tx_idempotency``
        is global; the owner is then compared explicitly.
        """
        if amount < 0:
            raise ValueError("earn amount must be positive")
        params = dict(
            user_id=user_id,
            signed_amount=amount,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        if idempotency_key:
            existing = await cls._find_ledger(session, idempotency_key)
            if existing is not None:
                cls._assert_same_operation(existing, **params)
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

        if idempotency_key and savepoints_supported(session):
            # Keep a concurrent unique-key collision inside a savepoint: only
            # this credit is rolled back, and the winner's row decides. The
            # caller's transaction stays intact.
            try:
                async with session.begin_nested():
                    wallet = await credit_and_flush()
            except IntegrityError:
                winner = await cls._find_ledger(session, idempotency_key, lock=True)
                if winner is None:
                    raise  # not a duplicate-key race; let the caller decide
                cls._assert_same_operation(winner, **params)
                logger.warning(f"Duplicate apple transaction key={idempotency_key}")
                return await cls.get_balance(session, user_id)
        elif idempotency_key:
            # SQLite (test engine only): no savepoint, so the caller's
            # transaction is left to the caller on any failure.
            wallet = await credit_and_flush()
        else:
            # Unkeyed: unchanged path. Unkeyed rows cannot collide on the key;
            # any other integrity error propagates and the caller decides.
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
            await session.flush()

        log_event(
            Events.APPLE_TRANSACTION,
            {"user_id": user_id, "amount": amount, "reason": reason, "balance": wallet.balance},
        )
        return wallet.balance

    @classmethod
    async def debit_once(
        cls,
        session: AsyncSession,
        user_id: int,
        amount: int,
        *,
        reason: str,
        idempotency_key: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[int] = None,
    ) -> LedgerDebit:
        """Debit ``amount`` at most once per ``idempotency_key``.

        Returns :class:`LedgerDebit`. The caller must check ``applied``:
        a replay of an existing key returns ``applied=False`` and writes
        nothing, so a replayed request can never grant a new reward or a new
        purchase on its own. A reused key whose user, amount, reason or
        reference differs raises :class:`LedgerKeyConflictError`.

        Raises :class:`InsufficientFundsException` when the balance is short.
        The caller owns the transaction: nothing is committed here.
        """
        if amount <= 0:
            raise ValueError("debit amount must be positive")
        if not idempotency_key:
            raise ValueError("debit_once requires an idempotency key")

        params = dict(
            user_id=user_id,
            signed_amount=-amount,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
        )
        existing = await cls._find_ledger(session, idempotency_key)
        if existing is not None:
            cls._assert_same_operation(existing, **params)
            return LedgerDebit(applied=False, balance_after=existing.balance_after)

        wallet = await cls._get_or_create(session, user_id)
        if wallet.balance < amount:
            raise InsufficientFundsException()

        new_balance = wallet.balance - amount

        def stage() -> None:
            wallet.balance = new_balance
            session.add(
                AppleTransaction(
                    user_id=user_id,
                    amount=-amount,
                    reason=reason,
                    ref_type=ref_type,
                    ref_id=ref_id,
                    idempotency_key=idempotency_key,
                    balance_after=new_balance,
                )
            )

        try:
            if savepoints_supported(session):
                # A concurrent winner with the same key makes the unique
                # ledger insert fail. Only this savepoint is rolled back; the
                # caller's transaction stays intact and we read the winner.
                async with session.begin_nested():
                    stage()
                    await session.flush()
            else:
                stage()
                await session.flush()
        except IntegrityError:
            if not savepoints_supported(session):
                raise  # no savepoint: the caller's transaction is now unusable
            existing = await cls._find_ledger(session, idempotency_key, lock=True)
            if existing is None:
                raise  # not a duplicate-key race; let the caller decide
            cls._assert_same_operation(existing, **params)
            logger.warning(f"Duplicate apple debit key={idempotency_key}")
            return LedgerDebit(applied=False, balance_after=existing.balance_after)

        log_event(
            Events.APPLE_TRANSACTION,
            {"user_id": user_id, "amount": -amount, "reason": reason, "balance": new_balance},
        )
        return LedgerDebit(applied=True, balance_after=new_balance)

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
        """Debit apples atomically. Raises when funds are insufficient.

        Keyed spends are strict: a replay of an already-applied key raises
        :class:`LedgerKeyReplayError` instead of pretending the debit ran.
        Use :meth:`debit_once` when a caller wants idempotent success.
        """
        if amount < 0:
            raise ValueError("spend amount must be positive")
        if idempotency_key:
            debit = await cls.debit_once(
                session,
                user_id,
                amount,
                reason=reason,
                idempotency_key=idempotency_key,
                ref_type=ref_type,
                ref_id=ref_id,
            )
            if not debit.applied:
                raise LedgerKeyReplayError(
                    f"idempotency key {idempotency_key!r} was already applied"
                )
            return debit.balance_after

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
                idempotency_key=None,
                balance_after=wallet.balance,
            )
        )
        # Unkeyed: never rolls the caller's transaction back (see earn).
        await session.flush()

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
