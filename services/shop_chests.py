"""Shop chests: apple-priced chests with configurable weighted rewards.

Product rules (see docs/shop-chests.md):

* A chest has a unique ``code``, a display name, an optional image key, a price
  in apples, an active flag, a display order and an INDEPENDENT reward table.
* A reward is a fixed apple amount, an ``ITEM_CATALOG`` item, or an eligible
  card. A card the user already owns is converted to ``duplicate_apples`` with
  NO collection XP.
* Purchases are unlimited while the user can pay and the chest is active.
* No game-balance defaults live here: prices, weights and rewards are data,
  managed through :class:`ShopChestService` (or ``scripts/shop_chests.py``).

Opening transaction (one caller-owned transaction, committed once):

1. lock the user row (lock policy rank 0);
2. replay: an existing opening for ``(user, operation_key)`` returns its stored
   outcome - no draw, no charge, no reward;
3. validate the chest (active, complete, eligible) and draw ONE reward from an
   injectable RNG;
4. decide duplicate conversion under a locking read of the card row;
5. check funds (read-only). Every refusal above this point writes nothing;
6. inside ONE atomic block (a savepoint on MariaDB): deliver the item or card
   reward, insert the opening row as the idempotency gate (unique key), debit
   through :meth:`AppleWalletService.debit_once`, credit apples or duplicate
   compensation, award collection XP, and snapshot the balance;
7. the caller commits.

On MariaDB, a refusal inside the block rolls the block back, so the debit, the
reward and the opening row go together, even if the caller catches the refusal
and commits. On SQLite (test engine, no savepoints) the caller's rollback is the
only undo; refusals before step 6 still write nothing there. An AMBIGUOUS commit (the connection drops after the
database may have committed) is reconciled in a fresh session with the SAME
key, never a new one: see :meth:`ShopChestService.open_chest_reliably`.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.connection import get_async_sessionmaker
from sib.database.models import (
    Character,
    PlayerCard,
    ShopChest,
    ShopChestOpening,
    ShopChestReward,
)
from sib.database.models.shop_chest import (
    REWARD_KIND_APPLES,
    REWARD_KIND_CARD,
    REWARD_KIND_ITEM,
    REWARD_KINDS,
)
from sib.services.apple_wallet import (
    AppleWalletService,
    InsufficientFundsException,
    LedgerKeyConflictError,
    savepoints_supported,
)
from sib.services.golden_hour import reward_multiplier as golden_hour_multiplier
from sib.services.items import ITEM_CATALOG, ItemService
from sib.services.lock_order import lock_user_row
from sib.services.player_cards import CardError, PlayerCardService, is_shop_eligible

CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
OPERATION_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
MAX_INT = 2_147_483_647
MAX_NAME_LENGTH = 80
MAX_IMAGE_KEY_LENGTH = 120
MAX_REWARD_ROWS = 100
MAX_TOTAL_WEIGHT = 1_000_000

REASON_PURCHASE = "shop_chest_purchase"
REASON_REWARD = "shop_chest_reward"
REASON_DUPLICATE = "shop_chest_duplicate"
OPENING_REF_TYPE = "shop_chest_opening"

USER_CHEST_UNAVAILABLE = "این صندوقچه فعلاً در دسترس نیست."
STALE_OPEN_TEXT = "این صفحه قدیمی است؛ از فروشگاه دوباره وارد صندوقچه شو."
CARD_RACE_TEXT = "شما قبلاً این شخصیت را داری؛ صندوقچه دوباره باز نشد."


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ShopChestError(ValueError):
    """A refusal that is safe to show to the player. Nothing was committed."""


class ShopChestNotFoundError(ShopChestError):
    """The chest does not exist."""


class ShopChestInactiveError(ShopChestError):
    """The chest is not currently for sale."""


class ShopChestConfigError(ShopChestError):
    """The chest configuration is invalid or incomplete (never sold while so)."""


class ShopChestConflictError(ShopChestError):
    """An operation key was reused for a different chest or request."""


class ShopChestInsufficientFundsError(ShopChestError):
    """The user cannot pay the chest price."""


class _OpeningGateTaken(Exception):
    """Internal: a concurrent request with the same operation key won the gate."""


class ShopChestCommitUnknown(Exception):
    """The COMMIT outcome is unknown (connection lost during commit).

    Internal signal for :meth:`ShopChestService.open_chest_reliably`. It is
    deliberately NOT a ``ShopChestError``: the player must never be told a
    refusal when the purchase may in fact have happened.
    """


# ---------------------------------------------------------------------------
# Pure configuration model (no database access)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RewardSpec:
    """One row of a chest's reward table, before or after persistence."""

    kind: str
    weight: int
    apples: Optional[int] = None
    item_key: Optional[str] = None
    character_id: Optional[int] = None
    sort_order: int = 0


@dataclass(frozen=True)
class ChestOpenResult:
    """Outcome of an opening, built before commit and safe to render after it."""

    opening_id: int
    chest_id: int
    chest_code: str
    chest_name: str
    image_key: Optional[str]
    operation_key: str
    reward_kind: str
    apples_awarded: int
    item_key: Optional[str]
    character_id: Optional[int]
    character_name: Optional[str]
    duplicate_converted: bool
    compensation_apples: int
    price_apples: int
    balance_after: int
    chest_version: int
    replayed: bool


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_chest_fields(
    *,
    code: str,
    name: str,
    price_apples: int,
    duplicate_apples: int,
    sort_order: int,
    image_key: Optional[str],
) -> None:
    """Field-level validation. Raises :class:`ShopChestConfigError`."""
    if not isinstance(code, str) or not CODE_RE.match(code):
        raise ShopChestConfigError(
            "کد صندوقچه باید با حرف کوچک شروع شود و فقط حروف کوچک، عدد و _ داشته باشد (۳ تا ۴۰ نویسه)."
        )
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
        raise ShopChestConfigError("نام صندوقچه باید بین ۱ تا ۸۰ نویسه باشد.")
    if image_key is not None and (
        not isinstance(image_key, str) or not image_key or len(image_key) > MAX_IMAGE_KEY_LENGTH
    ):
        raise ShopChestConfigError("کلید تصویر صندوقچه نامعتبر است.")
    if not _is_int(price_apples) or not 1 <= price_apples <= MAX_INT:
        raise ShopChestConfigError("قیمت صندوقچه باید عدد صحیح مثبت باشد.")
    if not _is_int(duplicate_apples) or not 0 <= duplicate_apples <= MAX_INT:
        raise ShopChestConfigError("جبران کارت تکراری باید عدد صحیح نامنفی باشد.")
    if not _is_int(sort_order) or not -MAX_INT <= sort_order <= MAX_INT:
        raise ShopChestConfigError("ترتیب نمایش نامعتبر است.")


def validate_reward_table(rewards: Sequence[RewardSpec]) -> None:
    """Shape, weight and catalog validation for a reward table (pure).

    Card eligibility needs the database and is checked by
    :meth:`ShopChestService.validate_card_eligibility`.
    """
    if not rewards:
        raise ShopChestConfigError("جدول جایزه این صندوقچه خالی است.")
    if len(rewards) > MAX_REWARD_ROWS:
        raise ShopChestConfigError("تعداد جوایز این صندوقچه بیش از حد مجاز است.")
    total = 0
    for reward in rewards:
        if reward.kind not in REWARD_KINDS:
            raise ShopChestConfigError(f"نوع جایزه نامعتبر است: {reward.kind!r}")
        if not _is_int(reward.weight) or not 1 <= reward.weight <= MAX_TOTAL_WEIGHT:
            raise ShopChestConfigError("وزن هر جایزه باید عدد صحیح مثبت باشد.")
        total += reward.weight
        if reward.kind == REWARD_KIND_APPLES:
            if not _is_int(reward.apples) or not 1 <= reward.apples <= MAX_INT:
                raise ShopChestConfigError("مقدار سیب جایزه باید عدد صحیح مثبت باشد.")
            if reward.item_key is not None or reward.character_id is not None:
                raise ShopChestConfigError("جایزه سیب فقط مقدار سیب دارد.")
        elif reward.kind == REWARD_KIND_ITEM:
            if reward.item_key not in ITEM_CATALOG:
                raise ShopChestConfigError(
                    f"آیتم جایزه در کاتالوگ وجود ندارد: {reward.item_key!r}"
                )
            if reward.apples is not None or reward.character_id is not None:
                raise ShopChestConfigError("جایزه آیتم فقط کلید آیتم دارد.")
        else:  # card
            if not _is_int(reward.character_id) or reward.character_id < 1:
                raise ShopChestConfigError("جایزه کارت باید شناسه شخصیت داشته باشد.")
            if reward.apples is not None or reward.item_key is not None:
                raise ShopChestConfigError("جایزه کارت فقط شناسه شخصیت دارد.")
    if total > MAX_TOTAL_WEIGHT:
        raise ShopChestConfigError("مجموع وزن جوایز بیش از حد مجاز است.")


def probability_rows(rewards: Sequence[RewardSpec]) -> List[Tuple[RewardSpec, Fraction]]:
    """Exact probability of each reward: ``weight / total_weight``."""
    total = sum(reward.weight for reward in rewards)
    if total <= 0:
        return [(reward, Fraction(0)) for reward in rewards]
    return [(reward, Fraction(reward.weight, total)) for reward in rewards]


def format_probability(probability: Fraction) -> str:
    """Human percentage with two decimals; tiny non-zero values never show 0%."""
    percent = probability * 100
    if 0 < percent < Fraction(1, 100):
        return "< 0.01%"
    return f"{float(percent):.2f}%"


def pick_reward(rewards: Sequence[RewardSpec], rng: Any) -> RewardSpec:
    """Weighted draw with an injectable RNG exposing ``randrange(n)``.

    Deterministic for a given RNG state: the roll is an integer in
    ``[0, total)`` and rewards are walked in table order.
    """
    if not rewards:
        raise ShopChestConfigError("جدول جایزه این صندوقچه خالی است.")
    total = sum(reward.weight for reward in rewards)
    roll = rng.randrange(total)
    cumulative = 0
    for reward in rewards:
        cumulative += reward.weight
        if roll < cumulative:
            return reward
    raise ShopChestConfigError("جدول جایزه این صندوقچه نامعتبر است.")  # pragma: no cover


def reward_key_parts(user_id: int, operation_key: str) -> str:
    return f"shop_chest:{user_id}:{operation_key}"


def _reward_spec_from_row(row: ShopChestReward) -> RewardSpec:
    return RewardSpec(
        kind=row.kind,
        weight=row.weight,
        apples=row.apples,
        item_key=row.item_key,
        character_id=row.character_id,
        sort_order=row.sort_order,
    )


def _reward_row_from_spec(chest_id: int, spec: RewardSpec) -> ShopChestReward:
    return ShopChestReward(
        chest_id=chest_id,
        kind=spec.kind,
        weight=spec.weight,
        apples=spec.apples,
        item_key=spec.item_key,
        character_id=spec.character_id,
        sort_order=spec.sort_order,
    )


# ---------------------------------------------------------------------------
# Transaction runner (commit is isolated so ambiguous commits are detectable)
# ---------------------------------------------------------------------------


async def commit_chest_transaction(session: AsyncSession) -> None:
    """The single commit point of a chest opening (module-level for tests)."""
    await session.commit()


async def _run_in_transaction(
    body: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    """Run ``body`` in one session and commit once.

    Exceptions from ``body`` roll the whole transaction back and propagate.
    A failure of the COMMIT itself becomes :class:`ShopChestCommitUnknown`.
    """
    factory = get_async_sessionmaker()
    async with factory() as session:
        try:
            result = await body(session)
        except BaseException:
            await session.rollback()
            raise
        try:
            await commit_chest_transaction(session)
        except Exception as exc:
            raise ShopChestCommitUnknown(str(exc)) from exc
        return result


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ShopChestService:
    """Chest configuration, probabilities and atomic, idempotent openings."""

    # ----- reads -----------------------------------------------------------

    @staticmethod
    async def list_active(session: AsyncSession) -> List[ShopChest]:
        rows = await session.scalars(
            select(ShopChest)
            .where(ShopChest.is_active.is_(True))
            .order_by(ShopChest.sort_order, ShopChest.id)
        )
        return list(rows.all())

    @staticmethod
    async def get_chest(session: AsyncSession, chest_id: int) -> Optional[ShopChest]:
        return await session.get(ShopChest, chest_id)

    @staticmethod
    async def get_by_code(
        session: AsyncSession, code: str, *, lock: bool = False
    ) -> Optional[ShopChest]:
        """Look a chest up by code. ``lock=True`` takes a row lock (MariaDB)."""
        stmt = select(ShopChest).where(ShopChest.code == code)
        if lock:
            # populate_existing: a row already in this session's identity map
            # must be re-read, or a stale version would pass the check.
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return await session.scalar(stmt)

    @staticmethod
    async def load_rewards(session: AsyncSession, chest_id: int) -> List[RewardSpec]:
        rows = await session.scalars(
            select(ShopChestReward)
            .where(ShopChestReward.chest_id == chest_id)
            .order_by(ShopChestReward.sort_order, ShopChestReward.id)
        )
        return [_reward_spec_from_row(row) for row in rows.all()]

    # ----- validation ------------------------------------------------------

    @staticmethod
    async def validate_card_eligibility(
        session: AsyncSession, rewards: Sequence[RewardSpec]
    ) -> None:
        """Every card reward must be a released, shop-enabled, active character."""
        ids = sorted({r.character_id for r in rewards if r.kind == REWARD_KIND_CARD})
        if not ids:
            return
        found = {
            row.id: row
            for row in (
                await session.scalars(select(Character).where(Character.id.in_(ids)))
            ).all()
        }
        for character_id in ids:
            character = found.get(character_id)
            if not is_shop_eligible(character):
                raise ShopChestConfigError(
                    f"شخصیت جایزه کارت قابل‌عرضه نیست: {character_id}"
                )

    @classmethod
    async def validate_chest(
        cls, session: AsyncSession, chest: ShopChest
    ) -> List[RewardSpec]:
        """Full validation of a stored chest. Returns its reward table."""
        validate_chest_fields(
            code=chest.code,
            name=chest.name,
            price_apples=chest.price_apples,
            duplicate_apples=chest.duplicate_apples,
            sort_order=chest.sort_order,
            image_key=chest.image_key,
        )
        rewards = await cls.load_rewards(session, chest.id)
        validate_reward_table(rewards)
        await cls.validate_card_eligibility(session, rewards)
        return rewards

    # ----- management ------------------------------------------------------

    @classmethod
    async def save_chest(
        cls,
        session: AsyncSession,
        *,
        code: str,
        name: str,
        price_apples: int,
        duplicate_apples: int = 0,
        image_key: Optional[str] = None,
        sort_order: int = 0,
        is_active: bool = False,
        rewards: Optional[Sequence[RewardSpec]] = None,
        expected_version: Optional[int] = None,
    ) -> ShopChest:
        """Create or update a chest by ``code`` (caller commits).

        ``rewards`` replaces the whole reward table when given. The chest must
        be fully valid to be (or stay) active.

        ``expected_version`` is an optimistic-concurrency check. ``0`` means
        "this code must not exist yet"; a positive value must equal the stored
        version. Any other outcome raises :class:`ShopChestConfigError` and
        writes nothing. When it is given, the existing row is read with a row
        lock so two writers cannot both pass the check (MariaDB; SQLite has no
        row locks, so that race is not tested here).
        """
        validate_chest_fields(
            code=code,
            name=name,
            price_apples=price_apples,
            duplicate_apples=duplicate_apples,
            sort_order=sort_order,
            image_key=image_key,
        )
        if expected_version is not None and (not _is_int(expected_version) or expected_version < 0):
            raise ShopChestConfigError("expected_version باید ۰ (صندوقچه جدید) یا نسخه‌ای باشد که خوانده شده.")
        chest = await cls.get_by_code(session, code, lock=expected_version is not None)
        if expected_version is not None:
            if chest is None and expected_version != 0:
                raise ShopChestConfigError("این صندوقچه وجود ندارد؛ expected_version باید ۰ باشد.")
            if chest is not None and (expected_version == 0 or chest.version != expected_version):
                raise ShopChestConfigError("این صندوقچه همزمان تغییر کرده است؛ دوباره بارگذاری کن.")

        if rewards is not None:
            specs = list(rewards)
            validate_reward_table(specs)
            await cls.validate_card_eligibility(session, specs)
        elif chest is not None:
            specs = await cls.load_rewards(session, chest.id)
        else:
            specs = []

        if is_active:
            validate_reward_table(specs)
            await cls.validate_card_eligibility(session, specs)

        if chest is None:
            chest = ShopChest(
                code=code,
                name=name,
                image_key=image_key,
                price_apples=price_apples,
                duplicate_apples=duplicate_apples,
                is_active=is_active,
                sort_order=sort_order,
                version=1,
            )
            session.add(chest)
            await session.flush()
        else:
            chest.name = name
            chest.image_key = image_key
            chest.price_apples = price_apples
            chest.duplicate_apples = duplicate_apples
            chest.is_active = is_active
            chest.sort_order = sort_order
            chest.version = chest.version + 1

        if rewards is not None:
            await session.execute(
                delete(ShopChestReward).where(ShopChestReward.chest_id == chest.id)
            )
            for spec in specs:
                session.add(_reward_row_from_spec(chest.id, spec))
        await session.flush()
        return chest

    @classmethod
    async def set_active(
        cls,
        session: AsyncSession,
        chest_id: int,
        active: bool,
        *,
        expected_version: Optional[int] = None,
    ) -> ShopChest:
        """Activate or deactivate. Activation validates the whole chest first."""
        if expected_version is not None:
            # A versioned write must see the stored row, not an identity-map copy.
            chest = await session.scalar(
                select(ShopChest)
                .where(ShopChest.id == chest_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        else:
            chest = await session.get(ShopChest, chest_id)
        if chest is None:
            raise ShopChestNotFoundError("صندوقچه پیدا نشد.")
        if expected_version is not None and chest.version != expected_version:
            raise ShopChestConfigError("این صندوقچه همزمان تغییر کرده است؛ دوباره بارگذاری کن.")
        if active:
            await cls.validate_chest(session, chest)
        chest.is_active = active
        chest.version = chest.version + 1
        await session.flush()
        return chest

    @classmethod
    async def describe(cls, session: AsyncSession, chest: ShopChest) -> List[Tuple[RewardSpec, Fraction, str]]:
        """Reward rows with exact probabilities and a short label each."""
        rewards = await cls.load_rewards(session, chest.id)
        rows = []
        for spec, probability in probability_rows(rewards):
            rows.append((spec, probability, await cls.reward_label(session, spec)))
        return rows

    @staticmethod
    async def reward_label(session: AsyncSession, spec: RewardSpec) -> str:
        if spec.kind == REWARD_KIND_APPLES:
            return f"{spec.apples} سیب 🍎"
        if spec.kind == REWARD_KIND_ITEM:
            from sib.services.items import item_emoji, item_label

            return f"{item_emoji(spec.item_key)} {item_label(spec.item_key)}"
        character = await session.get(Character, spec.character_id)
        name = character.name_fa if character is not None else f"#{spec.character_id}"
        return f"🎴 کارت {name}"

    # ----- openings --------------------------------------------------------

    @staticmethod
    async def find_opening(
        session: AsyncSession, user_id: int, operation_key: str
    ) -> Optional[ShopChestOpening]:
        return await session.scalar(
            select(ShopChestOpening)
            .where(
                ShopChestOpening.user_id == user_id,
                ShopChestOpening.operation_key == operation_key,
            )
            .execution_options(populate_existing=True)
        )

    @classmethod
    async def _result_from_opening(
        cls, session: AsyncSession, opening: ShopChestOpening, *, replayed: bool
    ) -> ChestOpenResult:
        chest = await session.get(ShopChest, opening.chest_id)
        character_name = None
        if opening.reward_character_id is not None:
            character = await session.get(Character, opening.reward_character_id)
            character_name = character.name_fa if character is not None else None
        return ChestOpenResult(
            opening_id=opening.id,
            chest_id=opening.chest_id,
            chest_code=chest.code if chest is not None else "",
            chest_name=chest.name if chest is not None else "",
            image_key=chest.image_key if chest is not None else None,
            operation_key=opening.operation_key,
            reward_kind=opening.reward_kind,
            apples_awarded=opening.reward_apples,
            item_key=opening.reward_item_key,
            character_id=opening.reward_character_id,
            character_name=character_name,
            duplicate_converted=opening.duplicate_converted,
            compensation_apples=opening.compensation_apples,
            price_apples=opening.price_apples,
            balance_after=opening.balance_after,
            chest_version=opening.chest_version,
            replayed=replayed,
        )

    @classmethod
    async def _replay_if_recorded(
        cls,
        session: AsyncSession,
        user_id: int,
        chest_id: int,
        operation_key: str,
    ) -> Optional[ChestOpenResult]:
        opening = await cls.find_opening(session, user_id, operation_key)
        if opening is None:
            return None
        if opening.chest_id != chest_id:
            raise ShopChestConflictError(
                "این درخواست قبلاً برای صندوقچهٔ دیگری ثبت شده است."
            )
        return await cls._result_from_opening(session, opening, replayed=True)

    @classmethod
    async def open_chest(
        cls,
        session: AsyncSession,
        user_id: int,
        chest_id: int,
        operation_key: str,
        *,
        rng: Any = None,
        expected_version: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> ChestOpenResult:
        """Open ``chest_id`` once per ``operation_key``. Caller commits.

        ``expected_version`` is the chest version the player saw on the page.
        A different stored version refuses the opening before any write, so a
        stale page can never buy a changed price or reward table. A recorded
        opening is still replayed whatever the version is now.

        Raises :class:`ShopChestError` subclasses for refusals. Every refusal
        is decided BEFORE any write where possible, and every write after the
        first one happens inside one atomic block (a savepoint on MariaDB).
        So a caller that catches the refusal and commits its outer transaction
        still persists no opening row, no reward and no charge.
        """
        if not OPERATION_KEY_RE.match(operation_key or ""):
            raise ShopChestError(STALE_OPEN_TEXT)

        # 1. user row first, before any read (the snapshot is taken after it).
        await lock_user_row(session, user_id)

        # 2. replay of a recorded opening: stored outcome, nothing written.
        replay = await cls._replay_if_recorded(session, user_id, chest_id, operation_key)
        if replay is not None:
            return replay
        awarded_at = now or datetime.now(timezone.utc)

        # 3. the chest must be active and fully valid (read-only).
        chest = await session.get(ShopChest, chest_id)
        if chest is None:
            raise ShopChestNotFoundError("صندوقچه پیدا نشد.")
        if expected_version is not None and chest.version != expected_version:
            raise ShopChestError(STALE_OPEN_TEXT)
        if not chest.is_active:
            raise ShopChestInactiveError("این صندوقچه الان برای فروش نیست.")
        try:
            rewards = await cls.validate_chest(session, chest)
        except ShopChestConfigError as exc:
            logger.error(f"shop chest {chest.code} misconfigured: {exc}")
            raise ShopChestConfigError(USER_CHEST_UNAVAILABLE) from exc

        # 4. one weighted draw, then duplicate detection under a locking read.
        reward = pick_reward(rewards, rng if rng is not None else random.SystemRandom())
        duplicate = False
        compensation = 0
        apples = 0
        if reward.kind == REWARD_KIND_APPLES:
            apples = int(reward.apples)
        elif reward.kind == REWARD_KIND_CARD:
            owned = await session.scalar(
                select(PlayerCard.id)
                .where(
                    PlayerCard.user_id == user_id,
                    PlayerCard.character_id == reward.character_id,
                )
                .with_for_update()
            )
            if owned is not None:
                duplicate = True
                compensation = chest.duplicate_apples

        reward_multiplier = (
            golden_hour_multiplier(REASON_REWARD, now=awarded_at)
            if reward.kind == REWARD_KIND_APPLES
            else 1
        )
        awarded_apples = apples * reward_multiplier

        # 5. funds pre-check, still before any write. The debit below stays
        #    the authoritative check; this only keeps a refused opening from
        #    writing anything when a caller commits after the refusal.
        if await AppleWalletService.get_balance(session, user_id) < chest.price_apples:
            raise ShopChestInsufficientFundsError(
                f"سیب کافی نیست! قیمت: {chest.price_apples} سیب 🍎"
            )

        base_key = reward_key_parts(user_id, operation_key)

        async def write_opening() -> ChestOpenResult:
            # 6. item / card rewards (ranks 5 / 4), then the opening gate.
            #    Refusals here come before the gate row is flushed.
            if reward.kind == REWARD_KIND_ITEM:
                await ItemService.grant(session, user_id, reward.item_key, 1)
            elif reward.kind == REWARD_KIND_CARD and not duplicate:
                try:
                    await PlayerCardService.acquire(session, user_id, reward.character_id)
                except (CardError, IntegrityError):
                    # Lost a race for this card (or it is no longer offered):
                    # refuse, so the whole opening rolls back.
                    raise ShopChestError(CARD_RACE_TEXT)

            # 7. the opening row is the idempotency gate. Its values are final.
            opening = ShopChestOpening(
                user_id=user_id,
                chest_id=chest.id,
                operation_key=operation_key,
                chest_version=chest.version,
                price_apples=chest.price_apples,
                reward_kind=reward.kind,
                reward_apples=awarded_apples,
                reward_item_key=reward.item_key,
                reward_character_id=reward.character_id,
                duplicate_converted=duplicate,
                compensation_apples=compensation,
                balance_after=0,
            )
            try:
                session.add(opening)
                await session.flush()
            except IntegrityError as exc:
                raise _OpeningGateTaken() from exc

            # 8. one debit for this opening, keyed to it (wallet, rank 6).
            try:
                debit = await AppleWalletService.debit_once(
                    session,
                    user_id,
                    chest.price_apples,
                    reason=REASON_PURCHASE,
                    ref_type=OPENING_REF_TYPE,
                    ref_id=opening.id,
                    idempotency_key=f"{base_key}:charge",
                )
            except InsufficientFundsException:
                raise ShopChestInsufficientFundsError(
                    f"سیب کافی نیست! قیمت: {chest.price_apples} سیب 🍎"
                )
            except LedgerKeyConflictError:
                raise ShopChestError(STALE_OPEN_TEXT)
            if not debit.applied:  # pragma: no cover - the gate is new, so the key is too
                raise ShopChestError(STALE_OPEN_TEXT)

            # 9. apple rewards and duplicate compensation (wallet credits).
            if reward.kind == REWARD_KIND_APPLES:
                await AppleWalletService.earn(
                    session,
                    user_id,
                    apples,
                    reason=REASON_REWARD,
                    ref_type=OPENING_REF_TYPE,
                    ref_id=opening.id,
                    idempotency_key=f"{base_key}:reward",
                    reward_multiplier=reward_multiplier,
                    occurred_at=awarded_at,
                )
            elif duplicate and compensation > 0:
                await AppleWalletService.earn(
                    session,
                    user_id,
                    compensation,
                    reason=REASON_DUPLICATE,
                    ref_type=OPENING_REF_TYPE,
                    ref_id=opening.id,
                    idempotency_key=f"{base_key}:dup",
                )

            # 10. collection XP for a NEW card only (progression, always last).
            if reward.kind == REWARD_KIND_CARD and not duplicate:
                from sib.services.player_xp import PlayerXPService

                await PlayerXPService.award_character_collection(
                    session, user_id=user_id, character_id=reward.character_id
                )

            balance = await AppleWalletService.get_balance(session, user_id)
            opening.balance_after = balance
            await session.flush()

            character_name = None
            if reward.character_id is not None:
                character = await session.get(Character, reward.character_id)
                character_name = character.name_fa if character is not None else None

            log_event(
                Events.SHOP_EVENT,
                {
                    "action": "chest_open",
                    "user_id": user_id,
                    "chest_id": chest.id,
                    "reward_kind": reward.kind,
                    "duplicate": duplicate,
                    "price": chest.price_apples,
                    "operation_key": operation_key,
                },
            )
            return ChestOpenResult(
                opening_id=opening.id,
                chest_id=chest.id,
                chest_code=chest.code,
                chest_name=chest.name,
                image_key=chest.image_key,
                operation_key=operation_key,
                reward_kind=reward.kind,
                apples_awarded=awarded_apples,
                item_key=reward.item_key,
                character_id=reward.character_id,
                character_name=character_name,
                duplicate_converted=duplicate,
                compensation_apples=compensation,
                price_apples=chest.price_apples,
                balance_after=balance,
                chest_version=chest.version,
                replayed=False,
            )

        if savepoints_supported(session):
            # Everything from the first write to the gate, debit and credits is
            # one savepoint: any refusal (including a lost gate race) undoes it.
            try:
                async with session.begin_nested():
                    return await write_opening()
            except _OpeningGateTaken:
                # A concurrent request with the same key committed first.
                replay = await cls._replay_if_recorded(
                    session, user_id, chest_id, operation_key
                )
                if replay is None:
                    raise ShopChestError(STALE_OPEN_TEXT)
                return replay

        # SQLite (test engine only, no savepoints): the caller's transaction is
        # the only undo. Refusals above this point still write nothing.
        try:
            return await write_opening()
        except _OpeningGateTaken as exc:
            raise exc.__cause__ or ShopChestError(STALE_OPEN_TEXT)

    @classmethod
    async def open_chest_reliably(
        cls,
        user_id: int,
        chest_id: int,
        operation_key: str,
        *,
        rng: Any = None,
        attempts: int = 2,
        expected_version: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> ChestOpenResult:
        """Open a chest in its own transaction, surviving an ambiguous commit.

        If COMMIT's outcome is unknown, a fresh session looks up the opening
        for ``operation_key``: found means the purchase happened and its stored
        outcome is returned; not found means it did not commit, and the open
        is retried with the SAME key (the key is never regenerated).
        """
        awarded_at = now or datetime.now(timezone.utc)
        for attempt in range(attempts):
            try:
                return await _run_in_transaction(
                    lambda session: cls.open_chest(
                        session,
                        user_id,
                        chest_id,
                        operation_key,
                        rng=rng,
                        expected_version=expected_version,
                        now=awarded_at,
                    )
                )
            except ShopChestCommitUnknown as exc:
                logger.warning(
                    f"shop chest commit unknown (user={user_id}, attempt={attempt}): {exc}"
                )
                recorded = await cls.reconcile(user_id, operation_key)
                if recorded is not None:
                    return recorded
        raise ShopChestError(
            "نتیجه باز کردن صندوقچه مشخص نشد؛ چند لحظه بعد دوباره امتحان کن."
        )

    @classmethod
    async def reconcile(cls, user_id: int, operation_key: str) -> Optional[ChestOpenResult]:
        """Read-only lookup of an opening by key, in a fresh session."""
        factory = get_async_sessionmaker()
        async with factory() as session:
            opening = await cls.find_opening(session, user_id, operation_key)
            if opening is None:
                return None
            return await cls._result_from_opening(session, opening, replayed=True)


# ---------------------------------------------------------------------------
# Bulk configuration (used by scripts/shop_chests.py)
# ---------------------------------------------------------------------------


def parse_reward_entries(entries: Sequence[Dict[str, Any]]) -> List[RewardSpec]:
    """Build reward specs from JSON-style dicts. Raises ShopChestConfigError."""
    specs: List[RewardSpec] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ShopChestConfigError(f"جایزه {index} باید یک شیٔ JSON باشد.")
        kind = entry.get("kind")
        if kind not in REWARD_KINDS:
            raise ShopChestConfigError(f"نوع جایزه {index} نامعتبر است.")
        specs.append(
            RewardSpec(
                kind=kind,
                weight=entry.get("weight"),
                apples=entry.get("apples"),
                item_key=entry.get("item_key"),
                character_id=entry.get("character_id"),
                sort_order=int(entry.get("sort_order", index)),
            )
        )
    return specs


async def _resolve_character_codes(
    session: AsyncSession, entries: Sequence[Any]
) -> List[Any]:
    """Replace ``character_code`` with this database's ``character_id``.

    Character ids differ between databases, so a configuration file names a
    card by its stable code. A code that does not exist here is refused, and
    eligibility is still enforced on save. Entries without ``character_code``
    pass through unchanged.
    """
    resolved: List[Any] = []
    for entry in entries:
        if not isinstance(entry, dict) or "character_code" not in entry:
            resolved.append(entry)
            continue
        code = entry["character_code"]
        if "character_id" in entry:
            raise ShopChestConfigError("جایزه کارت نباید هم کد و هم شناسه شخصیت داشته باشد.")
        character_id = await session.scalar(
            select(Character.id).where(Character.code == code)
        ) if isinstance(code, str) and code else None
        if character_id is None:
            raise ShopChestConfigError(f"شخصیتی با کد {code!r} در این پایگاه داده وجود ندارد.")
        resolved_entry = {k: v for k, v in entry.items() if k != "character_code"}
        resolved_entry["character_id"] = character_id
        resolved.append(resolved_entry)
    return resolved


async def apply_chest_configs(
    session: AsyncSession, chests: Sequence[Dict[str, Any]]
) -> List[str]:
    """Upsert chests and their reward tables from parsed config. Caller commits.

    Each dict: code, name, price_apples, duplicate_apples, image_key,
    sort_order, is_active, rewards (list of reward dicts; a card reward may
    name its character by ``character_code`` instead of ``character_id``), and
    ``expected_version``: the version the operator read (``0`` for a new chest).
    The check is per chest and is atomic with its write (see
    :meth:`ShopChestService.save_chest`). Returns the codes.
    """
    codes: List[str] = []
    for item in chests:
        if "expected_version" not in item:
            raise ShopChestConfigError(
                f"صندوقچه {item.get('code')!r} بدون expected_version است."
            )
        rewards = parse_reward_entries(
            await _resolve_character_codes(session, item.get("rewards") or [])
        )
        await ShopChestService.save_chest(
            session,
            code=item.get("code"),
            name=item.get("name"),
            price_apples=item.get("price_apples"),
            duplicate_apples=item.get("duplicate_apples", 0),
            image_key=item.get("image_key"),
            sort_order=int(item.get("sort_order", 0)),
            is_active=bool(item.get("is_active", False)),
            rewards=rewards,
            expected_version=item["expected_version"],
        )
        codes.append(item["code"])
    return codes
