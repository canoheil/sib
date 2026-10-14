"""Best-effort Telegram adapter. All sends/edits are circuit-breaker metered."""
import asyncio
import hashlib
import random
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Character, Inv2Attack, Inv2Invasion, Inv2Region, Inv2Reward, PlayerCard, User
from sib.services.character_art import artwork_path
from sib.services.config_store import ConfigService
from sib.services.sib_claim import fa_digits
from . import narrative
from .circuit_breaker import is_group_available, record_send_failure, record_send_success
from .enums import InvasionStatus
from .imaging.cache import cached_image
from .imaging import compose_announcement_image, compose_join_image, compose_result_image
from .imaging.compose import load_card_image_bytes


async def _breaker_failure(group_id) -> None:
    try:
        if group_id is not None:
            await record_send_failure(group_id)
    except Exception:
        logger.exception("inv2 circuit breaker failure recording failed")


async def _breaker_success(group_id) -> None:
    try:
        if group_id is not None:
            await record_send_success(group_id)
    except Exception:
        logger.exception("inv2 circuit breaker success recording failed")


async def _telegram(group_id, method, **kwargs):
    """One metered Telegram operation; never raise into gameplay/maintenance."""
    try:
        result = await method(**kwargs)
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.debug("inv2 Telegram send rejected group=%s", group_id, exc_info=True)
        await _breaker_failure(group_id)
        return None
    except Exception:
        logger.exception("inv2 Telegram send failed group=%s", group_id)
        return None
    await _breaker_success(group_id)
    return result


def _is_not_modified(error: TelegramBadRequest) -> bool:
    """Telegram refuses edits whose caption is byte-identical."""
    return "not modified" in (error.message or "").lower()


def _caption(text: str) -> str:
    return text[:1020]  # leave room below Telegram's 1024-character limit


def _fit_caption(narrative_text: str, suffix: str, limit: int = 1024) -> str:
    """Narrative + status line; if over the limit, truncate the narrative at
    a word boundary and append "…" before the status line."""
    if len(narrative_text) + len(suffix) <= limit:
        return narrative_text + suffix
    budget = max(0, limit - len(suffix) - 1)
    cut = narrative_text[:budget].rsplit(" ", 1)[0].rstrip()
    return f"{cut}…{suffix}"


def announcement_hp_line(current_hp: int, total_hp: int) -> str:
    """Live enemy HP block appended after the announcement narrative."""
    return f"\n\n⚔️ سلامتی شیاسیب: {fa_digits(max(0, current_hp))} / {fa_digits(total_hp)}"


def build_announcement_caption(narrative_text: str, current_hp: int, total_hp: int) -> str:
    """Announcement caption: narrative, blank line, then the live HP line."""
    return _fit_caption(narrative_text, announcement_hp_line(current_hp, total_hp))


def build_join_caption(narrative_text: str, damage: int, *, is_crit: bool, is_kill: bool) -> str:
    """Join caption: narrative, blank line, then the explicit damage line.

    The emoji marks the blow: 🔥 normal, 💥 critical, 🏆 killing blow
    (the kill marker wins when a critical lands the finishing hit).
    """
    emoji = "🏆" if is_kill else "💥" if is_crit else "🔥"
    return _fit_caption(narrative_text, f"\n\n{emoji} اسیب وارده به دشمن: {fa_digits(damage)}")


async def _edit_announcement_caption(bot, group_id, chat_id, message_id, caption) -> None:
    """One metered announcement caption edit.

    "Message is not modified" is an expected no-op when joins coalesce into an
    identical caption; it is swallowed at DEBUG and is NOT a breaker failure.
    Any other failure logs WARNING and records exactly one breaker failure.
    Never raises into the join flow.
    """
    try:
        await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption)
    except TelegramBadRequest as exc:
        if _is_not_modified(exc):
            logger.debug("inv2 HP edit unchanged, skipping group=%s message=%s", group_id, message_id)
            return
        logger.warning("inv2 HP edit rejected group=%s message=%s: %s", group_id, message_id, exc.message)
        await _breaker_failure(group_id)
        return
    except Exception:
        logger.warning("inv2 HP edit failed group=%s message=%s", group_id, message_id, exc_info=True)
        await _breaker_failure(group_id)
        return
    await _breaker_success(group_id)


async def _apply_hp_edit(bot, invasion_id: int, *, allow_victory: bool = False) -> None:
    """Rebuild the announcement caption from the latest committed HP and edit once.

    Only a live (ACTIVE) battle gets a debounced HP edit: once the battle is
    terminal, the finalize step owns the caption. ``allow_victory`` is used by
    the synchronous killing-blow edit, which must show 0 / total HP before
    finalize rewrites the caption with the victory narrative.
    """
    if bot is None:
        return
    try:
        async with get_session() as session:
            inv = await session.get(Inv2Invasion, invasion_id)
            if inv is None or inv.message_id is None:
                return
            if inv.status == InvasionStatus.PENDING.value:
                return  # announcement was never sent
            if inv.status == InvasionStatus.DEFEAT.value:
                return  # defeat narrative owns the caption
            if inv.status == InvasionStatus.VICTORY.value and not allow_victory:
                return  # victory narrative owns the caption (no edit after finalize)
            narrative_text = inv.announcement_narrative
            if narrative_text is None:
                # Legacy row announced before the narrative column existed:
                # best-effort re-render (pool history may have advanced).
                region = await session.get(Inv2Region, inv.region_code)
                narrative_text = narrative.render_announcement(
                    username="نگهبانان", card_name="کارت‌های اصلی",
                    region_name=region.name_fa if region else "نامشخص",
                    enemy_name=inv.enemy_name_fa, rng=random.Random(inv.id), invasion_id=inv.id)
            caption = build_announcement_caption(narrative_text, inv.enemy_hp_remaining, inv.enemy_hp)
            group_id = inv.group_id
            chat_id = inv.message_chat_id or group_id
            message_id = inv.message_id
    except Exception:
        logger.exception("inv2 HP edit state read failed invasion=%s", invasion_id)
        return
    await _edit_announcement_caption(bot, group_id, chat_id, message_id, caption)


# Per-invasion coalescing of announcement HP edits: rapid joins within the
# debounce window share a single delayed edit that reads the latest HP.
_pending_edits: dict[int, asyncio.Task] = {}
HP_EDIT_DEBOUNCE_SECONDS = 1.5


def schedule_hp_edit(bot, invasion_id: int) -> None:
    """Schedule one coalesced HP edit per invasion (no-op if one is pending)."""
    existing = _pending_edits.get(invasion_id)
    if existing is not None and not existing.done():
        return
    _pending_edits[invasion_id] = asyncio.create_task(_debounced_hp_edit(bot, invasion_id))


async def _debounced_hp_edit(bot, invasion_id: int) -> None:
    try:
        await asyncio.sleep(HP_EDIT_DEBOUNCE_SECONDS)
        await _apply_hp_edit(bot, invasion_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("inv2 debounced HP edit failed invasion=%s", invasion_id)
    finally:
        _pending_edits.pop(invasion_id, None)


async def cancel_pending_hp_edits() -> None:
    """Cancel in-flight debounced HP edits; called from the shutdown hook."""
    tasks = list(_pending_edits.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _pending_edits.clear()


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
                image_args = (inv.enemy_type, region.name_fa, inv.enemy_name_fa)
                path = await asyncio.to_thread(cached_image,
                    ("compose-v3", "announcement", inv.id, *image_args),
                    lambda: compose_announcement_image(*image_args))
                narrative_text = narrative.render_announcement(
                    username="نگهبانان", card_name="کارت‌های اصلی", region_name=region.name_fa,
                    enemy_name=inv.enemy_name_fa, rng=random.Random(inv.id), invasion_id=inv.id)
                caption = build_announcement_caption(narrative_text, inv.enemy_hp_remaining, inv.enemy_hp)
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
                inv.announcement_narrative = narrative_text
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
                card_bytes = await asyncio.to_thread(load_card_image_bytes, local_art)
                # Freeze display inputs before handing them to the render worker.
                # HP is the persisted snapshot read for presentation, not recalculated damage.
                image_args = (inv.enemy_type, card_bytes, user.display_name, attack.damage,
                              bool(attack.is_crit), inv.enemy_hp_remaining)
                path = await asyncio.to_thread(cached_image,
                    ("compose-v3", "join", inv.id, user_id, attack_id, image_args[0],
                     hashlib.sha256(card_bytes).hexdigest(), *image_args[2:]),
                    lambda: compose_join_image(*image_args))
                # Post-attack HP is already committed: 0 means this blow was fatal.
                caption = build_join_caption(attack.narrative, int(attack.damage),
                                             is_crit=bool(attack.is_crit),
                                             is_kill=inv.enemy_hp_remaining <= 0)
                await _telegram(inv.group_id, bot.send_photo, chat_id=inv.group_id,
                    photo=FSInputFile(path), caption=caption,
                    reply_to_message_id=inv.message_id, parse_mode=None)
                # Live HP view: refresh the announcement caption. The attack
                # is committed, so this never blocks or reopens the join flow.
                if inv.message_id is None:
                    return
                if inv.enemy_hp_remaining <= 0:
                    # Killing blow: show 0 / total HP immediately, before the
                    # victory flow rewrites the caption.
                    await _apply_hp_edit(bot, inv.id, allow_victory=True)
                else:
                    schedule_hp_edit(bot, inv.id)
        except Exception:
            logger.exception("inv2 join presentation failed invasion=%s user=%s", invasion_id, user_id)

    async def finalize(self, bot, invasion_id: int) -> bool:
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
                narrative_context = {"invasion_id": inv.id}
                if inv.status == InvasionStatus.VICTORY.value:
                    # Count stored defenders once; rewards can contain multiple rows per user.
                    narrative_context["participant_count"] = await session.scalar(
                        select(func.count(Inv2Attack.id)).where(Inv2Attack.invasion_id == inv.id))
                final = _caption(render(username="نگهبانان", card_name="کارت‌های اصلی",
                    region_name=region.name_fa, enemy_name=inv.enemy_name_fa, rng=random.Random(inv.id),
                    **narrative_context))
                # Telegram caption edit and photo send are separate metered calls.
                await _telegram(inv.group_id, bot.edit_message_caption,
                    chat_id=inv.message_chat_id or inv.group_id, message_id=inv.message_id,
                    caption=final, reply_markup=None, parse_mode=None)
                caption = final
                reward_lines = ()
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
                        reward_lines = tuple(f"{name} ×{totals[key]}" for key, name in labels if totals[key])
                        summary = "، ".join(reward_lines)
                        caption += "\n🎁 پاداش مدافعان: " + summary
                image_args = (inv.status.lower(), region.name_fa, inv.enemy_type, reward_lines)
                path = await asyncio.to_thread(cached_image,
                    ("compose-v3", "result", inv.id, *image_args),
                    lambda: compose_result_image(*image_args))
                sent = await _telegram(inv.group_id, bot.send_photo, chat_id=inv.group_id,
                    photo=FSInputFile(path), caption=_caption(caption), parse_mode=None)
                if sent is None:
                    return False
                if inv.status == InvasionStatus.VICTORY.value and inv.delivered_at is None:
                    # The photo was accepted; a failed DB commit leaves NULL so
                    # startup recovery will retry. Telegram and DB cannot be atomic.
                    inv.delivered_at = datetime.now(timezone.utc)
                return True
        except Exception:
            logger.exception("inv2 final presentation failed id=%s", invasion_id)
            return False

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
