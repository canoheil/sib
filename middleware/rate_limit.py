"""Lightweight in-memory rate limiting middleware."""

import asyncio
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from sib.core.config import settings


class RateLimitMiddleware(BaseMiddleware):
    """Protect handlers from excessive repeated updates.

    Limits are kept in memory and are intentionally lightweight.
    This protects the running process from floods; it is not a persistent
    anti-abuse system. State is reset when the bot process restarts.
    """

    def __init__(self) -> None:
        self._requests: dict[tuple[int, int, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._last_cleanup = time.monotonic()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id, chat_id, category = self._get_event_info(event)

        if user_id is None:
            return await handler(event, data)

        limit, window = self._get_limit(category)
        if limit <= 0:
            return await handler(event, data)

        now = time.monotonic()
        key = (user_id, chat_id, category)

        async with self._lock:
            bucket = self._requests[key]
            cutoff = now - window

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                # Do not send a warning here. A warning for every dropped
                # update would itself create outbound spam/API load.
                return None

            bucket.append(now)

            if now - self._last_cleanup >= 60:
                self._cleanup(now)
                self._last_cleanup = now

        return await handler(event, data)

    @staticmethod
    def _get_event_info(
        event: TelegramObject,
    ) -> tuple[int | None, int, str]:
        if isinstance(event, Message):
            if not event.from_user:
                return None, 0, "message"

            text = (event.text or "").strip()

            if text.startswith("/"):
                category = "command"
            elif text == "سیب":
                category = "wake_word"
            else:
                category = "message"

            return event.from_user.id, event.chat.id, category

        if isinstance(event, CallbackQuery):
            if not event.from_user:
                return None, 0, "callback"

            chat_id = (
                event.message.chat.id
                if event.message is not None
                and hasattr(event.message, "chat")
                else 0
            )
            return event.from_user.id, chat_id, "callback"

        return None, 0, "other"

    @staticmethod
    def _get_limit(category: str) -> tuple[int, int]:
        limits = {
            "message": (
                settings.RATE_LIMIT_MESSAGE_COUNT,
                settings.RATE_LIMIT_MESSAGE_WINDOW,
            ),
            "command": (
                settings.RATE_LIMIT_COMMAND_COUNT,
                settings.RATE_LIMIT_COMMAND_WINDOW,
            ),
            "wake_word": (
                settings.RATE_LIMIT_WAKE_WORD_COUNT,
                settings.RATE_LIMIT_WAKE_WORD_WINDOW,
            ),
            "callback": (
                settings.RATE_LIMIT_CALLBACK_COUNT,
                settings.RATE_LIMIT_CALLBACK_WINDOW,
            ),
        }
        return limits.get(category, (0, 0))

    def _cleanup(self, now: float) -> None:
        max_window = max(
            settings.RATE_LIMIT_MESSAGE_WINDOW,
            settings.RATE_LIMIT_COMMAND_WINDOW,
            settings.RATE_LIMIT_WAKE_WORD_WINDOW,
            settings.RATE_LIMIT_CALLBACK_WINDOW,
        )
        cutoff = now - max_window

        stale_keys = [
            key
            for key, bucket in self._requests.items()
            if not bucket or bucket[-1] <= cutoff
        ]

        for key in stale_keys:
            self._requests.pop(key, None)
