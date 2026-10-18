"""Group-only v2 join: one Main Card, one attack, guarded HP transition."""
import asyncio
import random
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select, update

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import Character, Inv2Attack, Inv2Invasion, PlayerCard, User
from sib.services.card_progression import MAX_CARD_LEVEL, card_attributes
from sib.services.invasion_v2.damage import compute_damage
from sib.services.invasion_v2.enums import InvasionStatus
from sib.services.invasion_v2.narrative import render_join
from sib.services.invasion_v2.presentation import InvasionPresentation, _telegram
from sib.services.maintenance.service import MaintenanceService
from sib.services.items import ItemService
from sib.services.lock_order import lock_user_rows_in_order
from sib.services.player_cards import PlayerCardService

router = Router(name="inv2_join")


async def _answer(callback, text, *, alert=False):
    group_id = getattr(getattr(callback.message, "chat", None), "id", None)
    if group_id is not None:
        await _telegram(group_id, callback.answer, text=text, show_alert=alert)
    else:
        try:
            await callback.answer(text=text, show_alert=alert)
        except Exception:
            logger.debug("inv2 callback answer failed", exc_info=True)


@router.callback_query(F.data.startswith("inv2:join:"))
@router.callback_query(F.data.startswith("inv2:joinbuff:"))
async def handle_inv2_join(callback: CallbackQuery, bot) -> None:
    try:
        if not await MaintenanceService._invasion_enabled():
            await _answer(callback, "⚔️ سامانهٔ یورش موقتاً غیرفعال است.", alert=True)
            return
        data = callback.data or ""
        use_buff = data.startswith("inv2:joinbuff:")
        suffix = data.removeprefix("inv2:joinbuff:" if use_buff else "inv2:join:")
        if not suffix.isdecimal() or not callback.from_user or not callback.message:
            await _answer(callback, "این یورش به پایان رسیده است.", alert=True)
            return
        invasion_id = int(suffix)
        user_id = callback.from_user.id
        async with get_session() as session:
            # Group/invasion row lock serializes joins on MariaDB; CAS below
            # also rejects racing writers without a row-locking database.
            inv = await session.scalar(select(Inv2Invasion).where(Inv2Invasion.id == invasion_id).with_for_update())
            now = datetime.now(timezone.utc)
            chat = getattr(callback.message, "chat", None)
            if (inv is None or inv.status != InvasionStatus.ACTIVE.value
                or inv.group_id != getattr(chat, "id", None)
                or inv.message_id != getattr(callback.message, "message_id", None)
                or inv.ends_at is None
                or (inv.ends_at.replace(tzinfo=timezone.utc) if inv.ends_at.tzinfo is None else inv.ends_at) <= now):
                await _answer(callback, "این یورش به پایان رسیده است.", alert=True)
                return
            previous = await session.scalar(select(Inv2Attack.id).where(
                Inv2Attack.invasion_id == inv.id, Inv2Attack.user_id == user_id))
            if previous is not None:
                await _answer(callback, "قبلاً در این یورش شرکت کرده‌اید.", alert=True)
                return
            if use_buff:
                # A killing blow settles every attacker's rewards. Take ALL
                # potential recipient user mutexes in ascending order before
                # inventory locks, not just the buff user's mutex: otherwise
                # simultaneous battles in different groups could invert them.
                participants = list((await session.scalars(select(Inv2Attack.user_id).where(
                    Inv2Attack.invasion_id == inv.id))).all())
                await lock_user_rows_in_order(session, [*participants, user_id])
                if await ItemService.get_total(session, user_id, "sib_buff", for_update=True) < 1:
                    await _answer(callback, "شما سیب باف ندارید! از فروشگاه تهیه کنید.", alert=True)
                    return
            card = await PlayerCardService.get_main_card(session, user_id)
            if card is None:
                await _answer(callback, "برای شرکت در یورش، ابتدا کارت اصلی پروفایل خود را تنظیم کنید.", alert=True)
                return
            character = await session.get(Character, card.character_id)
            if (character is None or type(card.level) is not int or not 1 <= card.level <= MAX_CARD_LEVEL
                or type(character.power) is not int or character.power < 0):
                await _answer(callback, "نیروی کارت اصلی مشخص نیست؛ یورش ثبت نشد.", alert=True)
                return
            power = card_attributes(card, character).power
            damage, crit = compute_damage(power, random.Random())
            # Apply to the rounded normal hit, independently of the unchanged
            # 10% critical roll (a critical hit can also receive the buff).
            if use_buff:
                damage *= 2
            remaining = max(0, inv.enemy_hp_remaining - damage)
            user = await session.get(User, user_id)
            name = user.display_name if user else callback.from_user.first_name
            story = render_join(username=name, card_name=character.name_fa,
                damage=damage, is_crit=crit, enemy_hp_remaining=remaining, rng=random.Random(),
                invasion_id=inv.id, is_buffed=use_buff)
            terminal = remaining <= 0
            # Compare-and-swap guards the old HP, active state and deadline.
            result = await session.execute(update(Inv2Invasion).where(
                Inv2Invasion.id == inv.id, Inv2Invasion.status == InvasionStatus.ACTIVE.value,
                Inv2Invasion.enemy_hp_remaining == inv.enemy_hp_remaining,
                Inv2Invasion.ends_at > now,
            ).execution_options(synchronize_session=False).values(enemy_hp_remaining=remaining,
                status=InvasionStatus.VICTORY.value if terminal else InvasionStatus.ACTIVE.value,
                finalized_at=now if terminal else None))
            if result.rowcount != 1:
                await _answer(callback, "وضعیت یورش تازه تغییر کرد؛ دوباره تلاش کنید.", alert=True)
                return
            # Only consume after the HP CAS succeeds. Consumption, attack,
            # HP and any victory rewards share this transaction; an exception
            # (including a duplicate insert) rolls all of them back together.
            if use_buff:
                await ItemService.consume(session, user_id, "sib_buff", 1)
            attack = Inv2Attack(invasion_id=inv.id, user_id=user_id, card_id=card.id,
                card_power=power, damage=damage, is_crit=crit, narrative=story, joined_at=now)
            session.add(attack)
            await session.flush()
            attack_id = attack.id
            if terminal:
                from sib.services.invasion_v2.rewards_service import settle_outcome
                await settle_outcome(session, inv.id, InvasionStatus.VICTORY.value)
        # The transaction is committed before acknowledgement or any image work.
        await _answer(callback, "💥 سیب باف مصرف شد؛ ضربه‌ات با آسیب ۲ برابر ثبت شد!"
                      if use_buff else "⚔️ ضربه‌ات ثبت شد!")
        presenter = InvasionPresentation()

        async def deliver():
            await presenter.on_join(bot, invasion_id, user_id, attack_id)
            if terminal:
                await presenter.finalize(bot, invasion_id)

        task = asyncio.create_task(deliver())
        task.add_done_callback(lambda t: logger.error("inv2 delivery task failed", exc_info=t.exception()) if not t.cancelled() and t.exception() else None)
    except Exception:
        logger.exception("inv2 join failed")
        await _answer(callback, "ثبت ضربه ممکن نشد؛ کمی بعد دوباره تلاش کنید.", alert=True)
