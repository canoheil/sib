"""Starter pack: the gift a brand-new player gets on their very first /start.

Why this exists (growth diagnosis, ``docs/growth-diagnosis-fa.md``)

Before this service a new account was created completely empty: no card, no
apples, nothing to do except read "add me to a group". The cheapest card in
the shop costs ``SHOP_CARD_PRICE_APPLES`` (300) and the only income sources
paid ~7.5 Apples every five minutes (the «سیب» claim) or ~26 Apples every
fifteen minutes (one Apple Mine shift), so the first card - the object the
whole game is about - was roughly **three hours** away. Most players never
got there.

The starter pack closes that gap:

* **one released shop card** (level 1) is granted and immediately selected as
  the player's Main Card, so battles, duels and invasions work from the first
  minute;
* **``STARTER_APPLES``** normal Apples are credited through the auditable
  wallet ledger, enough to open a chest or plant the first garden plot.

Exactly-once semantics
----------------------

There is no new column and no migration: the pack is paid through
``AppleWalletService.earn`` with the fixed idempotency key
``starter_pack:<user id>``, and that ledger row *is* the durable
"already granted" marker. A replay (double tap, duplicate Telegram update,
retry after a crash, a second /start days later) finds the key and returns
``granted=False`` without paying or duplicating the card, so the pack can
never be farmed by leaving and re-entering a group.

The card is only granted when the player does not already own that
character; a player who traded their starter card away cannot mint a new one,
because the ledger marker still exists.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import (
    AppleTransaction,
    Character,
    PlayerCard,
    ReleaseStatus,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.player_cards import (
    CardError,
    DuplicateCardError,
    PlayerCardService,
    is_shop_eligible,
)


#: Normal Apples granted to every brand-new player.
STARTER_APPLES = 100

#: Preferred starter character (an empire faction card available in the shop
#: from the original four). When it is missing or retired, the service falls
#: back to the cheapest released shop character so the gift never breaks.
PREFERRED_STARTER_CARD_CODE = "sib_sorkh"

#: Ledger reason + idempotency key. The key is the durable "already granted"
#: marker (see the module docstring) and must never change per install.
STARTER_LEDGER_REASON = "starter_pack"
STARTER_LEDGER_KEY = "starter_pack:{user_id}"


@dataclass(frozen=True)
class StarterPack:
    """What ``OnboardingService.grant_starter_pack`` actually did.

    ``granted`` is False when the pack had already been paid before (a
    replay), in which case ``apples``/``card_name`` describe nothing new and
    must not be announced again.
    """

    granted: bool
    apples: int = 0
    card_name: Optional[str] = None
    balance: int = 0


class OnboardingService:
    """First-session gift: starter card + starter Apples, paid exactly once."""

    @staticmethod
    async def _ledger_marker_exists(session: AsyncSession, user_id: int) -> bool:
        key = STARTER_LEDGER_KEY.format(user_id=user_id)
        found = await session.scalar(
            select(AppleTransaction.id).where(
                AppleTransaction.idempotency_key == key
            )
        )
        return found is not None

    @classmethod
    async def _starter_character(cls, session: AsyncSession) -> Optional[Character]:
        """The starter card: preferred code first, then cheapest shop card.

        Both branches require the character to be shop-eligible (released,
        shop-enabled, active) so the gift can never hand out a character the
        shop itself refuses to sell - an unreleased or retired card would also
        break the "cards in the shop are the whole roster" rule.
        """
        preferred = await session.scalar(
            select(Character).where(Character.code == PREFERRED_STARTER_CARD_CODE)
        )
        if is_shop_eligible(preferred):
            return preferred

        candidates = list(
            (
                await session.scalars(
                    select(Character)
                    .where(
                        Character.is_active.is_(True),
                        Character.shop_enabled.is_(True),
                        Character.release_status == ReleaseStatus.RELEASED,
                    )
                    .order_by(Character.id.asc())
                )
            ).all()
        )
        return candidates[0] if candidates else None

    @classmethod
    async def grant_starter_pack(
        cls, session: AsyncSession, user_id: int
    ) -> StarterPack:
        """Pay the starter pack once per account; safe to call on every /start.

        Runs inside the caller's transaction (``/start`` already opened one),
        so the card, the ledger row and the user row commit together or not at
        all. Never raises for a missing roster: a deployment without characters
        simply pays the Apples.
        """
        if await cls._ledger_marker_exists(session, user_id):
            return StarterPack(
                granted=False,
                balance=await AppleWalletService.get_balance(session, user_id),
            )

        card_name: Optional[str] = None
        try:
            character = await cls._starter_character(session)
        except Exception:  # pragma: no cover - defensive, never block /start
            logger.exception("starter pack: character lookup failed")
            character = None

        if character is not None:
            owned = await session.scalar(
                select(PlayerCard.id).where(
                    PlayerCard.user_id == user_id,
                    PlayerCard.character_id == character.id,
                )
            )
            if owned is None:
                card = None
                try:
                    card = await PlayerCardService.acquire(
                        session, user_id, character.id
                    )
                except DuplicateCardError:
                    # A concurrent /start won the race; the card exists, so
                    # there is nothing left to grant.
                    logger.info(
                        "starter pack: card already owned user=%s", user_id
                    )
                except CardError:
                    logger.exception(
                        "starter pack: card could not be granted user=%s", user_id
                    )
                if card is not None:
                    card_name = character.name_fa
                    # The whole point of the gift: a Main Card from minute one
                    # (invasion power, duels and battles all read it). The
                    # selection goes through the canonical service, which
                    # clears any other favourite under the user lock; a failure
                    # there must not lose the card that was just acquired.
                    try:
                        await PlayerCardService.set_favorite(
                            session, user_id, card.id
                        )
                    except Exception:  # pragma: no cover - defensive
                        logger.warning(
                            "starter pack: could not select the Main Card "
                            "user=%s card=%s",
                            user_id,
                            card.id,
                            exc_info=True,
                        )

        balance = await AppleWalletService.earn(
            session,
            user_id,
            STARTER_APPLES,
            reason=STARTER_LEDGER_REASON,
            idempotency_key=STARTER_LEDGER_KEY.format(user_id=user_id),
        )

        log_event(
            Events.ITEM_GRANTED,
            {
                "action": "starter_pack",
                "user_id": user_id,
                "apples": STARTER_APPLES,
                "card": card_name,
            },
        )
        return StarterPack(
            granted=True,
            apples=STARTER_APPLES,
            card_name=card_name,
            balance=balance,
        )
