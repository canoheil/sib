"""Character management service (Phase 9).

Fully data-driven CRUD: introducing, editing, releasing, retiring and
rebalancing characters NEVER requires a Python code change. Every
balance-relevant edit writes an immutable snapshot into
character_balance_versions so historical battle results stay
interpretable.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger, log_event, Events
from sib.database.models import (
    Character,
    CharacterBalanceVersion,
    Rarity,
    ReleaseStatus,
)
from sib.services.config_store import ConfigService


class CharacterValidationError(ValueError):
    """Raised when character data violates the balance model."""


class CharacterService:
    """CRUD + balance versioning for characters."""

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @classmethod
    async def validate_base_budget(cls, session: AsyncSession, power: int, speed: int, intelligence: int) -> None:
        budgets = await ConfigService.get(session, "card_level_budgets")
        level1_budget = int(budgets[0]) if budgets else 60
        if power < 0 or speed < 0 or intelligence < 0:
            raise CharacterValidationError("ویژگی‌ها نمی‌توانند منفی باشند.")
        if power + speed + intelligence != level1_budget:
            raise CharacterValidationError(
                f"مجموع قدرت/سرعت/هوش باید دقیقاً {level1_budget} باشد (مدل بودجه ویژگی)."
            )

    @staticmethod
    def validate_rarity(rarity: str) -> None:
        if rarity not in Rarity.ALL:
            raise CharacterValidationError(f"کمیابی نامعتبر: {rarity}")

    # ------------------------------------------------------------------
    # Create / update
    # ------------------------------------------------------------------
    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        code: str,
        name_fa: str,
        power: int,
        speed: int,
        intelligence: int,
        name_en: Optional[str] = None,
        lore_fa: str = "",
        artwork: Optional[str] = None,
        passive_key: Optional[str] = None,
        passive_data: Optional[Dict[str, Any]] = None,
        hidden_modifiers: Optional[Dict[str, Any]] = None,
        strengths: Optional[List[str]] = None,
        weaknesses: Optional[List[str]] = None,
        price: int = 100,
        rarity: str = Rarity.COMMON,
        release_status: str = ReleaseStatus.DRAFT,
    ) -> Character:
        existing = await session.scalar(select(Character).where(Character.code == code))
        if existing:
            raise CharacterValidationError(f"شخصیت با کد «{code}» قبلاً ثبت شده است.")

        await cls.validate_base_budget(session, power, speed, intelligence)
        cls.validate_rarity(rarity)

        character = Character(
            code=code,
            name_fa=name_fa,
            name_en=name_en,
            lore_fa=lore_fa,
            artwork=artwork,
            power=power,
            speed=speed,
            intelligence=intelligence,
            passive_key=passive_key,
            passive_data=Character._dump(passive_data),
            hidden_modifiers=Character._dump(hidden_modifiers),
            strengths=Character._dump(strengths or []),
            weaknesses=Character._dump(weaknesses or []),
            price=price,
            rarity=rarity,
            release_status=release_status,
            released_at=datetime.now(timezone.utc) if release_status == ReleaseStatus.RELEASED else None,
            balance_version=1,
        )
        session.add(character)
        await session.flush()

        await cls._write_snapshot(session, character, note="ایجاد شخصیت")
        log_event(Events.CHARACTER_EVENT, {"action": "created", "code": code})
        return character

    @classmethod
    async def update(
        cls,
        session: AsyncSession,
        character_id: int,
        *,
        stats_changed: bool = True,
        note: Optional[str] = None,
        **fields: Any,
    ) -> Character:
        character = await session.get(Character, character_id)
        if not character:
            raise CharacterValidationError("شخصیت پیدا نشد.")

        stat_keys = {"power", "speed", "intelligence"}
        balance_keys = stat_keys | {"passive_key", "passive_data", "hidden_modifiers", "price", "rarity"}

        new_stats = {
            "power": fields.get("power", character.power),
            "speed": fields.get("speed", character.speed),
            "intelligence": fields.get("intelligence", character.intelligence),
        }
        if any(k in fields for k in stat_keys):
            await cls.validate_base_budget(session, **new_stats)

        if "rarity" in fields:
            cls.validate_rarity(fields["rarity"])

        balance_touched = balance_keys & set(fields)
        for key, value in fields.items():
            if key in ("passive_data", "hidden_modifiers", "strengths", "weaknesses"):
                setattr(character, key, Character._dump(value))
            else:
                setattr(character, key, value)

        if balance_touched:
            character.balance_version += 1
            await session.flush()
            await cls._write_snapshot(
                session, character, note=note or "به‌روزرسانی بالانس"
            )
            log_event(
                Events.CHARACTER_EVENT,
                {"action": "balanced", "code": character.code, "version": character.balance_version},
            )
        else:
            await session.flush()
            log_event(Events.CHARACTER_EVENT, {"action": "updated", "code": character.code})

        return character

    # ------------------------------------------------------------------
    # Availability lifecycle
    # ------------------------------------------------------------------
    @classmethod
    async def deactivate(cls, session: AsyncSession, character_id: int) -> Character:
        character = await session.get(Character, character_id)
        if not character:
            raise CharacterValidationError("شخصیت پیدا نشد.")
        character.is_active = False
        await session.flush()
        log_event(Events.CHARACTER_EVENT, {"action": "deactivated", "code": character.code})
        return character

    @classmethod
    async def reactivate(cls, session: AsyncSession, character_id: int) -> Character:
        character = await session.get(Character, character_id)
        if not character:
            raise CharacterValidationError("شخصیت پیدا نشد.")
        character.is_active = True
        await session.flush()
        log_event(Events.CHARACTER_EVENT, {"action": "reactivated", "code": character.code})
        return character

    @classmethod
    async def set_release_status(
        cls, session: AsyncSession, character_id: int, status: str
    ) -> Character:
        if status not in (ReleaseStatus.DRAFT, ReleaseStatus.RELEASED, ReleaseStatus.RETIRED):
            raise CharacterValidationError(f"وضعیت انتشار نامعتبر: {status}")
        character = await session.get(Character, character_id)
        if not character:
            raise CharacterValidationError("شخصیت پیدا نشد.")
        character.release_status = status
        if status == ReleaseStatus.RELEASED and character.released_at is None:
            character.released_at = datetime.now(timezone.utc)
        await session.flush()
        log_event(
            Events.CHARACTER_EVENT,
            {"action": "release_status", "code": character.code, "status": status},
        )
        return character

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    @staticmethod
    async def get_by_id(session: AsyncSession, character_id: int) -> Optional[Character]:
        return await session.get(Character, character_id)

    @staticmethod
    async def get_by_code(session: AsyncSession, code: str) -> Optional[Character]:
        return await session.scalar(select(Character).where(Character.code == code))

    @staticmethod
    async def list_released(session: AsyncSession) -> List[Character]:
        stmt = (
            select(Character)
            .where(Character.is_active == True)
            .where(Character.release_status == ReleaseStatus.RELEASED)
            .order_by(Character.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def list_all(session: AsyncSession) -> List[Character]:
        stmt = select(Character).order_by(Character.id.asc())
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def get_versions(session: AsyncSession, character_id: int) -> List[CharacterBalanceVersion]:
        stmt = (
            select(CharacterBalanceVersion)
            .where(CharacterBalanceVersion.character_id == character_id)
            .order_by(CharacterBalanceVersion.version.desc())
        )
        return list((await session.scalars(stmt)).all())

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    @staticmethod
    async def _write_snapshot(
        session: AsyncSession, character: Character, note: Optional[str] = None
    ) -> CharacterBalanceVersion:
        snap = CharacterBalanceVersion(
            character_id=character.id,
            version=character.balance_version,
            snapshot=json.dumps(character.snapshot(), ensure_ascii=False),
            note=note,
        )
        session.add(snap)
        await session.flush()
        return snap


RARITY_LABELS_FA = {
    Rarity.COMMON: "معمولی",
    Rarity.RARE: "کمیاب",
    Rarity.EPIC: "حماسی",
    Rarity.LEGENDARY: "افسانه‌ای",
}

STAT_EMOJI = {"power": "💪", "speed": "⚡", "intelligence": "🧠"}


def render_character_card(character: Character, *, show_price: bool = True) -> str:
    """Persian character presentation used by wiki/shop/profile."""
    rarity_fa = RARITY_LABELS_FA.get(character.rarity, character.rarity)
    lines = [
        f"🍎 **{character.name_fa}** ({rarity_fa})",
        "",
    ]
    if character.lore_fa:
        lines.append(f"_{character.lore_fa}_")
        lines.append("")
    lines.append(
        f"💪 قدرت: `{character.power}` | ⚡ سرعت: `{character.speed}` | 🧠 هوش: `{character.intelligence}`"
    )
    if character.passive_key:
        lines.append(f"✨ توانایی منفعل: `{character.passive_key}`")
    if character.strengths_list:
        lines.append(f"✅ نقاط قوت: {', '.join(character.strengths_list)}")
    if character.weaknesses_list:
        lines.append(f"⚠️ نقاط ضعف: {', '.join(character.weaknesses_list)}")
    if show_price:
        lines.append(f"💰 قیمت: `{character.price}` سیب 🍎")
    return "\n".join(lines)
