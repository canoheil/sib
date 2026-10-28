"""Antidote crafting and use; the caller owns each economy transaction."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import UserDisease
from sib.services import disease_service
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.items import ItemService
from sib.services.lock_order import lock_user_row

router = Router(name="disease")

PLANT = "disease_plant"
ANTIDOTE = "disease_antidote"
BUFF = "sib_buff"

CRAFT_SUCCESS = "✅ پادزهر آماده ساخته شد!"
MISSING_PLANT = "🌿 گیاه ویژه کافی نداری!"
MISSING_BUFF = "💥 سیب باف کافی نداری!"
MISSING_APPLES = "🍎 سیب کافی برای ساخت پادزهر نداری!"
NOT_SICK = "تو بیمار نیستی!"
MISSING_ANTIDOTE = "💊 پادزهر آماده نداری!"
USE_SUCCESS = "✅ درمان شدی! پادزهر مصرف شد."
UNKNOWN_USER = "ابتدا با /start ثبت‌نام کن."
GENERIC_ERROR = "خطایی رخ داد. لطفاً دوباره تلاش کن."


class DiseaseItemError(ValueError):
    """A safe, user-facing refusal made before any resources are changed."""


async def _craft_cost(session: AsyncSession) -> int:
    key = "disease_craft_apple_cost"
    try:
        cost = await ConfigService.get_int(session, key)
    except (TypeError, ValueError, OverflowError):
        cost = int(ConfigService.default(key))
    return max(0, cost)


async def craft_antidote(session: AsyncSession, user_id: int) -> None:
    """Trade one plant, one buff and the configured apples for one antidote.

    Lock the user first (including absent inventory rows), inventory rows in
    key order, then the wallet. All checks precede all writes; exceptions
    after a write must escape the caller's transaction so it rolls back.
    """
    try:
        await lock_user_row(session, user_id)
    except ValueError as exc:
        raise DiseaseItemError(UNKNOWN_USER) from exc

    owned = {}
    for key in sorted((ANTIDOTE, PLANT, BUFF)):
        owned[key] = await ItemService.get_total(session, user_id, key, for_update=True)
    if owned[PLANT] < 1:
        raise DiseaseItemError(MISSING_PLANT)
    if owned[BUFF] < 1:
        raise DiseaseItemError(MISSING_BUFF)

    cost = await _craft_cost(session)
    balance = await AppleWalletService.get_balance(session, user_id, for_update=True)
    if balance < cost:
        raise DiseaseItemError(MISSING_APPLES)

    await ItemService.consume(session, user_id, PLANT, 1)
    await ItemService.consume(session, user_id, BUFF, 1)
    await AppleWalletService.spend(
        session, user_id, cost, reason="disease_antidote_craft", ref_type="user_item"
    )
    await ItemService.grant(session, user_id, ANTIDOTE, 1)


async def use_antidote(session: AsyncSession, user_id: int) -> None:
    """Consume exactly one antidote only if the locked disease state is sick."""
    try:
        await lock_user_row(session, user_id)
    except ValueError as exc:
        # A player with no account cannot have a disease row either.
        raise DiseaseItemError(NOT_SICK) from exc
    disease = await session.scalar(
        select(UserDisease)
        .where(UserDisease.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if disease is None or disease.infection_level <= 0:
        raise DiseaseItemError(NOT_SICK)
    if await ItemService.get_total(session, user_id, ANTIDOTE, for_update=True) < 1:
        raise DiseaseItemError(MISSING_ANTIDOTE)

    await ItemService.consume(session, user_id, ANTIDOTE, 1)
    await disease_service.cure_user(session, user_id)


@router.message(Command("craft_antidote"))
async def handle_craft_antidote(message: Message) -> None:
    if message.from_user is None:
        return
    try:
        async with get_session() as session:
            await craft_antidote(session, message.from_user.id)
    except DiseaseItemError as exc:
        await message.reply(str(exc))
        return
    except Exception:
        logger.exception("antidote crafting failed (user=%s)", message.from_user.id)
        await message.reply(GENERIC_ERROR)
        return
    await message.reply(CRAFT_SUCCESS)


@router.message(Command("use_antidote"))
async def handle_use_antidote(message: Message) -> None:
    if message.from_user is None:
        return
    try:
        async with get_session() as session:
            await use_antidote(session, message.from_user.id)
    except DiseaseItemError as exc:
        await message.reply(str(exc))
        return
    except Exception:
        logger.exception("antidote use failed (user=%s)", message.from_user.id)
        await message.reply(GENERIC_ERROR)
        return
    await message.reply(USE_SUCCESS)
