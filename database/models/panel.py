"""Panel ownership model.

Tracks which user owns an interactive Telegram panel message so that
other users cannot mutate or close somebody else's panel. Public actions
(join buttons, matchmaking acceptance, active game moves) are intentionally
NOT routed through this table.
"""

from typing import Optional
from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column
from sib.database.base import Base, TimestampMixin


class PanelOwner(Base, TimestampMixin):
    """Owner of an interactive panel message."""

    __tablename__ = "panel_owners"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    panel_type: Mapped[str] = mapped_column(String(50), default="generic", nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PanelOwner chat={self.chat_id} msg={self.message_id} "
            f"owner={self.owner_user_id} type={self.panel_type}>"
        )
