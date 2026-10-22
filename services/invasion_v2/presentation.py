"""Best-effort Telegram adapter. All sends/edits are circuit-breaker metered."""
import asyncio
import hashlib
import random
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile, InlineKeyboardMarkup
from sqlalchemy import func, select

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import (
    Character, Inv2Attack, Inv2Invasion, Inv2RandomEvent, Inv2Region, Inv2Reward,
    PlayerCard, User,
)
from sib.keyboards.common import success_button
from sib.services.character_art import artwork_path
from sib.services.config_store import ConfigService, DEFAULT_INVASION_JOIN_WINDOW_SECONDS
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
    except TelegramBadRequest as exc:
        if kwargs.get("reply_to_message_id") is not None and _is_missing_reply(exc):
            logger.debug("inv2 reply target gone, retrying standalone group=%s", group_id)
            return await _telegram(group_id, method, **{**kwargs, "reply_to_message_id": None})
        logger.debug("inv2 Telegram send rejected group=%s", group_id, exc_info=True)
        await _breaker_failure(group_id)
        return None
    except TelegramForbiddenError:
        logger.debug("inv2 Telegram send rejected group=%s", group_id, exc_info=True)
        await _breaker_failure(group_id)
        return None
    except Exception:
        logger.exception("inv2 Telegram send failed group=%s", group_id)
        return None
    await _breaker_success(group_id)
    return result


async def _unmetered(bot, method_name: str, group_id, **kwargs) -> bool:
    """One cosmetic Telegram call: never raises, never meters the breaker.

    Pinning decorates a delivery that already succeeded. A group whose bot is
    not an admin with "pin messages" must keep receiving invasions, so a
    rejection here must not count toward the send-failure circuit breaker
    (three strikes mute a group for 24h) and a success must not reset it.
    Bots without the method at all (previews, test doubles) are a no-op.
    """
    method = getattr(bot, method_name, None)
    if method is None:
        logger.debug("inv2 %s unsupported by this bot; skipped group=%s", method_name, group_id)
        return False
    try:
        await method(**kwargs)
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning("inv2 %s rejected group=%s; bot needs admin rights to pin messages",
                       method_name, group_id)
        return False
    except Exception:
        logger.exception("inv2 %s failed group=%s", method_name, group_id)
        return False
    return True


async def pin_announcement(bot, invasion_id: int) -> None:
    """Pin the sent announcement, then unpin the group's previous one.

    Called after the announcement transaction has committed, so a pin failure
    can never roll back message_id/ACTIVE and make the next tick announce the
    same invasion twice. The new pin is placed first: if Telegram refuses it
    the old announcement stays pinned rather than leaving the group with
    nothing. pinned_at is written only for pins Telegram accepted, and the
    previous row is cleared only once its unpin succeeded, so a transient
    failure is simply retried by the next invasion.
    """
    try:
        async with get_session() as session:
            inv = await session.get(Inv2Invasion, invasion_id)
            if inv is None or inv.message_id is None or inv.pinned_at is not None:
                return
            group_id, message_id = inv.group_id, inv.message_id
            chat_id = inv.message_chat_id or group_id
            previous = list((await session.execute(
                select(Inv2Invasion.id, Inv2Invasion.group_id,
                       Inv2Invasion.message_chat_id, Inv2Invasion.message_id)
                .where(Inv2Invasion.group_id == group_id,
                       Inv2Invasion.id != invasion_id,
                       Inv2Invasion.pinned_at.is_not(None))
                .order_by(Inv2Invasion.id))).all())
        # The pin notification is the point of the feature: never silent.
        if not await _unmetered(bot, "pin_chat_message", group_id, chat_id=chat_id,
                                message_id=message_id, disable_notification=False):
            return
        unpinned = [prev_id for prev_id, prev_group, prev_chat, prev_message in previous
                    if prev_message is None or await _unmetered(
                        bot, "unpin_chat_message", prev_group,
                        chat_id=prev_chat or prev_group, message_id=prev_message)]
        async with get_session() as session:
            inv = await session.get(Inv2Invasion, invasion_id)
            if inv is not None and inv.pinned_at is None:
                inv.pinned_at = datetime.now(timezone.utc)
            for prev_id in unpinned:
                stale = await session.get(Inv2Invasion, prev_id)
                if stale is not None:
                    stale.pinned_at = None
    except Exception:
        logger.exception("inv2 announcement pin failed invasion=%s", invasion_id)


def _is_not_modified(error: TelegramBadRequest) -> bool:
    """Telegram refuses edits whose caption is byte-identical."""
    return "not modified" in (error.message or "").lower()


def _is_missing_reply(error: TelegramBadRequest) -> bool:
    """Telegram refuses a reply when an admin deleted the target message."""
    msg = (error.message or "").lower()
    return "message to be replied not found" in msg or "reply message not found" in msg


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
    return f"\n\n⚔️ جان شیاسیب: **{fa_digits(max(0, current_hp))}** / **{fa_digits(total_hp)}**"


def build_announcement_caption(narrative_text: str, current_hp: int, total_hp: int) -> str:
    """Announcement caption: narrative, blank line, then the live HP line."""
    return _fit_caption(narrative_text, announcement_hp_line(current_hp, total_hp))


def build_join_caption(narrative_text: str, damage: int, *, is_crit: bool, is_kill: bool) -> str:
    """Join caption: narrative, blank line, then the explicit damage line.

    The emoji marks the blow: 🔥 normal, 💥 critical, 🏆 killing blow
    (the kill marker wins when a critical lands the finishing hit).
    """
    emoji = "🏆" if is_kill else "💥" if is_crit else "🔥"
    return _fit_caption(narrative_text, f"\n\n{emoji} آسیب وارده به دشمن: **{fa_digits(damage)}**")


def join_keyboard(invasion_id: int) -> InlineKeyboardMarkup:
    """Both join modes; the one source of truth for their labels/callbacks.

    Telegram clears an inline keyboard whenever an edit omits reply_markup, so
    every caption edit of a still-joinable announcement must re-send it.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [success_button(text="⚔️ پیوستن (عادی)", callback_data=f"inv2:join:{invasion_id}")],
        [success_button(text="💥 پیوستن با سیب باف", callback_data=f"inv2:joinbuff:{invasion_id}")],
    ])


async def _edit_announcement_caption(
    bot, group_id, chat_id, message_id, caption, reply_markup=None
) -> None:
    """One metered announcement caption edit.

    reply_markup is re-sent for a live battle and deliberately omitted (None)
    for a finished one, where omitting it is how Telegram removes the button.

    "Message is not modified" is an expected no-op when joins coalesce into an
    identical caption; it is swallowed at DEBUG and is NOT a breaker failure.
    Any other failure logs WARNING and records exactly one breaker failure.
    Never raises into the join flow.
    """
    try:
        await bot.edit_message_caption(chat_id=chat_id, message_id=message_id,
                                       caption=caption, reply_markup=reply_markup)
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
            # A live edit must re-send the button (Telegram drops the keyboard of
            # an edit that omits reply_markup). Starting at 0 HP the battle is
            # over, so the kill edit deliberately omits it and clears the button.
            reply_markup = (join_keyboard(inv.id)
                            if inv.status == InvasionStatus.ACTIVE.value else None)
    except Exception:
        logger.exception("inv2 HP edit state read failed invasion=%s", invasion_id)
        return
    await _edit_announcement_caption(bot, group_id, chat_id, message_id, caption,
                                     reply_markup=reply_markup)


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


# G2 garden-corruption notice (private DM, sent post-commit). Exact product
# wording; kept here next to the other Invasion v2 Telegram delivery code.
WITHER_NOTIFICATION_TEXT = (
    "🥀 چون توی یورش شرکت نکردی، بخشی از باغت پژمرده شد! "
    "برای ترمیمش **کود الهی** بخر."
)

# G3 — group notification for withered gardens (sent post-commit, after the terminal message).
WITHER_GROUP_HEADER = "⚠️ باغ این بازیکن‌ها به‌خاطر شرکت نکردن در یورش پژمرده شد:"
WITHER_GROUP_FOOTER = "برای ترمیم زمین‌ها، **کود الهی** بخرید."


async def announce_withered_in_group(bot, results) -> None:
    """Send a follow-up group message listing withered defenders (G3).

    Runs AFTER the terminal invasion message has been delivered to the group.
    Groups results by invasion (one invasion per maintenance tick, but the
    helper is safe for batched calls) and sends one message per group.  Uses
    ``plain_display_name`` so no @handles or IDs are exposed.  No-op when
    ``results`` is empty or ``bot`` is None.  Never raises into the
    maintenance cycle.
    """
    if not results or bot is None:
        return
    try:
        from collections import defaultdict

        from sib.core.text import plain_display_name

        # Group by invasion_id — one invasion per tick in practice, but safe for batches.
        by_invasion: dict[int, list] = defaultdict(list)
        for r in results:
            by_invasion[getattr(r, "invasion_id", None)].append(r)

        for invasion_id, group_results in by_invasion.items():
            if invasion_id is None:
                continue
            async with get_session() as session:
                invasion = await session.get(Inv2Invasion, invasion_id)
                if invasion is None:
                    continue
                group_id = invasion.group_id
                # Collect user_ids in encounter order; de-duplicate preserves first occurrence.
                seen = set()
                ordered_ids: list[int] = []
                for r in group_results:
                    uid = getattr(r, "user_id", None)
                    if uid is None or uid in seen:
                        continue
                    seen.add(uid)
                    ordered_ids.append(uid)
                if not ordered_ids:
                    continue
                # Load users for display names.
                users_by_id: dict[int, User] = {}
                if ordered_ids:
                    rows = await session.scalars(select(User).where(User.id.in_(ordered_ids)))
                    for u in rows.all():
                        users_by_id[u.id] = u
                names: list[str] = []
                for uid in ordered_ids:
                    u = users_by_id.get(uid)
                    raw = (u.first_name if u and getattr(u, "first_name", None) else None) or "مدافع"
                    names.append(plain_display_name(raw, "مدافع"))
                if not names:
                    continue
                text = f"{WITHER_GROUP_HEADER}\n**{'، '.join(names)}**\n{WITHER_GROUP_FOOTER}"
                await _telegram(group_id, bot.send_message, chat_id=group_id, text=text, parse_mode=None)
    except Exception:
        logger.exception("inv2 wither group announcement failed")


async def deliver_wither_notifications(bot, results) -> None:
    """Best-effort private DMs for garden corruption, run AFTER commit.

    The withering itself is already durable; a blocked or never-started
    private chat is logged and skipped — the 🥀 state remains visible in the
    player's garden panel. This deliberately bypasses the group circuit
    breaker because these are one-to-one user chats, not group sends.
    """
    if bot is None or not results:
        return
    for result in results:
        try:
            await bot.send_message(
                chat_id=result.user_id,
                text=WITHER_NOTIFICATION_TEXT,
                parse_mode=None,
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.debug(
                "inv2 wither DM rejected user=%s", result.user_id, exc_info=True
            )
        except Exception:
            logger.exception("inv2 wither DM failed user=%s", result.user_id)


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
                    ("compose-v4", "announcement", inv.id, *image_args),
                    lambda: compose_announcement_image(*image_args))
                narrative_text = narrative.render_announcement(
                    username="نگهبانان", card_name="کارت‌های اصلی", region_name=region.name_fa,
                    enemy_name=inv.enemy_name_fa, rng=random.Random(inv.id), invasion_id=inv.id)
                caption = build_announcement_caption(narrative_text, inv.enemy_hp_remaining, inv.enemy_hp)
                kb = join_keyboard(inv.id)
                raw_window = await ConfigService.get(session, "invasion_join_window_seconds")
                window = int(DEFAULT_INVASION_JOIN_WINDOW_SECONDS if raw_window is None else raw_window)
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
            # Outside the transaction on purpose: pinning is best-effort and
            # must never undo a committed, already-delivered announcement.
            await pin_announcement(bot, invasion_id)
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
                    ("compose-v4", "join", inv.id, user_id, attack_id, image_args[0],
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
                narrative_text = _caption(render(username="نگهبانان", card_name="کارت‌های نگهبانان",
                    region_name=region.name_fa, enemy_name=inv.enemy_name_fa, rng=random.Random(inv.id),
                    **narrative_context))
                # Telegram caption edit and photo send are separate metered calls.
                await _telegram(inv.group_id, bot.edit_message_caption,
                    chat_id=inv.message_chat_id or inv.group_id, message_id=inv.message_id,
                    caption=narrative_text, reply_markup=None, parse_mode=None)
                caption = narrative_text
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
                                  ("obsidian_apple", "سیب ابسیدینی"),
                                  ("seedling", "نهال"))
                        reward_lines = tuple(f"{name} ×{fa_digits(totals[key])}"
                                             for key, name in labels if totals[key])
                        summary = "، ".join(reward_lines)
                        # Blank line before the reward block, matching the HP and
                        # damage lines. Fitting the narrative instead of appending
                        # keeps the reward line itself from being cut by Telegram.
                        caption = _fit_caption(narrative_text,
                                               "\n\n🎁 پاداش مدافعان: **" + summary + "**", limit=1020)
                image_args = (inv.status.lower(), region.name_fa, inv.enemy_type, reward_lines)
                path = await asyncio.to_thread(cached_image,
                    ("compose-v4", "result", inv.id, *image_args),
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


# ---------------------------------------------------------------------------
# IV3 random events
# ---------------------------------------------------------------------------
# Every event is a photo with a short caption; a chest carries one public
# button. Delivery is post-commit and never raises into the maintenance loop:
# a failed photo degrades to a text message, and a missing image simply skips
# the photo for that event (the battle and the other events are unaffected).
EVENT_CAPTION_LIMIT = 1020
EVENT_SEND_ATTEMPTS = 3


def loot_keyboard(event_id: int) -> InlineKeyboardMarkup:
    """The chest's single public button (success/green, never owner-scoped)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        success_button(text="🎁 باز کردن صندوق", callback_data=f"inv2ev:loot:{event_id}")
    ]])


def build_event_caption(event, *, winners=(), closed: bool = False) -> str:
    """Narrative + effect line (+ chest status) inside the photo caption limit."""
    from . import event_catalog as catalog

    narrative = event.narrative or ""
    template = catalog.BY_CODE.get(event.template_code)
    if template is None:
        return _caption(narrative)
    suffix = "\n\n" + catalog.render_effect_line(
        template,
        amount=event.applied_amount,
        loot_amount=event.loot_amount,
        max_claims=event.loot_max_claims,
    )
    if template.category == catalog.LOOT:
        if closed:
            suffix += "\n🔒 صندوقچه بسته شد."
        else:
            suffix += "\n" + catalog.render_loot_status(
                event.loot_claimed,
                event.loot_max_claims or catalog.DEFAULT_LOOT_MAX_CLAIMS,
                winners,
            )
    return _fit_caption(narrative, suffix, limit=EVENT_CAPTION_LIMIT)


async def _edit_event_caption(
    bot, group_id, chat_id, message_id, caption, reply_markup=None
) -> bool:
    """One metered caption edit; "not modified" is a no-op, never a failure."""
    try:
        await bot.edit_message_caption(
            chat_id=chat_id, message_id=message_id, caption=caption,
            reply_markup=reply_markup, parse_mode=None,
        )
    except TelegramBadRequest as exc:
        if _is_not_modified(exc):
            return True
        logger.warning("inv2 event caption edit rejected group=%s message=%s: %s",
                       group_id, message_id, exc.message)
        await _breaker_failure(group_id)
        return False
    except Exception:
        logger.warning("inv2 event caption edit failed group=%s message=%s",
                       group_id, message_id, exc_info=True)
        await _breaker_failure(group_id)
        return False
    await _breaker_success(group_id)
    return True


async def publish_event(bot, event_id: int) -> bool:
    """Send one committed ``APPLIED`` event to its group.

    Two short transactions around the Telegram call (read → send → persist):
    no database lock is held across a network request. If another worker wins
    the persist race, the message is still delivered exactly once in practice
    because only the winner writes ``message_id`` and the loser's row already
    has one.
    """
    if bot is None:
        return False
    try:
        from sib.services.maintenance.service import MaintenanceService
        from . import event_catalog as catalog
        from .imaging.templates import event_image_path

        if not await MaintenanceService._invasion_enabled():
            return False

        async with get_session() as session:
            event = await session.get(Inv2RandomEvent, event_id)
            if event is None or event.status != "APPLIED" or event.message_id is not None:
                return False
            invasion = await session.get(Inv2Invasion, event.invasion_id)
            if invasion is None or invasion.status != InvasionStatus.ACTIVE.value:
                event.status = "SKIPPED"
                event.resolved_at = datetime.now(timezone.utc)
                return False
            if not await is_group_available(event.group_id, session=session):
                event.status = "SKIPPED"
                event.resolved_at = datetime.now(timezone.utc)
                return False
            template = catalog.BY_CODE.get(event.template_code)
            caption = build_event_caption(event)
            keyboard = (loot_keyboard(event.id)
                        if template is not None and template.category == catalog.LOOT
                        else None)
            group_id = event.group_id
            chat_id = event.message_chat_id or group_id
            reply_to_message_id = invasion.message_id
            image_path = event_image_path(event.template_code)

        message = None
        if image_path is not None:
            message = await _telegram(
                group_id, bot.send_photo, chat_id=group_id,
                photo=FSInputFile(image_path), caption=_caption(caption),
                reply_markup=keyboard, reply_to_message_id=reply_to_message_id,
                parse_mode=None,
            )
        if message is None:
            # Missing art or a rejected photo: the event still reaches the
            # group as text. This is the documented graceful fallback.
            message = await _telegram(
                group_id, bot.send_message, chat_id=group_id, text=caption,
                reply_markup=keyboard, reply_to_message_id=reply_to_message_id,
                parse_mode=None,
            )
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            event = await session.scalar(
                select(Inv2RandomEvent).where(Inv2RandomEvent.id == event_id).with_for_update()
            )
            if event is None:
                return message is not None
            if message is None:
                event.send_attempts = int(event.send_attempts or 0) + 1
                if event.send_attempts >= EVENT_SEND_ATTEMPTS:
                    event.status = "SKIPPED"
                    event.resolved_at = now
                logger.warning("inv2 event delivery failed id=%s attempt=%s",
                               event_id, event.send_attempts)
                return False
            if event.status != "APPLIED" or event.message_id is not None:
                return True  # another worker delivered it first
            event.message_chat_id = getattr(getattr(message, "chat", None), "id", None) or group_id
            event.message_id = getattr(message, "message_id", None)
            event.announced_at = now
            if event.duration_seconds:
                event.starts_at = now
                event.ends_at = now + timedelta(seconds=int(event.duration_seconds))
                event.status = "ACTIVE"
            else:
                event.status = "CLOSED"   # one-shot: shown once, no window
                event.resolved_at = now
            invasion_id = event.invasion_id
            moved_hp = event.applied_amount is not None
        if moved_hp:
            # The pinned announcement shows live HP; an event that healed or
            # damaged the boss must refresh it instead of waiting for a join.
            schedule_hp_edit(bot, invasion_id)
        return True
    except Exception:
        logger.exception("inv2 event presentation failed id=%s", event_id)
        return False


async def publish_applied_events(bot) -> int:
    """Deliver every committed event that is waiting for its message."""
    if bot is None:
        return 0
    try:
        from sib.services.maintenance.service import MaintenanceService
        if not await MaintenanceService._invasion_enabled():
            return 0
        async with get_session() as session:
            ids = list((await session.scalars(
                select(Inv2RandomEvent.id).where(
                    Inv2RandomEvent.status == "APPLIED",
                    Inv2RandomEvent.message_id.is_(None),
                ).order_by(Inv2RandomEvent.id).limit(20)
            )).all())
        sent = 0
        for event_id in ids:
            if await publish_event(bot, event_id):
                sent += 1
        return sent
    except Exception:
        logger.exception("inv2 event delivery pass failed")
        return 0


async def refresh_loot_caption(bot, event_id: int) -> None:
    """Rebuild a chest caption after a claim (winners and remaining slots)."""
    if bot is None:
        return
    try:
        from .event_scheduler import winners_for_event
        from . import event_catalog as catalog
        async with get_session() as session:
            event = await session.get(Inv2RandomEvent, event_id)
            if event is None or event.message_id is None:
                return
            template = catalog.BY_CODE.get(event.template_code)
            if template is None or template.category != catalog.LOOT:
                return
            winners = await winners_for_event(session, event_id)
            full = int(event.loot_claimed) >= int(
                event.loot_max_claims or catalog.DEFAULT_LOOT_MAX_CLAIMS
            )
            caption = build_event_caption(event, winners=winners[-3:])
            group_id = event.group_id
            chat_id = event.message_chat_id or group_id
            message_id = event.message_id
        await _edit_event_caption(
            bot, group_id, chat_id, message_id, _caption(caption),
            reply_markup=None if full else loot_keyboard(event_id),
        )
    except Exception:
        logger.exception("inv2 chest caption refresh failed id=%s", event_id)


async def close_expired_events(bot) -> int:
    """Remove the chest button once its window (or its battle) is over.

    The durable close happens in the DB-only tick; this pass only finishes the
    Telegram side: rows whose window ended with a delivered message still have
    ``resolved_at`` NULL, which is the "caption not finalised" marker.
    """
    if bot is None:
        return 0
    closed = 0
    try:
        from . import event_catalog as catalog
        if not await _invasion_flag():
            return 0
        async with get_session() as session:
            events = list((await session.scalars(
                select(Inv2RandomEvent).where(
                    Inv2RandomEvent.status == "CLOSED",
                    Inv2RandomEvent.resolved_at.is_(None),
                    Inv2RandomEvent.message_id.is_not(None),
                ).order_by(Inv2RandomEvent.id).limit(20)
            )).all())
            pending = [
                (event.id, event.group_id, event.message_chat_id, event.message_id,
                 build_event_caption(event, closed=True), event.template_code,
                 event.ends_at)
                for event in events
            ]
        for event_id, group_id, chat_id, message_id, caption, code, ends_at in pending:
            template = catalog.BY_CODE.get(code)
            if template is not None and template.category == catalog.LOOT:
                await _edit_event_caption(
                    bot, group_id, chat_id or group_id, message_id,
                    _caption(caption), reply_markup=None,
                )
            async with get_session() as session:
                event = await session.get(Inv2RandomEvent, event_id)
                if event is not None and event.resolved_at is None:
                    event.resolved_at = datetime.now(timezone.utc)
            closed += 1
    except Exception:
        logger.exception("inv2 event closing pass failed")
    return closed


async def _invasion_flag() -> bool:
    from sib.services.maintenance.service import MaintenanceService
    return await MaintenanceService._invasion_enabled()
