"""Lock-order policy for economy transactions (single source of truth).

Every transaction that moves apples, items, cards or XP acquires row locks in
this order. A transaction must never wait for a lock ranked EARLIER while it
already holds a later one:

    rank 0  users                 the per-user economy mutex
    rank 1  marriage              relationship domain (see services/marriage.py)
    rank 2  marriage_proposal
    rank 3  flower_gift
    rank 4  player_cards          card rows, ascending id when several
    rank 5  user_items            inventory rows, ascending item_key when several
    rank 6  apple_wallets         the apple balance
    rank 7  player_progressions   XP / level rows (always last)

The ranks of marriage/proposal/gift/user_items agree with
``LOCK_ORDER_RANK`` in ``tests/test_lock_ordering.py``; that test imports this
table and fails if they drift apart.

Rules
-----
* Every economy operation for ONE user (purchase, sale, upgrade, chest opening,
  garden unlock, item buy/sell, daily-chest claim) locks the user row FIRST,
  before any read that decides a rule (:func:`lock_user_row`). Same-user work
  therefore serializes on rank 0, and inversions among ranks 4-7 inside one
  transaction cannot form a cycle with another same-user transaction.
* Multi-user work locks user rows in ascending ``id`` order
  (:func:`lock_user_rows_in_order`).
* Cross-user cycles are only possible where two users' child rows are locked
  by one transaction (for example, a trade touching two users' cards). Those
  paths must take the rows in ascending id order; see the trading notes in
  ``docs/lock-order.md``.

Known, accepted deviations (each is serialized by the user row):

* ``services/daily_chest.py`` pays apples and grants cards after the claim
  insert (wallet before cards). The daily chest is unchanged by request.
* ``services/items.py`` ``grant`` inserts the FIRST inventory row for an item
  after the wallet debit. The row is user-scoped, so the insert can only
  contend with same-user transactions, which the user row already serializes.

Apple battle settlement pays the winner's apples (rank 6) BEFORE the match
progression award (rank 7), so it takes wallet -> progression like every other
path.
"""

from typing import Iterable, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import User

LOCK_RANKS = {
    "users": 0,
    "marriage": 1,
    "marriage_proposal": 2,
    "flower_gift": 3,
    "player_cards": 4,
    "user_items": 5,
    "apple_wallets": 6,
    "player_progressions": 7,
}


async def lock_user_row(session: AsyncSession, user_id: int) -> None:
    """Row-lock the user (rank 0). Call before any other read or write.

    Raises ``ValueError`` when the user does not exist, so an economy
    operation can never run for a phantom account.
    """
    locked = await session.scalar(
        select(User.id).where(User.id == user_id).with_for_update()
    )
    if locked is None:
        raise ValueError(f"unknown user {user_id}")


async def lock_user_rows_in_order(session: AsyncSession, user_ids: Iterable[int]) -> List[int]:
    """Lock several user rows in ascending id order (deadlock-free)."""
    ordered = sorted({int(uid) for uid in user_ids})
    for uid in ordered:
        await lock_user_row(session, uid)
    return ordered
