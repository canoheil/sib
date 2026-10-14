"""Non-blocking, throttled observation of group users for Invasion v2.

DB errors never affect an incoming message/callback. Only (user, group) pairs
with a registered User and Group can be persisted due to foreign keys.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import func
from sqlalchemy.dialects import mysql, sqlite

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Inv2GroupMember

THROTTLE_SECONDS = 60
MAX_PENDING_WRITES = 256


async def record_group_member(user_id: int, group_id: int, seen_at: datetime) -> None:
    """Insert or advance last_seen_at atomically; never rewind an older visit."""
    async with get_session() as session:
        dialect = session.get_bind().dialect.name
        values = dict(user_id=user_id, group_id=group_id, last_seen_at=seen_at)
        if dialect in ("mysql", "mariadb"):
            stmt = mysql.insert(Inv2GroupMember).values(**values)
            stmt = stmt.on_duplicate_key_update(
                last_seen_at=func.greatest(Inv2GroupMember.last_seen_at, stmt.inserted.last_seen_at)
            )
        elif dialect == "sqlite":  # isolated tests
            stmt = sqlite.insert(Inv2GroupMember).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id", "group_id"],
                set_={"last_seen_at": func.max(
                    Inv2GroupMember.last_seen_at, stmt.excluded.last_seen_at
                )},
            )
        else:
            raise RuntimeError(f"Unsupported group membership dialect: {dialect}")
        await session.execute(stmt)


class GroupMembershipMiddleware(BaseMiddleware):
    """Schedule bounded, once-per-minute writes; never await them in updates."""

    def __init__(self) -> None:
        self._recent: dict[tuple[int, int], float] = {}
        self._tasks: set[asyncio.Task] = set()

    @staticmethod
    def _identity(event: TelegramObject) -> tuple[int, int] | None:
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
        group_id = getattr(chat, "id", None)
        user_id = getattr(user, "id", None)
        if (not isinstance(group_id, int) or isinstance(group_id, bool)
                or not isinstance(user_id, int) or isinstance(user_id, bool)
                or not user_id or not group_id):
            return None
        return user_id, group_id

    async def _write_safely(self, user_id: int, group_id: int, seen_at: datetime) -> None:
        try:
            await record_group_member(user_id, group_id, seen_at)
        except Exception:
            # Missing FK rows / transient DB errors should be retried on the
            # next observation rather than hidden by the one-minute throttle.
            self._recent.pop((user_id, group_id), None)
            logger.debug("inv2 member observation failed user=%s group=%s", user_id, group_id, exc_info=True)

    def _schedule(self, event: TelegramObject) -> None:
        pair = self._identity(event)
        if pair is None:
            return
        now = time.monotonic()
        if now - self._recent.get(pair, float("-inf")) < THROTTLE_SECONDS:
            return
        if len(self._tasks) >= MAX_PENDING_WRITES:
            logger.debug("inv2 member observation dropped: backlog full")
            return
        task = asyncio.create_task(self._write_safely(*pair, datetime.now(timezone.utc)))
        self._recent[pair] = now
        if len(self._recent) > 4096:
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
            self._schedule(event)
        except Exception:
            logger.debug("inv2 member observation could not be queued", exc_info=True)
        return await handler(event, data)
