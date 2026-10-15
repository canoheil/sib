"""Authoritative group power from recently seen users' selected Main Cards."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from sib.core.logging import logger
from sib.database.models import Character, Inv2GroupMember
from sib.services.card_progression import MAX_CARD_LEVEL, MIN_CARD_LEVEL, card_attributes
from sib.services.config_store import ConfigService
from sib.services.player_cards import PlayerCardService


# Minimum number of active members with a Main Card before a group may spawn.
# `system_config.invasion_min_players_with_card` is authoritative when present
# and valid; this constant is the safe built-in default when it is missing.
INVASION_MIN_PLAYERS_WITH_CARD_KEY = "invasion_min_players_with_card"
DEFAULT_INVASION_MIN_PLAYERS_WITH_CARD = 3


class DbGroupPowerResolver:
    """Average Main Card power over the active members who selected one.

    Members inside the activity window without a Main Card are ignored: the
    average covers the members who have one, and a group spawns only when at
    least `invasion_min_players_with_card` of them do. Everything else stays
    fail-closed with None - an invalid activity window, a card that exists but
    cannot be scored, and any database error.
    """

    async def _minimum_players(self, session) -> int | None:
        raw = await ConfigService.get(session, INVASION_MIN_PLAYERS_WITH_CARD_KEY)
        if raw is None:  # key absent from both system_config and the defaults
            raw = DEFAULT_INVASION_MIN_PLAYERS_WITH_CARD
        if type(raw) not in (int, str) or not str(raw).isdigit() or int(raw) <= 0:
            # An unusable threshold is a configuration error, not a licence to
            # spawn: no threshold means no spawn.
            return None
        return int(raw)

    async def average_main_card_power(self, *, group_id: int, session) -> float | None:
        try:
            days = await ConfigService.get(session, "inv2_member_active_window_days")
            # Invalid overrides are unknown membership windows, not an excuse
            # to guess; the migration and in-code default both supply 30.
            if type(days) not in (int, str) or not str(days).isdigit() or int(days) <= 0:
                return None
            minimum = await self._minimum_players(session)
            if minimum is None:
                logger.warning(
                    "inv2 spawn skipped: group=%s invalid %s",
                    group_id, INVASION_MIN_PLAYERS_WITH_CARD_KEY,
                )
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

            powers = []
            ignored = []
            for user_id in members:
                card = await PlayerCardService.get_main_card(session, user_id)
                if card is None:
                    ignored.append(user_id)
                    continue
                # A card that exists but cannot be scored is corrupt data, not
                # "this member has no card": stay fail-closed instead of
                # averaging around unknown power.
                if type(card.level) is not int or not MIN_CARD_LEVEL <= card.level <= MAX_CARD_LEVEL:
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
                powers.append(power)

            if ignored:
                logger.debug(
                    "inv2 group power ignoring members without main card group=%s ignored=%s",
                    group_id, ignored,
                )
            if len(powers) < minimum:
                logger.info(
                    "inv2 spawn skipped: group=%s players_with_card=%s required=%s",
                    group_id, len(powers), minimum,
                )
                return None
            return sum(powers) / len(powers)
        except Exception:
            logger.debug("inv2 group power unresolved group=%s", group_id, exc_info=True)
            return None
