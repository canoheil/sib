"""Process-local, per-user throttling shared by all message/callback observers.

Spam jail intentionally lives in memory: restart clears it and separate bot
workers do not share it. Run one polling worker (or use a shared cache before
scaling horizontally). No database/API work is performed for dropped events.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


@dataclass
class SpamState:
    requests: deque[float] = field(default_factory=deque)
    strikes: deque[float] = field(default_factory=deque)
    # Monotonic deadline, not a wall-clock/database timestamp.
    banned_until: float = 0.0

    @property
    def strike_count(self) -> int:
        return len(self.strikes)


class ThrottlingMiddleware(BaseMiddleware):
    LIMIT = 3
    WINDOW = 2.0
    STRIKE_WINDOW = 60.0
    BAN_SECONDS = 2 * 60 * 60

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._users: dict[int, SpamState] = {}
        self._last_cleanup = clock()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)) or event.from_user is None:
            return await handler(event, data)

        now = self._clock()
        if now - self._last_cleanup >= self.STRIKE_WINDOW:
            self._cleanup(now)
            self._last_cleanup = now
        state = self._users.setdefault(event.from_user.id, SpamState())
        if now < state.banned_until:
            return None
        if state.banned_until:
            state.requests.clear()
            state.strikes.clear()
            state.banned_until = 0.0

        while state.requests and state.requests[0] <= now - self.WINDOW:
            state.requests.popleft()
        while state.strikes and state.strikes[0] <= now - self.STRIKE_WINDOW:
            state.strikes.popleft()
        # No await between checking and reserving a slot: concurrent updates
        # cannot race within the bot's event loop, including across chats.
        if len(state.requests) >= self.LIMIT:
            state.strikes.append(now)
            if state.strike_count >= 3:
                state.banned_until = now + self.BAN_SECONDS
            return None
        state.requests.append(now)
        return await handler(event, data)

    def _cleanup(self, now: float) -> None:
        stale = [
            user_id for user_id, state in self._users.items()
            if state.banned_until <= now
            and (not state.requests or state.requests[-1] <= now - self.STRIKE_WINDOW)
            and (not state.strikes or state.strikes[-1] <= now - self.STRIKE_WINDOW)
        ]
        for user_id in stale:
            del self._users[user_id]


# Preserve existing imports while exposing the more descriptive name.
RateLimitMiddleware = ThrottlingMiddleware
