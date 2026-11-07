"""Database connection management and session handling."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import create_engine, select
from sib.core.config import settings
from sib.core.logging import logger
from sib.database.models import (
    Base,
    Game,
    SponsorChannel,
    ChannelType,
    AdCampaign,
    FrequencyMode,
)

from sqlalchemy.pool import NullPool, AsyncAdaptedQueuePool

# Global async engine and session factory
_async_engine: AsyncEngine | None = None
_async_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_async_engine(db_url: str | None = None) -> AsyncEngine:
    """Get or create the global async database engine."""
    global _async_engine
    if _async_engine is None:
        url = db_url or settings.DATABASE_URL
        _async_engine = create_async_engine(
            url,
            echo=(settings.LOG_LEVEL == "DEBUG"),
            poolclass=NullPool,
        )
    return _async_engine


def get_async_sessionmaker(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory."""
    global _async_sessionmaker
    if _async_sessionmaker is None:
        eng = engine or get_async_engine()
        _async_sessionmaker = async_sessionmaker(
            bind=eng,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _async_sessionmaker


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for obtaining an async session."""
    session_factory = get_async_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def seed_default_data(
    session: AsyncSession,
    *,
    include_sample_ad: bool = True,
) -> None:
    """Seed required default games, official channel, and sample ad campaign."""
    default_games = [
        {
            "code": "high_card",
            "name_fa": "کارت بالا (های کارت)",
            "description_fa": "انتخاب یک کارت تصادفی، کارت بالاتر برنده است!",
            "supports_solo": True, "supports_2v2": False, "supports_4v4": False,
            "turn_timeout_seconds": settings.HIGH_CARD_TIMEOUT,
        },
        {
            "code": "rock_paper_scissors",
            "name_fa": "سنگ، کاغذ، قیچی",
            "description_fa": "بازی خاطره‌انگیز سنگ کاغذ قیچی!",
            "supports_solo": True, "supports_2v2": False, "supports_4v4": False,
            "turn_timeout_seconds": settings.ROCK_PAPER_SCISSORS_TIMEOUT,
        },
        {
            "code": "tic_tac_toe",
            "name_fa": "دوز (تیک‌تاک‌تو)",
            "description_fa": "رقابت نوبتی ۳ در ۳ برای رسیدن به خط مستقیم!",
            "supports_solo": True, "supports_2v2": False, "supports_4v4": False,
            "turn_timeout_seconds": settings.TIC_TAC_TOE_TIMEOUT,
        },
        {
            "code": "connect_four",
            "name_fa": "چهار در یک ردیف",
            "description_fa": "صفحه ۷ در ۶ با قابلیت بازی انفرادی و تیمی ۲ به ۲ و ۴ به ۴!",
            "supports_solo": True, "supports_2v2": True, "supports_4v4": True,
            "turn_timeout_seconds": settings.CONNECT_FOUR_TIMEOUT,
        },
        {
            "code": "battleship",
            "name_fa": "کشتی جنگی (بتل‌شیپ)",
            "description_fa": "صفحه ۸ در ۸، جای‌گذاری ناوگان دریایی و حمله به مختصات حریف!",
            "supports_solo": True, "supports_2v2": True, "supports_4v4": True,
            "turn_timeout_seconds": settings.BATTLESHIP_TIMEOUT,
        },
        {
            "code": "apple_battle",
            "name_fa": "نبرد سیبی",
            "description_fa": "نبرد روایی کارت‌ها با حریف واقعی از گروه‌های دیگر!",
            "supports_solo": True, "supports_2v2": False, "supports_4v4": False,
            "turn_timeout_seconds": 600,
        },
    ]

    for g_data in default_games:
        existing = await session.scalar(select(Game).where(Game.code == g_data["code"]))
        if not existing:
            session.add(Game(**g_data))

    if settings.OFFICIAL_CHANNEL_ID:
        existing_ch = await session.scalar(
            select(SponsorChannel).where(SponsorChannel.channel_id == settings.OFFICIAL_CHANNEL_ID)
        )
        if not existing_ch:
            session.add(SponsorChannel(
                channel_id=settings.OFFICIAL_CHANNEL_ID,
                title="کانال رسمی سیب",
                invite_link=settings.OFFICIAL_CHANNEL_LINK,
                channel_type=ChannelType.OFFICIAL,
                is_required=True, is_active=True, priority=100,
            ))

    if include_sample_ad:
        existing_ad = await session.scalar(select(AdCampaign).limit(1))
        if not existing_ad:
            session.add(AdCampaign(
                sponsor_name="حامی سیب",
                campaign_name="کمپین خوش‌آمدگویی",
                ad_text="با حمایت از کانال رسمی، از توسعه بازی‌های جدید سیب پشتیبانی کنید!",
                cta_label="کانال حامی",
                target_url=settings.OFFICIAL_CHANNEL_LINK or "https://t.me/sibbot",
                is_active=True, priority=10,
                frequency_mode=FrequencyMode.SMART, max_impressions=10000,
            ))


class CharacterSeedError(RuntimeError):
    """The shipped character seed data could not be applied.

    Raised instead of booting with a partially applied roster: a card that
    is missing from the database is missing from the shop, the wiki and
    every collection, so a silent skip would be an invisible content loss.
    """

    def __init__(self, failures: list[tuple[str, str]]) -> None:
        self.failures = list(failures)
        detail = "; ".join(f"{code}: {error}" for code, error in self.failures)
        super().__init__(
            f"{len(self.failures)} character seed entr"
            f"{'y' if len(self.failures) == 1 else 'ies'} failed -> {detail}"
        )


async def seed_characters(session) -> int:
    """Sync the roster and explicitly supplied official content, idempotently.

    ``data/characters_seed.json`` is the only shipped definition source.
    Missing cards are created. Existing entries WITHOUT ``affiliations_fa``
    keep the append-only behavior: their definitions are not overwritten.
    Entries WITH that key have received official content; only their name,
    affiliations, base stats and story are synced, verbatim. Only changed
    fields are written, keeping reruns and balance snapshots idempotent.

    An explicit ``sync_faction: true`` additionally syncs the primary
    faction link for that entry, using its declared existing faction code.
    Other faction links retain their old behavior. Availability, artwork,
    passive mechanics, legacy roles and all player/economy state are
    preserved. All writes belong to the caller's transaction. Any invalid
    entry raises CharacterSeedError so startup cannot commit a partial sync.
    """
    from pathlib import Path
    import json as _json

    from sib.database.models import Character, CharacterBalanceVersion, ReleaseStatus
    from sib.services.characters import CharacterService, CharacterValidationError
    from sib.services.factions import FactionService

    # Faction lore metadata is seeded/backfilled on EVERY startup (also on
    # installations that already have characters): existing rows are only
    # filled where the faction is still NULL, never overwritten.
    await FactionService.seed_from_data(session)
    await FactionService.backfill_characters_from_data(session)

    seed_path = Path(__file__).resolve().parents[1] / "data" / "characters_seed.json"
    if not seed_path.exists():
        logger.warning(f"Character seed file not found: {seed_path}")
        return 0

    entries = _json.loads(seed_path.read_text(encoding="utf-8"))
    existing_codes = set((await session.scalars(select(Character.code))).all())

    created = 0
    updated = 0
    failures: list[tuple[str, str]] = []
    official_keys = (
        "name_fa", "affiliations_fa", "power", "speed", "intelligence", "lore_fa"
    )
    for entry in entries:
        code = entry.get("code") or "<missing code>"
        try:
            official_fields = None
            sync_faction = entry.get("sync_faction", False)
            if type(sync_faction) is not bool:
                raise CharacterValidationError("sync_faction must be an explicit boolean.")
            if sync_faction and "affiliations_fa" not in entry:
                raise CharacterValidationError("Faction sync requires official card content.")
            if "affiliations_fa" in entry:
                missing = [key for key in official_keys if key not in entry]
                if missing:
                    raise CharacterValidationError(
                        f"Official content is incomplete: {', '.join(missing)}"
                    )
                for key in ("name_fa", "affiliations_fa", "lore_fa"):
                    if not isinstance(entry[key], str):
                        raise CharacterValidationError(
                            f"Official {key} must be verbatim text, not null or another type."
                        )
                for key in ("power", "speed", "intelligence"):
                    if type(entry[key]) is not int:
                        raise CharacterValidationError(f"Official {key} must be an integer.")
                await CharacterService.validate_base_budget(
                    session, entry["power"], entry["speed"], entry["intelligence"]
                )
                official_fields = {key: entry[key] for key in official_keys}
                if sync_faction:
                    if "faction" not in entry:
                        raise CharacterValidationError("Faction sync requires an explicit faction key.")
                    faction_code = entry["faction"]
                    if faction_code is not None and not isinstance(faction_code, str):
                        raise CharacterValidationError("The official faction code must be text or null.")
                    official_faction = await FactionService.get_by_code(session, faction_code)
                    if faction_code is not None and official_faction is None:
                        raise CharacterValidationError(f"Unknown official faction: {faction_code}")
                    official_fields["faction_id"] = (
                        official_faction.id if official_faction is not None else None
                    )

            if code in existing_codes:
                if official_fields is not None:
                    character = await CharacterService.get_by_code(session, code)
                    changed = {
                        key: value for key, value in official_fields.items()
                        if getattr(character, key) != value
                    }
                    if changed:
                        await CharacterService.update(
                            session, character.id, note="ثبت اطلاعات رسمی کارت", **changed
                        )
                        updated += 1
                continue  # Unmarked definitions retain append-only semantics.

            faction = await FactionService.get_by_code(session, entry.get("faction"))
            character = await CharacterService.create(
                session,
                code=entry["code"],
                name_fa=entry["name_fa"],
                name_en=entry.get("name_en"),
                lore_fa=entry.get("lore_fa", ""),
                artwork=entry.get("artwork"),
                power=entry["power"],
                speed=entry["speed"],
                intelligence=entry["intelligence"],
                passive_key=entry.get("passive_key"),
                passive_data=entry.get("passive_data"),
                hidden_modifiers=entry.get("hidden_modifiers"),
                strengths=entry.get("strengths"),
                weaknesses=entry.get("weaknesses"),
                # Existing rows are classified by migration 081; do not
                # overwrite later operator rarity edits on startup.
                rarity=entry.get("rarity", "COMMON"),
                faction_id=faction.id if faction else None,
                faction_role_fa=entry.get("faction_role_fa"),
                affiliations_fa=entry.get("affiliations_fa"),
                release_status=entry.get("release_status", ReleaseStatus.DRAFT),
            )
            character.shop_enabled = bool(entry.get("shop_enabled", True))
            existing_codes.add(code)
            created += 1
        except CharacterValidationError as exc:
            failures.append((code, str(exc)))
            logger.error(f"Invalid seed character {code}: {exc}")

    if failures:
        # Loud: the caller (init_db / bot startup) must not continue with a
        # partially applied roster, and the transaction rolls back so the
        # next start retries from a clean state.
        logger.error(
            "Character seed FAILED for: "
            + ", ".join(code for code, _ in failures)
            + " - fix data/characters_seed.json and restart."
        )
        raise CharacterSeedError(failures)

    if created:
        logger.info(f"Seeded {created} characters from data layer.")
    if updated:
        logger.info(f"Synced official content for {updated} character(s).")
    return created


async def init_db(engine: AsyncEngine | None = None) -> None:
    """Initialize database tables and seed essential default records."""
    eng = engine or get_async_engine()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database tables verified.")

    session_factory = get_async_sessionmaker(eng)
    async with session_factory() as session:
        await seed_default_data(session)
        await seed_characters(session)
        from sib.services.shop_chests import ShopChestService

        await ShopChestService.seed_tiers(session)
        await session.commit()

    logger.info("Default seed data initialized.")
