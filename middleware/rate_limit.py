"""Process-local, per-user throttling shared by all message/callback observers.

Spam jail intentionally lives in memory: restart clears it and separate bot
workers do not share it. Run one polling worker (or use a shared cache before
scaling horizontally). No database/API work is performed for dropped events.

The limits are deliberately forgiving for fast button-clicking games
(Casino, Rocket, pet menus): 6 events per 2 s, and the jail only starts
after 5 strikes within 60 s, lasting 10 minutes. Dropped events are not
silent any more: the user gets a throttled warning on a strike and a clear
jail notice when banned (a callback alert for buttons, a chat message for
plain messages — never a chat message on a CallbackQuery).
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

#: Quick warning when the rate limit is hit (at most once per ALERT_WINDOW).
WARNING_TEXT = "⚠️ لطفاً کمی آهسته‌تر!"
#: Jail notice; ``{minutes}`` is rendered in Persian digits.
JAIL_TEXT_TEMPLATE = (
    "⛔ شما به دلیل اسپم برای {minutes} دقیقه از بازی محروم شدید. "
    "لطفاً بعداً تلاش کنید."
)
#: Latin -> Persian digits, so the middleware stays dependency-free.
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


@dataclass
class SpamState:
    requests: deque[float] = field(default_factory=deque)
    strikes: deque[float] = field(default_factory=deque)
    # Monotonic deadline, not a wall-clock/database timestamp.
    banned_until: float = 0.0
    # Last time the user was warned/alerted (monotonic).
    last_alert: float = 0.0

    @property
    def strike_count(self) -> int:
        return len(self.strikes)


class ThrottlingMiddleware(BaseMiddleware):
    #: Events allowed per WINDOW before a strike (fast clicking in games).
    LIMIT = 6
    WINDOW = 2.0
    #: Strikes within STRIKE_WINDOW that send the user to jail.
    STRIKE_LIMIT = 5
    STRIKE_WINDOW = 60.0
    #: Jail length: 10 minutes.
    BAN_SECONDS = 10 * 60
    #: UX alerts are throttled to at most one per ALERT_WINDOW.
    ALERT_WINDOW = 60.0

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._users: dict[int, SpamState] = {}
        self._last_cleanup = clock()

    def jail_text(self) -> str:
        minutes = int(self.BAN_SECONDS // 60)
        return JAIL_TEXT_TEMPLATE.format(minutes=str(minutes).translate(_FA_DIGITS))

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
            # Jailed: drop the event, but tell the user why (throttled alert,
            # so a jailed clicker is not left with a spinning button).
            await self._notify(event, self.jail_text(), state, now)
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
            if state.strike_count >= self.STRIKE_LIMIT:
                state.banned_until = now + self.BAN_SECONDS
                # Entering the jail always notifies, once per jail.
                await self._notify(event, self.jail_text(), state, now, force=True)
            else:
                await self._notify(event, WARNING_TEXT, state, now)
            return None
        state.requests.append(now)
        return await handler(event, data)

    async def _notify(
        self,
        event: TelegramObject,
        text: str,
        state: SpamState,
        now: float,
        *,
        force: bool = False,
    ) -> None:
        """Best-effort UX feedback for a dropped event.

        A CallbackQuery is answered with an alert (never a chat message, so
        the inline UI is not disturbed); a Message gets a chat message. At
        most one alert per ALERT_WINDOW, unless ``force`` (jail entry).
        """
        if not force and now - state.last_alert < self.ALERT_WINDOW:
            return
        state.last_alert = now
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)
        except Exception:
            pass  # feedback must never break the update pipeline

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
