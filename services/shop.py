"""Weekly rotating shop service (Phase 11).

Pricing: cards cost 300 / 2,000 / 6,000 / 15,000 Apples for COMMON /
RARE / EPIC / LEGENDARY. :meth:`ShopService.card_price` is the authoritative
price used by every screen, the balance pre-check, the debit and the ledger.
Legacy per-card/per-entry prices and client-supplied prices are ignored.

Rotation policy (deterministic and data-driven):
- Duration comes from `shop_rotation_hours` (default 168 = 7 days).
- The pool comes from `shop_pool_character_ids` (empty = all released,
  active & shop-enabled characters).
- Select five distinct cards, weighted toward lower rarities. An undersized
  pool offers all eligible cards, never duplicates or ineligible fillers.
- Persist the selection for the whole window; historical entries stay intact.

Purchases are one transaction owned by the caller (``get_session()``):
user row lock -> replay/ownership checks -> apple debit (keyed per button
press, see :meth:`ShopService.purchase`) -> card acquisition -> XP. Every
refusal is raised as :class:`ShopError` and must escape the caller's
transaction block, so nothing a refused purchase wrote is committed.
"""

import random
import re
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import (
    CARD_SHOP_PRICES,
    Character,
    PlayerCard,
    ReleaseStatus,
    ShopEntry,
    ShopEntryType,
    ShopRotation,
)
from sib.services.apple_wallet import (
    AppleWalletService,
    InsufficientFundsException,
    LedgerKeyConflictError,
    savepoints_supported,
)
from sib.services.config_store import ConfigService
from sib.services.lock_order import lock_user_row
from sib.services.player_cards import (
    DuplicateCardError,
    PlayerCardService,
    is_shop_eligible,
)


def _atomic_block(session: AsyncSession):
    """A savepoint where the dialect supports one, otherwise a no-op."""
    if savepoints_supported(session):
        return session.begin_nested()
    return nullcontext()


class ShopError(ValueError):
    """Raised for invalid shop operations."""


# Per-press operation nonce minted when a purchase button is rendered.
OPERATION_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
PURCHASE_KEY_FORMAT = "shop_purchase:{user_id}:{nonce}"
STALE_PURCHASE_TEXT = "این صفحه خرید قدیمی است؛ از فروشگاه دوباره وارد شو."

SHOP_ROTATION_SIZE = 5
# Relative per-card weights, not percentages; Legendaries remain possible.
SHOP_RARITY_WEIGHTS = {"COMMON": 60, "RARE": 25, "EPIC": 10, "LEGENDARY": 1}


@dataclass(frozen=True)
class PurchaseOutcome:
    """Result of :meth:`ShopService.purchase`.

    ``replayed`` is True when this operation key had already been processed:
    nothing was charged or granted by this call, and ``card`` is the card the
    user owns for the character now (``None`` if it was traded away since).
    """

    card: Optional[PlayerCard]
    replayed: bool
    price: int


def _utc(dt: datetime) -> datetime:
    """MySQL DATETIME columns come back naive; treat them as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class ShopService:
    """Weekly rotating shop with atomic purchases."""

    # ------------------------------------------------------------------
    # Rotation lifecycle
    # ------------------------------------------------------------------
    @classmethod
    async def current_rotation(cls, session: AsyncSession) -> ShopRotation:
        now = datetime.now(timezone.utc)

        rotation = await session.scalar(
            select(ShopRotation)
            .where(ShopRotation.is_active == True)
            .where(ShopRotation.starts_at <= now)
            .where(ShopRotation.ends_at > now)
            .order_by(ShopRotation.id.desc())
            .limit(1)
        )
        if rotation:
            return rotation
        return await cls._open_new_rotation(session, now)  # type: ignore[misc]

    @classmethod
    async def _open_new_rotation(cls, session: AsyncSession, now: datetime) -> ShopRotation:
        hours = int(await ConfigService.get(session, "shop_rotation_hours"))
        # Close any stragglers.
        old = (
            await session.scalars(
                select(ShopRotation).where(ShopRotation.is_active == True)
            )
        ).all()
        for r in old:
            r.is_active = False

        # Logical sequence seeds weighted selection independently of DB
        # auto-increment gaps.
        from sqlalchemy import func

        rotations_count = await session.scalar(select(func.count(ShopRotation.id)))
        seq = int(rotations_count or 0) + 1
        week_id = f"W{seq:04d}-{int(now.timestamp())}"

        rotation = ShopRotation(
            week_id=week_id,
            starts_at=now,
            ends_at=now + timedelta(hours=hours),
            is_active=True,
        )
        session.add(rotation)
        await session.flush()

        pool = await cls._pool_characters(session)
        selected = cls._select_characters(pool, seq)
        for index, character in enumerate(selected):
            session.add(
                ShopEntry(
                    rotation_id=rotation.id,
                    character_id=character.id,
                    entry_type=ShopEntryType.CHARACTER,
                    price_override=None,
                    badge=cls._badge_for(index),
                    sort_order=index,
                )
            )
        await session.flush()

        log_event(
            Events.SHOP_EVENT,
            {"action": "rotation_opened", "week_id": week_id, "items": len(selected)},
        )
        return rotation

    @staticmethod
    def _select_characters(pool: List[Character], sequence: int) -> List[Character]:
        """Weighted sampling without replacement, stable for a rotation."""
        rng = random.Random(sequence)
        remaining = list(pool)
        selected = []
        for _ in range(min(SHOP_ROTATION_SIZE, len(remaining))):
            character = rng.choices(
                remaining,
                weights=[SHOP_RARITY_WEIGHTS[c.rarity] for c in remaining],
                k=1,
            )[0]
            selected.append(character)
            remaining.remove(character)
        return selected

    @staticmethod
    def _badge_for(index: int) -> Optional[str]:
        if index == 0:
            return "🔥 محبوب این هفته"
        if index == 1:
            return "✨ تازه رسید"
        return None

    @classmethod
    async def _pool_characters(cls, session: AsyncSession) -> List[Character]:
        pool_ids = await ConfigService.get(session, "shop_pool_character_ids")
        stmt = (
            select(Character)
            .where(Character.is_active == True)
            .where(Character.release_status == ReleaseStatus.RELEASED)
            .where(Character.shop_enabled == True)
            .order_by(Character.id.asc())
        )
        all_chars = list((await session.scalars(stmt)).all())
        if not pool_ids:
            return all_chars
        wanted = {int(x) for x in pool_ids}
        return [c for c in all_chars if c.id in wanted]

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    @classmethod
    async def list_entries(
        cls, session: AsyncSession, *, user_id: Optional[int] = None
    ) -> List[Tuple[ShopEntry, Character, bool]]:
        """Return (entry, character, already_owned) for the current rotation.

        One joined query for entries+characters and (at most) one extra
        query for the caller's owned set — no per-entry round trips.
        """
        rotation = await cls.current_rotation(session)
        rows = (
            await session.execute(
                select(ShopEntry, Character)
                .join(Character, Character.id == ShopEntry.character_id)
                .where(ShopEntry.rotation_id == rotation.id)
                .where(ShopEntry.is_active == True)
                .order_by(ShopEntry.sort_order.asc(), ShopEntry.id.asc())
            )
        ).all()

        owned_ids: set = set()
        if user_id is not None and rows:
            character_ids = [character.id for _, character in rows]
            owned_ids = set(
                (
                    await session.scalars(
                        select(PlayerCard.character_id).where(
                            PlayerCard.user_id == user_id,
                            PlayerCard.character_id.in_(character_ids),
                        )
                    )
                ).all()
            )
        return [
            (entry, character, character.id in owned_ids) for entry, character in rows
        ]

    @classmethod
    async def get_entry(
        cls,
        session: AsyncSession,
        entry_id: int,
        *,
        user_id: Optional[int] = None,
    ) -> Tuple[Optional[ShopEntry], Optional[Character], bool]:
        """Fetch a single shop entry directly (detail screens).

        Returns (None, None, False) when the entry or its character is
        missing or the entry is not part of the current rotation; avoids
        materializing the whole rotation for one card.
        """
        entry = await session.get(ShopEntry, entry_id)
        if not entry or not entry.is_active:
            return None, None, False
        rotation = await cls.current_rotation(session)
        if entry.rotation_id != rotation.id:
            return None, None, False
        character = await session.get(Character, entry.character_id)
        if not character:
            return None, None, False
        owned = False
        if user_id is not None:
            owned = (
                await session.scalar(
                    select(PlayerCard.id).where(
                        PlayerCard.user_id == user_id,
                        PlayerCard.character_id == character.id,
                    )
                )
                is not None
            )
        return entry, character, owned

    @staticmethod
    def card_price(character: Character) -> int:
        """Authoritative rarity price; ignore legacy prices and overrides."""
        return CARD_SHOP_PRICES[character.rarity]

    # ------------------------------------------------------------------
    # Purchase (atomic)
    # ------------------------------------------------------------------
    @classmethod
    async def purchase(
        cls,
        session: AsyncSession,
        user_id: int,
        entry_id: int,
        *,
        nonce: str,
    ) -> PurchaseOutcome:
        """Buy the card for ``entry_id`` once per ``nonce``.

        ``nonce`` identifies one purchase press (minted when the item page is
        rendered). The ledger key is ``shop_purchase:{user}:{nonce}``:

        * the same nonce replays the stored outcome (no charge, no card);
        * a new nonce is a new purchase, charged in full, even if the user
          bought and then traded this character away earlier;
        * a nonce reused for a different entry is refused.

        Raises :class:`ShopError` for refusals. The caller must not commit
        after a refusal: the exception has to escape its transaction.
        """
        if not OPERATION_NONCE_RE.match(nonce or ""):
            raise ShopError(STALE_PURCHASE_TEXT)

        # User row first, before any read (lock policy rank 0): the snapshot
        # used for the replay and ownership checks is taken after the lock.
        await lock_user_row(session, user_id)
        key = PURCHASE_KEY_FORMAT.format(user_id=user_id, nonce=nonce)

        # Replays are answered before the shop state is re-checked: a button
        # pressed after its entry left the shop must not look like a new buy.
        existing = await AppleWalletService._find_ledger(session, key)
        if existing is not None:
            return await cls._replayed_outcome(session, user_id, entry_id, existing)

        now = datetime.now(timezone.utc)
        entry = await session.get(ShopEntry, entry_id)
        if not entry or not entry.is_active:
            raise ShopError("این کالا دیگر در فروشگاه نیست.")

        rotation = await session.get(ShopRotation, entry.rotation_id)
        if (
            not rotation
            or not rotation.is_active
            or _utc(rotation.starts_at) > now
            or _utc(rotation.ends_at) <= now
        ):
            raise ShopError("این چرخش فروشگاه به پایان رسیده است.")

        # Eligibility is checked at purchase time, not only when the rotation
        # was created: a character retired or shop-disabled after the rotation
        # must not be sold from a button that is still on screen. This runs
        # after the replay check above, so an already-paid nonce still replays.
        character = await session.get(Character, entry.character_id)
        if not is_shop_eligible(character):
            raise ShopError("شخصیت موردنظر در دسترس نیست.")

        # Price comes only from the persisted character's rarity, never
        # legacy price columns or callback data.
        price = cls.card_price(character)

        # Ownership check BEFORE any debit (never charge for nothing).
        already_owned = await session.scalar(
            select(PlayerCard.id).where(
                PlayerCard.user_id == user_id,
                PlayerCard.character_id == character.id,
            )
        )
        if already_owned is not None:
            raise ShopError("شما قبلاً این شخصیت را دارید!")

        # Funds pre-check BEFORE the card write, so an unaffordable purchase
        # writes nothing even if a caller commits after a refusal. The debit
        # below stays the authoritative check.
        if await AppleWalletService.get_balance(session, user_id) < price:
            raise ShopError(f"سیب کافی نیست! قیمت: {price} سیب 🍎")

        # Lock order (cards rank 4 -> wallet rank 6 -> progression rank 7): the
        # card row is written BEFORE the wallet is debited, and XP comes last.
        # The card insert, the debit and the XP award are ONE atomic unit:
        #   * on MariaDB they run inside a savepoint, so a refusal after the
        #     card write undoes all of them, even if a caller catches the
        #     ShopError and commits its outer transaction;
        #   * on SQLite (no savepoints, test engine only) an explicit refusal
        #     after the card insert deletes that card again in the same
        #     transaction, so a commit still persists nothing.
        card: Optional[PlayerCard] = None
        try:
            async with _atomic_block(session):
                try:
                    card = await PlayerCardService.acquire(session, user_id, character.id)
                except (DuplicateCardError, IntegrityError):
                    # Lost a card race before any charge.
                    raise ShopError("شما قبلاً این شخصیت را دارید!")

                try:
                    debit = await AppleWalletService.debit_once(
                        session,
                        user_id,
                        price,
                        reason="shop_purchase",
                        ref_type="shop_entry",
                        ref_id=entry.id,
                        idempotency_key=key,
                    )
                except InsufficientFundsException:
                    raise ShopError(f"سیب کافی نیست! قیمت: {price} سیب 🍎")
                except LedgerKeyConflictError:
                    raise ShopError(STALE_PURCHASE_TEXT)
                if not debit.applied:
                    # Only possible if the key appeared after our replay check;
                    # the user lock makes that unreachable. Refuse, so the card
                    # insert is undone with it and nothing is charged twice.
                    raise ShopError(STALE_PURCHASE_TEXT)

                from sib.services.player_xp import PlayerXPService

                await PlayerXPService.award_character_collection(
                    session, user_id=user_id, character_id=character.id
                )
        except ShopError:
            if card is not None and not savepoints_supported(session):
                await session.delete(card)
                await session.flush()
            raise

        log_event(
            Events.SHOP_EVENT,
            {
                "action": "purchase",
                "user_id": user_id,
                "entry_id": entry.id,
                "character": character.code,
                "price": price,
            },
        )
        return PurchaseOutcome(card=card, replayed=False, price=price)

    @classmethod
    async def _replayed_outcome(
        cls,
        session: AsyncSession,
        user_id: int,
        entry_id: int,
        existing,
    ) -> PurchaseOutcome:
        """Stored outcome for an already-processed purchase key (no writes)."""
        entry = await session.get(ShopEntry, entry_id)
        if existing is not None and (
            existing.user_id != user_id
            or existing.reason != "shop_purchase"
            or existing.ref_type != "shop_entry"
            or existing.ref_id != entry_id
        ):
            raise ShopError(STALE_PURCHASE_TEXT)
        card = None
        price = -existing.amount if existing is not None else 0
        if entry is not None:
            character_id = entry.character_id
            card = await session.scalar(
                select(PlayerCard).where(
                    PlayerCard.user_id == user_id,
                    PlayerCard.character_id == character_id,
                )
            )
        return PurchaseOutcome(card=card, replayed=True, price=price)
