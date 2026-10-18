"""Castle operations. Caller owns commit/rollback; all rewards use the wallet ledger."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.dialects import mysql, sqlite
from sib.database.models import Group, GroupCastle, CastleDonation, CastleIncomeClaim
from sib.services.apple_wallet import AppleWalletService
from sib.services.lock_order import lock_user_row

THRESHOLDS = (
    0,
    5_000,
    20_000,
    50_000,
    110_000,
    210_000,
    360_000,
    580_000,
    880_000,
    1_300_000,
)
DAILY_CAP = 500


class CastleError(ValueError):
    pass


def level_for_total(total: int) -> int:
    return sum(total >= threshold for threshold in THRESHOLDS)


def apply_castle_buff(damage: int, buff: float) -> int:
    return int(damage * (1 + buff))


class CastleService:
    @staticmethod
    async def get_or_create_castle(session, group_id):
        # Never lock Group here: the invasion scheduler locks Group before
        # invasion rows, while joins lock invasion before user economy mutexes.
        # A User -> Group lock would introduce a cross-feature lock cycle.
        group = await session.scalar(select(Group).where(Group.id == group_id))
        if group is None or not group.is_active:
            raise CastleError("گروه فعال نیست.")
        dialect = session.get_bind().dialect.name
        values = dict(group_id=group_id, level=1, total_donated=0)
        if dialect in ("mysql", "mariadb"):
            stmt = mysql.insert(GroupCastle).values(**values)
            stmt = stmt.on_duplicate_key_update(group_id=stmt.inserted.group_id)
        elif dialect == "sqlite":
            stmt = (
                sqlite.insert(GroupCastle)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["group_id"])
            )
        else:
            raise RuntimeError(f"Unsupported castle database: {dialect}")
        await session.execute(stmt)
        castle = await session.scalar(
            select(GroupCastle)
            .where(GroupCastle.group_id == group_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return castle

    @staticmethod
    async def _remaining(session, user_id, now):
        start = now.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Current locking read avoids stale REPEATABLE READ aggregates.
        amounts = (
            await session.scalars(
                select(CastleDonation.amount)
                .where(
                    CastleDonation.user_id == user_id,
                    CastleDonation.donated_at >= start,
                    CastleDonation.donated_at < start + timedelta(days=1),
                )
                .with_for_update()
            )
        ).all()
        return max(0, DAILY_CAP - sum(amounts))

    @classmethod
    async def donate(
        cls, session, user_id, group_id, amount, *, request_key=None, now=None
    ):
        """amount=None means Max. Returns actual contribution; replay is a saved receipt."""
        if amount is not None and (type(amount) is not int or amount <= 0):
            raise CastleError("مبلغ معتبر نیست.")
        now = now or datetime.now(timezone.utc)
        await lock_user_row(session, user_id)
        castle = await cls.get_or_create_castle(session, group_id)
        key = request_key or f"castle:donate:{uuid4().hex}"
        existing = await session.scalar(
            select(CastleDonation)
            .where(CastleDonation.request_key == key)
            .with_for_update()
        )
        if existing:
            if (
                existing.user_id != user_id
                or existing.group_id != group_id
                or (amount is not None and existing.amount != amount)
            ):
                raise CastleError("درخواست تکراری نامعتبر است.")
            return existing.amount
        remaining = await cls._remaining(session, user_id, now)
        balance = await AppleWalletService.get_balance(
            session, user_id, for_update=True
        )
        available = min(remaining, balance, THRESHOLDS[-1] - castle.total_donated)
        actual = available if amount is None else amount
        if actual <= 0 or actual > available:
            raise CastleError("موجودی، سقف روزانهٔ ۵۰۰ سیب یا ظرفیت قلعه کافی نیست.")
        donation = CastleDonation(
            user_id=user_id,
            group_id=group_id,
            amount=actual,
            donated_at=now,
            request_key=key,
        )
        session.add(donation)
        await session.flush()
        result = await AppleWalletService.debit_once(
            session,
            user_id,
            actual,
            reason="castle_donation",
            idempotency_key=key,
            ref_type="castle_donation",
            ref_id=donation.id,
        )
        if not result.applied:
            raise CastleError("این پرداخت قبلاً ثبت شده است.")
        castle.total_donated += actual
        castle.level = level_for_total(castle.total_donated)
        await session.flush()
        return actual

    @staticmethod
    async def get_invasion_buff(session, group_id):
        level = await session.scalar(
            select(GroupCastle.level)
            .join(Group, Group.id == GroupCastle.group_id)
            .where(GroupCastle.group_id == group_id, Group.is_active.is_(True))
        )
        return (level or 0) * 0.02

    @classmethod
    async def claim_daily_income(cls, session, user_id, group_id, *, now=None):
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        await lock_user_row(session, user_id)
        castle = await cls.get_or_create_castle(session, group_id)
        previous = await session.scalar(
            select(CastleIncomeClaim)
            .where(
                CastleIncomeClaim.user_id == user_id, CastleIncomeClaim.claim_day == day
            )
            .with_for_update()
        )
        if previous:
            raise CastleError("درآمد قلعه را امروز دریافت کرده‌اید.")
        amount = castle.level * 2
        claim = CastleIncomeClaim(
            user_id=user_id, group_id=group_id, claim_day=day, amount=amount
        )
        session.add(claim)
        await session.flush()
        await AppleWalletService.earn(
            session,
            user_id,
            amount,
            reason="castle_spring",
            ref_type="castle_income",
            ref_id=claim.id,
            idempotency_key=f"castle:spring:{user_id}:{day}",
        )
        return amount
