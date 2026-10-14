"""Best-effort Telegram adapter. All sends/edits are circuit-breaker metered."""
import asyncio
import random
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Character, Inv2Attack, Inv2Invasion, Inv2Region, Inv2Reward, PlayerCard, User
from sib.services.character_art import artwork_path
from sib.services.config_store import ConfigService
from . import narrative
from .circuit_breaker import is_group_available, record_send_failure, record_send_success
from .enums import InvasionStatus
from .imaging.cache import cached_image
from .imaging.compose import announcement_image, join_image, result_image


async def _telegram(group_id, method, **kwargs):
    """One metered Telegram operation; never raise into gameplay/maintenance."""
    try:
        result = await method(**kwargs)
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.debug("inv2 Telegram send rejected group=%s", group_id, exc_info=True)
        try:
            if group_id is not None:
                await record_send_failure(group_id)
        except Exception:
            logger.exception("inv2 circuit breaker failure recording failed")
        return None
    except Exception:
        logger.exception("inv2 Telegram send failed group=%s", group_id)
        return None
    try:
        if group_id is not None:
            await record_send_success(group_id)
    except Exception:
        logger.exception("inv2 circuit breaker success recording failed")
    return result


def _caption(text: str) -> str:
    return text[:1020]  # leave room below Telegram's 1024-character limit


class InvasionPresentation:
    async def announce(self, bot, invasion_id: int) -> None:
        if bot is None:
            return
        try:
            from sib.services.maintenance.service import MaintenanceService
            if not await MaintenanceService._invasion_enabled():
                return
            async with get_session() as session:
                inv = await session.scalar(select(Inv2Invasion).where(Inv2Invasion.id == invasion_id).with_for_update())
                if inv is None or inv.status != InvasionStatus.PENDING.value or inv.message_id is not None:
                    return
                region = await session.get(Inv2Region, inv.region_code)
                if region is None:
                    return
                group_id = inv.group_id
                if not await is_group_available(group_id, session=session):
                    return
                path = await asyncio.to_thread(cached_image, ("announcement", inv.id, inv.enemy_type),
                    lambda: announcement_image(inv.enemy_type, region.name_fa, region.controlling_faction))
                caption = _caption(narrative.render_announcement(
                    username="نگهبانان", card_name="کارت‌های اصلی", region_name=region.name_fa,
                    enemy_name=inv.enemy_name_fa, rng=random.Random(inv.id)))
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="⚔️ پیوستن به نبرد", callback_data=f"inv2:join:{inv.id}")]])
                raw_window = await ConfigService.get(session, "invasion_join_window_seconds")
                window = int(300 if raw_window is None else raw_window)
                if window <= 0:
                    raise ValueError("Invalid join window")
                message = await _telegram(group_id, bot.send_photo, chat_id=group_id,
                    photo=FSInputFile(path), caption=caption, reply_markup=kb, parse_mode=None)
                if message is None:
                    return
                now = datetime.now(timezone.utc)
                inv.message_chat_id = message.chat.id
                inv.message_id = message.message_id
                inv.announcement_sent_at = now
                inv.started_at = now
                inv.ends_at = now + timedelta(seconds=window)
                inv.status = InvasionStatus.ACTIVE.value
        except Exception:
            logger.exception("inv2 announcement failed id=%s", invasion_id)

    async def on_join(self, bot, invasion_id: int, user_id: int, attack_id: int) -> None:
        if bot is None:
            return
        try:
            from sib.services.maintenance.service import MaintenanceService
            if not await MaintenanceService._invasion_enabled():
                return
            async with get_session() as session:
                inv = await session.get(Inv2Invasion, invasion_id)
                attack = await session.get(Inv2Attack, attack_id)
                if inv is None or attack is None or attack.invasion_id != invasion_id or attack.user_id != user_id:
                    return
                card = await session.get(PlayerCard, attack.card_id)
                character = await session.get(Character, card.character_id) if card else None
                user = await session.get(User, user_id)
                if character is None or user is None:
                    return
                local_art = artwork_path(character)
                if local_art is None:
                    raise FileNotFoundError(f"Missing local card art for character {character.code}")
                path = await asyncio.to_thread(cached_image,
                    (inv.id, user_id, attack.damage, bool(attack.is_crit), inv.enemy_type),
                    lambda: join_image(inv.enemy_type, local_art, user.display_name, attack.damage, attack.is_crit))
                caption = _caption(attack.narrative + ("\n💥 ضربه کاری!" if attack.is_crit else ""))
                await _telegram(inv.group_id, bot.send_photo, chat_id=inv.group_id,
                    photo=FSInputFile(path), caption=caption,
                    reply_to_message_id=inv.message_id, parse_mode=None)
        except Exception:
            logger.exception("inv2 join presentation failed invasion=%s user=%s", invasion_id, user_id)

    async def finalize(self, bot, invasion_id: int) -> None:
        if bot is None:
            return
        try:
            from sib.services.maintenance.service import MaintenanceService
            if not await MaintenanceService._invasion_enabled():
                return
            async with get_session() as session:
                inv = await session.get(Inv2Invasion, invasion_id)
                if inv is None or inv.status not in (InvasionStatus.VICTORY.value, InvasionStatus.DEFEAT.value):
                    return
                region = await session.get(Inv2Region, inv.region_code)
                if region is None or inv.message_id is None:
                    return
                render = narrative.render_victory if inv.status == InvasionStatus.VICTORY.value else narrative.render_defeat
                final = _caption(render(username="نگهبانان", card_name="کارت‌های اصلی",
                    region_name=region.name_fa, enemy_name=inv.enemy_name_fa, rng=random.Random(inv.id)))
                # Telegram caption edit and photo send are separate metered calls.
                await _telegram(inv.group_id, bot.edit_message_caption,
                    chat_id=inv.message_chat_id or inv.group_id, message_id=inv.message_id,
                    caption=final, reply_markup=None, parse_mode=None)
                path = await asyncio.to_thread(cached_image, (inv.id, inv.status),
                    lambda: result_image(inv.enemy_type, inv.status, region.name_fa, region.controlling_faction))
                caption = final
                if inv.status == InvasionStatus.VICTORY.value:
                    rewards = (await session.execute(select(Inv2Reward.reward_type, Inv2Reward.reward_amount)
                        .where(Inv2Reward.invasion_id == inv.id))).all()
                    if rewards:
                        from collections import Counter
                        totals = Counter()
                        for kind, amount in rewards:
                            totals[kind] += amount
                        labels = (("golden_apple", "سیب طلایی"),
                                  ("diamond_apple", "سیب الماسی"),
                                  ("obsidian_apple", "سیب ابسیدینی"))
                        summary = "، ".join(f"{name} ×{totals[key]}" for key, name in labels if totals[key])
                        caption += "\n🎁 پاداش مدافعان: " + summary
                await _telegram(inv.group_id, bot.send_photo, chat_id=inv.group_id,
                    photo=FSInputFile(path), caption=_caption(caption), parse_mode=None)
        except Exception:
            logger.exception("inv2 final presentation failed id=%s", invasion_id)

    async def publish_pending(self, bot) -> None:
        """Present committed PENDING rows. Called only under the feature flag."""
        if bot is None:
            return
        try:
            from sib.services.maintenance.service import MaintenanceService
            if not await MaintenanceService._invasion_enabled():
                return
            async with get_session() as session:
                ids = list((await session.scalars(select(Inv2Invasion.id).where(
                    Inv2Invasion.status == InvasionStatus.PENDING.value,
                    Inv2Invasion.message_id.is_(None)).order_by(Inv2Invasion.id).limit(20))).all())
            for invasion_id in ids:
                await self.announce(bot, invasion_id)
        except Exception:
            logger.exception("inv2 pending announcements failed")
