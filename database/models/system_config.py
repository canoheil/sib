"""System configuration model (data-driven game/economy settings)."""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sib.database.base import Base, TimestampMixin


class SystemConfig(Base, TimestampMixin):
    """A single configuration entry (JSON-encoded value)."""

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True, autoincrement=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<SystemConfig {self.key}={self.value}>"
