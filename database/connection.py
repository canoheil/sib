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


async def init_db(engine: AsyncEngine | None = None) -> None:
    """Initialize database tables and seed essential default records."""
    eng = engine or get_async_engine()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database tables verified.")

    session_factory = get_async_sessionmaker(eng)
    async with session_factory() as session:
        await seed_default_data(session)
        await session.commit()

    logger.info("Default seed data initialized.")
