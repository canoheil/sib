"""Per-player state for the come-back reminders (migration 063).

Nothing in the game brought a player back: no streak nudge, no "your garden is
ready", no "your chest is still closed". ``services/reminders.py`` fixes that,
and this row is what keeps it from becoming spam:

``last_sent_at``
    When the last reminder of any kind was delivered. The gap between two
    reminders for one player is a config value (20 hours by default), so a
    player can never receive more than one reminder per day.

``last_kind``
    ``chest`` / ``garden`` — for auditing and for "did we already nudge about
    this today" checks.

``blocked_at``
    Set the first time Telegram answers ``Forbidden`` (the player blocked the
    bot). The player is never messaged again, and this is what makes the sweep
    cheap: blocked users drop out of the candidate query for good.

``opted_out``
    The player turned reminders off from the profile («🔔 یادآوری‌ها»). The
    toggle is theirs; no admin action and no message is needed to honour it.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, false
from sqlalchemy.orm import Mapped, mapped_column

from sib.database.base import Base


class UserReminderState(Base):
    """One row per player that has ever been reminded (or opted out)."""

    __tablename__ = "user_reminder_states"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    blocked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opted_out: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<UserReminderState user={self.user_id} kind={self.last_kind} "
            f"opted_out={self.opted_out}>"
        )
