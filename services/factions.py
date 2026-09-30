"""Faction data layer (lore metadata).

Everything faction-related is DATA:
- the roster of factions lives in ``data/factions_seed.json``;
- which character belongs to which faction (and the character's role in
  it) lives in ``data/characters_seed.json``.

Adding a fourth faction therefore needs no code change: insert a row and
point characters at it. Nothing in this module (or anywhere else) may
branch on a faction CODE - the only special case that exists is "no
faction" (`faction_id is None`).

Factions are LORE ONLY: no bonuses, resistances, matchmaking or trade
restrictions are derived from them.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger
from sib.database.models import Character, Faction

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
FACTIONS_SEED_PATH = _DATA_DIR / "factions_seed.json"
CHARACTERS_SEED_PATH = _DATA_DIR / "characters_seed.json"


def _load_json(path: Path):
    if not path.exists():
        logger.warning(f"Seed file not found: {path}")
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        logger.error(f"Invalid seed file {path}: {exc}")
        return []


class FactionService:
    """Read + seed factions (data-driven, non-destructive)."""

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    @staticmethod
    async def list_active(session: AsyncSession) -> List[Faction]:
        """Active factions in display order (the UI's source list)."""
        stmt = (
            select(Faction)
            .where(Faction.is_active == True)  # noqa: E712
            .order_by(Faction.display_order.asc(), Faction.id.asc())
        )
        return list((await session.scalars(stmt)).all())

    @staticmethod
    async def get_by_code(session: AsyncSession, code: Optional[str]) -> Optional[Faction]:
        if not code:
            return None
        return await session.scalar(select(Faction).where(Faction.code == code))

    @staticmethod
    async def get_for_character(session: AsyncSession, character: Character) -> Optional[Faction]:
        """The faction of a character (None when unaffiliated)."""
        if not character or not character.faction_id:
            return None
        return await session.get(Faction, character.faction_id)

    # ------------------------------------------------------------------
    # Seeding / backfill (never overwrites existing data)
    # ------------------------------------------------------------------
    @classmethod
    async def seed_from_data(cls, session: AsyncSession) -> int:
        """Insert factions from the data file that do not exist yet.

        Existing rows are never modified: an operator's edits win.
        """
        created = 0
        for entry in _load_json(FACTIONS_SEED_PATH):
            code = entry.get("code")
            if not code:
                continue
            existing = await cls.get_by_code(session, code)
            if existing is not None:
                continue
            session.add(
                Faction(
                    code=code,
                    name_fa=entry.get("name_fa", code),
                    name_en=entry.get("name_en"),
                    lore_fa=entry.get("lore_fa", ""),
                    territory_fa=entry.get("territory_fa"),
                    symbol=entry.get("symbol"),
                    display_order=int(entry.get("display_order", 0)),
                    is_active=bool(entry.get("is_active", True)),
                )
            )
            created += 1
        if created:
            await session.flush()
            logger.info(f"Seeded {created} factions from data layer.")
        return created

    @classmethod
    async def backfill_characters_from_data(
        cls, session: AsyncSession
    ) -> Tuple[int, int]:
        """Give existing characters their faction metadata.

        Used on installations that already have characters (the character
        seed itself early-returns then). Only rows with ``faction_id IS
        NULL`` are touched - a faction (or role) that was set by hand or
        by an earlier release is never overwritten.

        Returns ``(faction_linked, roles_filled)``.
        """
        entries = _load_json(CHARACTERS_SEED_PATH)
        faction_cache: Dict[str, Optional[int]] = {}
        linked = 0
        roles = 0
        for entry in entries:
            code = entry.get("code")
            faction_code = entry.get("faction")
            if not code:
                continue
            character = await session.scalar(select(Character).where(Character.code == code))
            if character is None:
                continue
            if faction_code and character.faction_id is None:
                if faction_code not in faction_cache:
                    faction = await cls.get_by_code(session, faction_code)
                    faction_cache[faction_code] = faction.id if faction else None
                faction_id = faction_cache[faction_code]
                if faction_id is not None:
                    character.faction_id = faction_id
                    linked += 1
            role = entry.get("faction_role_fa")
            if role and not character.faction_role_fa:
                character.faction_role_fa = role
                roles += 1
        if linked or roles:
            await session.flush()
            logger.info(
                f"Faction backfill: linked {linked} character(s), filled {roles} role(s)."
            )
        return linked, roles
