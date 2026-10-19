"""Non-blocking REF1 referral reward on first observed group activity.

A referral pays only when the invitee actually shows up in a group - not
for pressing ``/start`` in private. The group branch of ``/start``
(``sib.handlers.start``) covers invitees who begin with ``/start`` in a
group; this middleware covers every other first step (a wake word, a menu
button, a game move) without touching the Invasion v2 group-membership
observer. Both paths call the same exactly-once
``ReferralService.grant_reward_if_due``.

Like the group-membership observer, the check runs in a bounded detached
task and never delays or breaks the update being processed. Users with no
pending referral are remembered in-process, so after the first check the
middleware costs one set lookup per group update.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.services.referral import ReferralReward, ReferralService

THROTTLE_SECONDS = 60
MAX_PENDING_CHECKS = 256
MAX_TRACKED_USERS = 8192


async def check_referral_reward(user_id: int) -> tuple[Optional[ReferralReward], bool]:
    """Grant if due; return ``(reward, resolved)`` after the commit.

    ``resolved`` is True when no reward can ever become due for this user
    (none pending, or the one just paid), so the middleware may stop
    checking. An unregistered user is *not* resolved - they may register
    (with a referral) a moment later.
    """
    async with get_session() as session:
        reward = await ReferralService.grant_reward_if_due(session, user_id)
        pending = await ReferralService.has_pending_reward(session, user_id)
    return reward, pending is False


class ReferralRewardMiddleware(BaseMiddleware):
    """Schedule bounded referral checks for group updates; never await them."""

    def __init__(self) -> None:
        self._recent: dict[int, float] = {}
        self._resolved: dict[int, None] = {}
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def _identity(event: TelegramObject) -> Optional[int]:
        if isinstance(event, Message):
            message = event
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            message = event.message
            user = event.from_user
        else:
            return None
        chat = getattr(message, "chat", None)
        chat_type = getattr(getattr(chat, "type", None), "value", getattr(chat, "type", None))
        if chat_type not in ("group", "supergroup") or user is None or getattr(user, "is_bot", False):
            return None
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int) or isinstance(user_id, bool) or not user_id:
            return None
        return user_id

    async def _check_safely(self, user_id: int, bot: Any) -> None:
        try:
            reward, resolved = await check_referral_reward(user_id)
        except Exception:
            # Transient DB errors are retried on the next observation
            # rather than hidden by the throttle/resolved bookkeeping.
            self._recent.pop(user_id, None)
            logger.debug("referral reward check failed user=%s", user_id, exc_info=True)
            return
        if resolved:
            self._remember_resolved(user_id)
        if reward is not None:
            # The session above has committed; only now tell the inviter.
            await ReferralService.notify_inviter(bot, reward)

    def _remember_resolved(self, user_id: int) -> None:
        self._recent.pop(user_id, None)
        self._resolved[user_id] = None
        if len(self._resolved) > MAX_TRACKED_USERS:
            self._resolved.pop(next(iter(self._resolved)))

    def _schedule(self, event: TelegramObject, bot: Any) -> None:
        user_id = self._identity(event)
        if user_id is None or user_id in self._resolved:
            return
        now = time.monotonic()
        if now - self._recent.get(user_id, float("-inf")) < THROTTLE_SECONDS:
            return
        if len(self._tasks) >= MAX_PENDING_CHECKS:
            logger.debug("referral reward check dropped: backlog full")
            return
        task = asyncio.create_task(self._check_safely(user_id, bot))
        self._recent[user_id] = now
        if len(self._recent) > MAX_TRACKED_USERS:
            self._recent.pop(next(iter(self._recent)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            self._schedule(event, data.get("bot") if isinstance(data, dict) else None)
        except Exception:
            logger.debug("referral reward check could not be queued", exc_info=True)
        return await handler(event, data)
