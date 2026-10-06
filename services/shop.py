"""Weekly rotating shop service (Phase 11).

Rotation policy (deterministic and data-driven):
- Duration comes from `shop_rotation_hours` (default 168 = 7 days).
- The pool comes from `shop_pool_character_ids` (empty = all released
  & shop-enabled characters).
- Entries rotate round-robin over the pool, so every character re-enters
  the shop in later weeks automatically. Historical entries remain
  immutable.

Purchases are atomic: price check -> apple debit -> card acquisition in
one transaction with an idempotency key.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import (
    Character,
    PlayerCard,
    ReleaseStatus,
    ShopEntry,
    ShopEntryType,
    ShopRotation,
)
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.config_store import ConfigService
from sib.services.player_cards import DuplicateCardError, PlayerCardService


class ShopError(ValueError):
    """Raised for invalid shop operations."""


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

        last = await session.scalar(
            select(ShopRotation).order_by(ShopRotation.id.desc()).limit(1)
        )
        # Logical sequence = number of rotations so far (deterministic
        # round-robin, independent of DB auto-increment gaps).
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
        if pool:
            # Deterministic round-robin by rotation sequence.
            offset = (seq - 1) % len(pool)
            ordered = pool[offset:] + pool[:offset]
            for index, character in enumerate(ordered):
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
            {"action": "rotation_opened", "week_id": week_id, "items": len(pool)},
        )
        return rotation

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
    def entry_price(entry: ShopEntry, character: Character) -> int:
        return entry.price_override if entry.price_override is not None else character.price

    # ------------------------------------------------------------------
    # Purchase (atomic)
    # ------------------------------------------------------------------
    @classmethod
    async def purchase(
        cls, session: AsyncSession, user_id: int, entry_id: int
    ) -> PlayerCard:
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

        character = await session.get(Character, entry.character_id)
        if not character or not character.is_active:
            raise ShopError("شخصیت موردنظر در دسترس نیست.")

        price = cls.entry_price(entry, character)

        # Ownership check BEFORE any debit (never charge for nothing).
        already_owned = await session.scalar(
            select(PlayerCard.id).where(
                PlayerCard.user_id == user_id,
                PlayerCard.character_id == character.id,
            )
        )
        if already_owned is not None:
            raise ShopError("شما قبلاً این شخصیت را دارید!")

        # Atomic debit + card acquisition.
        try:
            await AppleWalletService.spend(
                session,
                user_id,
                price,
                reason="shop_purchase",
                ref_type="shop_entry",
                ref_id=entry.id,
                idempotency_key=f"shop:{entry.id}:{user_id}",
            )
        except InsufficientFundsException:
            raise ShopError(f"سیب کافی نیست! قیمت: {price} سیب 🍎")

        try:
            card = await PlayerCardService.acquire(session, user_id, character.id)
        except DuplicateCardError:
            # Race backstop: re-raise so the transaction rolls back the debit.
            raise ShopError("شما قبلاً این شخصیت را دارید!")

        from sib.services.player_xp import PlayerXPService

        await PlayerXPService.award_character_collection(
            session, user_id=user_id, character_id=character.id
        )

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
        return card
