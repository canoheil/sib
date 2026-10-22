"""Domain rules for Apple Friend requests and friendships."""

from typing import List, Optional, Tuple

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import AppleFriendship, FriendshipStatus, User
from sib.services.sib_claim import fa_digits


def profile_friend_count_line(count: int) -> str:
    """Persian profile line showing an unrestricted friendship count."""
    return f"🍏 دوستان سیبی: {fa_digits(int(count))} نفر"


class FriendshipError(ValueError):
    """Base error for a friendship operation."""


class SelfFriendshipError(FriendshipError):
    pass


class FriendshipAlreadyExistsError(FriendshipError):
    pass


class FriendshipPendingError(FriendshipError):
    pass


class FriendshipNotFoundError(FriendshipError):
    pass


class NotFriendRequestTargetError(FriendshipError):
    pass


class FriendshipNotPendingError(FriendshipError):
    pass


class FriendshipService:
    """Create, resolve, list, and instantly remove friendships."""

    @staticmethod
    async def _lock_users(session: AsyncSession, a: int, b: int) -> None:
        ids = sorted({int(a), int(b)})
        users = list(
            (
                await session.scalars(
                    select(User)
                    .where(User.id.in_(ids))
                    .order_by(User.id)
                    .with_for_update()
                )
            ).all()
        )
        found = {int(user.id) for user in users}
        if found != set(ids):
            raise FriendshipError("کاربر موردنظر پیدا نشد.")
        if any(user.is_bot for user in users):
            raise FriendshipError("دوستی با این کاربر ممکن نیست.")

    @staticmethod
    def _pair_clause(user_id: int, friend_id: int):
        return or_(
            (AppleFriendship.user_id == int(user_id)) & (AppleFriendship.friend_id == int(friend_id)),
            (AppleFriendship.user_id == int(friend_id)) & (AppleFriendship.friend_id == int(user_id)),
        )

    @classmethod
    async def create_request(
        cls, session: AsyncSession, user_id: int, friend_id: int
    ) -> AppleFriendship:
        user_id, friend_id = int(user_id), int(friend_id)
        if user_id == friend_id:
            raise SelfFriendshipError("نمی‌تونی برای خودت درخواست دوستی بفرستی!")
        await cls._lock_users(session, user_id, friend_id)
        existing = await session.scalar(
            select(AppleFriendship).where(cls._pair_clause(user_id, friend_id)).with_for_update()
        )
        if existing is not None:
            if existing.status == FriendshipStatus.ACCEPTED:
                raise FriendshipAlreadyExistsError("شما از قبل با این کاربر دوست هستید!")
            raise FriendshipPendingError("شما یک درخواست دوستی در انتظار دارید.")
        friendship = AppleFriendship(
            user_id=user_id, friend_id=friend_id, status=FriendshipStatus.PENDING
        )
        session.add(friendship)
        await session.flush()
        return friendship

    @classmethod
    async def accept_request(
        cls, session: AsyncSession, friendship_id: int, actor_id: int
    ) -> AppleFriendship:
        friendship = await session.scalar(
            select(AppleFriendship).where(AppleFriendship.id == int(friendship_id)).with_for_update()
        )
        if friendship is None:
            raise FriendshipNotFoundError("این درخواست دوستی پیدا نشد.")
        if friendship.friend_id != int(actor_id):
            raise NotFriendRequestTargetError("فقط مخاطب درخواست می‌تونه اون رو قبول کنه.")
        if friendship.status != FriendshipStatus.PENDING:
            raise FriendshipNotPendingError("این درخواست دوستی دیگه در انتظار نیست.")
        result = await session.execute(
            update(AppleFriendship)
            .where(
                AppleFriendship.id == int(friendship_id),
                AppleFriendship.status == FriendshipStatus.PENDING,
            )
            .values(status=FriendshipStatus.ACCEPTED)
        )
        if result.rowcount != 1:
            raise FriendshipNotPendingError("این درخواست دوستی دیگه در انتظار نیست.")
        await session.flush()
        return friendship

    @classmethod
    async def reject_request(
        cls, session: AsyncSession, friendship_id: int, actor_id: int
    ) -> AppleFriendship:
        friendship = await session.scalar(
            select(AppleFriendship).where(AppleFriendship.id == int(friendship_id)).with_for_update()
        )
        if friendship is None:
            raise FriendshipNotFoundError("این درخواست دوستی پیدا نشد.")
        if friendship.friend_id != int(actor_id):
            raise NotFriendRequestTargetError("فقط مخاطب درخواست می‌تونه اون رو رد کنه.")
        if friendship.status != FriendshipStatus.PENDING:
            raise FriendshipNotPendingError("این درخواست دوستی دیگه در انتظار نیست.")
        await session.delete(friendship)
        await session.flush()
        return friendship

    @classmethod
    async def unfriend(cls, session: AsyncSession, user_id: int, friend_id: int) -> bool:
        user_id, friend_id = int(user_id), int(friend_id)
        if user_id == friend_id:
            return False
        await cls._lock_users(session, user_id, friend_id)
        friendship = await session.scalar(
            select(AppleFriendship)
            .where(cls._pair_clause(user_id, friend_id))
            .where(AppleFriendship.status == FriendshipStatus.ACCEPTED)
            .with_for_update()
        )
        if friendship is None:
            return False
        await session.delete(friendship)
        await session.flush()
        return True

    @classmethod
    async def are_friends(cls, session: AsyncSession, user_id: int, friend_id: int) -> bool:
        """True only when an ACCEPTED friendship links the two users.

        Direction-agnostic: ``apple_friendships`` stores one row per pair, so
        the pair is matched in BOTH column orders. A PENDING request is NOT a
        friendship and never counts (see ``FriendshipStatus``).

        Deliberately a plain read with no ``FOR UPDATE``: callers use it only
        to SIZE a reward (the Sib Hug / Sib Kiss friendship bonus), never to
        guard an integrity rule. Locking the pair here would also gap-lock the
        pair range on MariaDB, which would block unrelated friend-request
        inserts for the whole duration of a hug transaction.
        """
        user_id, friend_id = int(user_id), int(friend_id)
        if user_id == friend_id:
            return False
        row_id = await session.scalar(
            select(AppleFriendship.id)
            .where(cls._pair_clause(user_id, friend_id))
            .where(AppleFriendship.status == FriendshipStatus.ACCEPTED)
            .limit(1)
        )
        return row_id is not None

    @staticmethod
    async def list_friends(
        session: AsyncSession, user_id: int, *, offset: int = 0, limit: Optional[int] = None
    ) -> Tuple[List[User], int]:
        user_id = int(user_id)
        condition = (
            ((AppleFriendship.user_id == user_id) & (AppleFriendship.status == FriendshipStatus.ACCEPTED))
            | ((AppleFriendship.friend_id == user_id) & (AppleFriendship.status == FriendshipStatus.ACCEPTED))
        )
        # Keep count and page selection as separate explicit queries for all supported dialects.
        count_result = await session.scalar(
            select(func.count(AppleFriendship.id)).where(condition)
        )
        count = int(count_result or 0)
        other_id = case(
            (AppleFriendship.user_id == user_id, AppleFriendship.friend_id),
            else_=AppleFriendship.user_id,
        )
        query = (
            select(User)
            .join(AppleFriendship, User.id == other_id)
            .where(condition)
            .order_by(User.first_name, User.id)
        )
        if offset:
            query = query.offset(max(0, int(offset)))
        if limit is not None:
            query = query.limit(max(0, int(limit)))
        users = list((await session.scalars(query)).all())
        return users, count

    @classmethod
    async def count_friends(cls, session: AsyncSession, user_id: int) -> int:
        _friends, count = await cls.list_friends(session, user_id, limit=0)
        return count
