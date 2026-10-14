"""Authoritative group power from recently seen users' selected Main Cards."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from sib.core.logging import logger
from sib.database.models import Character, Inv2GroupMember
from sib.services.card_progression import MAX_CARD_LEVEL, MIN_CARD_LEVEL, card_attributes
from sib.services.config_store import ConfigService
from sib.services.player_cards import PlayerCardService


class DbGroupPowerResolver:
    """Fail closed if even one active member's displayed power is uncertain."""

    async def average_main_card_power(self, *, group_id: int, session) -> float | None:
        try:
            days = await ConfigService.get(session, "inv2_member_active_window_days")
            # Invalid overrides are unknown membership windows, not an excuse
            # to guess; the migration and in-code default both supply 30.
            if type(days) not in (int, str) or not str(days).isdigit() or int(days) <= 0:
                return None
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
            members = list((await session.scalars(
                select(Inv2GroupMember.user_id).where(
                    Inv2GroupMember.group_id == group_id,
                    Inv2GroupMember.last_seen_at >= cutoff,
                ).order_by(Inv2GroupMember.user_id)
            )).all())
            if not members:
                return None

            total = 0
            for user_id in members:
                card = await PlayerCardService.get_main_card(session, user_id)
                if card is None or type(card.level) is not int or not MIN_CARD_LEVEL <= card.level <= MAX_CARD_LEVEL:
                    return None
                character = await session.get(Character, card.character_id)
                if character is None or any(
                    type(getattr(character, key)) is not int or getattr(character, key) < 0
                    for key in ("power", "speed", "intelligence")
                ):
                    return None
                power = card_attributes(card, character).power
                if type(power) is not int or power < 0:
                    return None
                total += power
            return total / len(members)
        except Exception:
            logger.debug("inv2 group power unresolved group=%s", group_id, exc_info=True)
            return None
