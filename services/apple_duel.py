"""Direct duel lifecycle. No queue, new formula, loadout or economy system."""

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sib.core.config import settings
from sib.database.models import (
    AppleBattle,
    AppleBattleStatus,
    AppleDuel,
    AppleDuelSlot,
    Character,
    PlayerCard,
    Trade,
    TradeStatus,
    User,
)
from sib.services.apple_battle import AppleBattleService, BattleError
from sib.services.player_cards import PlayerCardService
from sib.services.session import SessionService
from sqlalchemy import delete, select, update


class DuelError(ValueError):
    pass


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def now_utc():
    return datetime.now(timezone.utc)


class AppleDuelService:
    @staticmethod
    async def _lock_users(session, ids):
        from sib.services.lock_order import lock_user_rows_in_order

        await lock_user_rows_in_order(session, ids)
        users = list(
            (
                await session.scalars(
                    select(User)
                    .where(User.id.in_(ids))
                    .order_by(User.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(users) != 2 or any(u.is_bot or not u.is_active for u in users):
            raise DuelError("هر دو مبارز باید حساب کاربری معتبر داشته باشند.")
        return users

    @staticmethod
    async def _release(session, duel_id):
        await session.execute(
            delete(AppleDuelSlot).where(AppleDuelSlot.duel_id == duel_id)
        )

    @classmethod
    async def expire(cls, session, duel_id, *, now=None):
        now = now or now_utc()
        changed = await session.execute(
            update(AppleDuel)
            .where(
                AppleDuel.id == duel_id,
                AppleDuel.status == "PENDING",
                AppleDuel.expires_at <= now,
            )
            .values(status="EXPIRED", terminal_presented=False)
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount:
            await cls._release(session, duel_id)
        return bool(changed.rowcount)

    @classmethod
    async def create(cls, session, group_id, inviter_id, recipient_id, *, now=None):
        if inviter_id == recipient_id:
            raise DuelError("نمی‌توانی خودت را به دوئل دعوت کنی.")
        now = now or now_utc()
        async with session.begin_nested():
            await cls._lock_users(session, [inviter_id, recipient_id])
            for uid in (inviter_id, recipient_id):
                old = await session.get(AppleDuelSlot, uid)
                if old:
                    await cls.expire(session, old.duel_id, now=now)
                if (
                    await SessionService.live_party_for_user(session, uid, lock=True)
                    or await SessionService.live_match_for_user(session, uid, lock=True)
                    or await SessionService.has_active_session(session, uid)
                ):
                    raise DuelError("یکی از بازیکنان دعوت یا بازی فعال دیگری دارد.")
                if not await session.scalar(
                    select(PlayerCard.id).where(PlayerCard.user_id == uid).limit(1)
                ):
                    role = "دعوت‌کننده" if uid == inviter_id else "بازیکن دعوت‌شده"
                    raise DuelError(f"{role} هنوز هیچ کارتی ندارد؛ دعوت ساخته نشد.")
            duel = AppleDuel(
                id=uuid.uuid4().hex,
                group_id=group_id,
                inviter_id=inviter_id,
                recipient_id=recipient_id,
                status="PENDING",
                expires_at=now + timedelta(seconds=settings.APPLE_DUEL_EXPIRY_SECONDS),
            )
            session.add(duel)
            await session.flush()
            session.add_all(
                [
                    AppleDuelSlot(user_id=uid, duel_id=duel.id)
                    for uid in (inviter_id, recipient_id)
                ]
            )
            await session.flush()
        return duel

    @staticmethod
    async def authorize(session, duel_id, actor_id, group_id, message_id=None):
        duel = await session.scalar(
            select(AppleDuel)
            .where(AppleDuel.id == duel_id)
            .execution_options(populate_existing=True)
        )
        if not duel:
            raise DuelError("دعوت پیدا نشد.")
        if duel.recipient_id != actor_id or duel.group_id != group_id:
            raise DuelError("این دکمه فقط برای بازیکن دعوت‌شده در گروه خودش است.")
        if message_id is not None and duel.message_id != message_id:
            raise DuelError("پیام دعوت معتبر نیست.")
        return duel

    @classmethod
    async def act(
        cls,
        session,
        duel_id,
        actor_id,
        group_id,
        action,
        *,
        message_id=None,
        now=None,
        tie_choice=secrets.choice,
    ):
        supplied_now = now
        now = now or now_utc()
        duel = await cls.authorize(session, duel_id, actor_id, group_id, message_id)
        # Global lock order: User ids ascending -> invitation -> cards ascending.
        await cls._lock_users(session, [duel.inviter_id, duel.recipient_id])
        duel = await session.scalar(
            select(AppleDuel)
            .where(AppleDuel.id == duel_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = supplied_now or now_utc()
        if duel.status != "PENDING":
            return duel
        if utc(duel.expires_at) <= now:
            await cls.expire(session, duel.id, now=now)
            await session.refresh(duel)
            return duel
        if action == "change":
            return duel  # UI opens the existing collection; no acceptance.
        if action not in ("accept", "reject"):
            raise DuelError("گزینهٔ دعوت معتبر نیست.")
        if action == "reject":
            claimed = await session.execute(
                update(AppleDuel)
                .where(
                    AppleDuel.id == duel.id,
                    AppleDuel.status == "PENDING",
                    AppleDuel.expires_at > now,
                )
                .values(status="REJECTED")
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount:
                await cls._release(session, duel.id)
            await session.refresh(duel)
            return duel
        # Savepoint makes catching a payment/validation error safe for callers.
        async with session.begin_nested():
            for uid in (duel.inviter_id, duel.recipient_id):
                if await SessionService.live_party_for_user(
                    session, uid
                ) or await SessionService.live_match_for_user(session, uid, lock=True):
                    raise DuelError("یکی از بازیکنان در بازی دیگری است.")
            mains = [
                await PlayerCardService.get_main_card(session, uid)
                for uid in (duel.inviter_id, duel.recipient_id)
            ]
            if any(c is None for c in mains):
                raise DuelError(
                    "هر دو بازیکن باید کارت اصلی انتخاب کنند؛ از «کارت‌های من» یا «تعویض کارت» استفاده کن."
                )
            cards = list(
                (
                    await session.scalars(
                        select(PlayerCard)
                        .where(PlayerCard.id.in_([c.id for c in mains]))
                        .order_by(PlayerCard.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).all()
            )
            by_id = {c.id: c for c in cards}
            character_ids = sorted({c.character_id for c in cards})
            characters = {
                c.id: c
                for c in (
                    await session.scalars(
                        select(Character)
                        .where(Character.id.in_(character_ids))
                        .order_by(Character.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).all()
            }
            snapshots = []
            for uid, main in zip((duel.inviter_id, duel.recipient_id), mains):
                card = by_id.get(main.id)
                if not card or card.user_id != uid or not card.is_favorite:
                    raise DuelError(
                        "کارت اصلی تغییر کرده یا دیگر متعلق به بازیکن نیست؛ دوباره تلاش کن."
                    )
                pending_trade = await session.scalar(
                    select(Trade.id)
                    .where(
                        Trade.status == TradeStatus.PENDING,
                        (Trade.proposer_card_id == card.id)
                        | (Trade.recipient_card_id == card.id),
                    )
                    .limit(1)
                    .with_for_update()
                )
                if pending_trade:
                    raise DuelError(
                        "یکی از کارت‌های اصلی در مبادلهٔ باز است؛ کارت دیگری انتخاب کن."
                    )
                char = characters.get(card.character_id)
                if not char or not char.is_active:
                    raise DuelError("شخصیت یکی از کارت‌ها در دسترس نیست.")
                stats = AppleBattleService._card_stats(card, char)
                user = await session.get(User, uid)
                snapshots.append(
                    dict(
                        user_id=uid,
                        player_name=user.display_name[:80],
                        card_id=card.id,
                        character_id=char.id,
                        code=char.code,
                        artwork=char.artwork,
                        name=char.name_fa[:80],
                        level=card.level,
                        power=stats.power,
                        speed=stats.speed,
                        intelligence=stats.intelligence,
                        hidden_modifiers=char.hidden_modifier_params,
                        passive_key=char.passive_key,
                        passive_data=char.passive_data,
                        balance_version=char.balance_version,
                    )
                )
            # Re-read clock after potentially slow card/character lock waits.
            now = supplied_now or now_utc()
            if utc(duel.expires_at) <= now:
                await cls.expire(session, duel.id, now=now)
                await session.refresh(duel)
                return duel
            claimed = await session.execute(
                update(AppleDuel)
                .where(
                    AppleDuel.id == duel.id,
                    AppleDuel.status == "PENDING",
                    AppleDuel.expires_at > now,
                )
                .values(status="ACCEPTED")
                .execution_options(synchronize_session=False)
            )
            if not claimed.rowcount:
                await session.refresh(duel)
                return duel
            battle = AppleBattle(
                player1_id=duel.inviter_id,
                player2_id=duel.recipient_id,
                player1_card_id=mains[0].id,
                player2_card_id=mains[1].id,
                level=mains[0].level,
                status=AppleBattleStatus.PENDING,
                matched_at=now,
            )
            session.add(battle)
            await session.flush()
            await AppleBattleService.compute_and_persist_result(session, battle.id)
            result = json.loads(battle.result_json)
            # Current engine always has a winner; only explicit future draws use this rule.
            if result.get("winner_id") is None:
                battle.winner_id = tie_choice([duel.inviter_id, duel.recipient_id])
                result.update(winner_id=battle.winner_id, tie_break="uniform_random")
                battle.result_json = json.dumps(result, ensure_ascii=False)
            if battle.winner_id not in (duel.inviter_id, duel.recipient_id):
                raise BattleError("نتیجهٔ نبرد معتبر نیست.")
            await AppleBattleService._award_seedling(
                session, battle.id, battle.winner_id
            )
            battle.seedlings_awarded = 1
            battle.status = AppleBattleStatus.COMPLETED
            battle.completed_at = now
            duel.status = "ACCEPTED"
            duel.battle_id = battle.id
            duel.snapshot_json = json.dumps(
                {"cards": snapshots, "events": [], "engine": "apple_battle_v1"},
                ensure_ascii=False,
            )
            duel.cursor = 0
            duel.next_scene_at = now
            await session.flush()
        return duel

    @classmethod
    async def claim_scene(cls, session, duel_id, *, now=None):
        """DB-only at-most-once scene claim; commit before Telegram I/O."""
        now = now or now_utc()
        duel = await session.scalar(
            select(AppleDuel)
            .where(AppleDuel.id == duel_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not duel or duel.status != "ACCEPTED" or utc(duel.next_scene_at) > now:
            return None
        cursor = duel.cursor
        changed = await session.execute(
            update(AppleDuel)
            .where(
                AppleDuel.id == duel.id,
                AppleDuel.status == "ACCEPTED",
                AppleDuel.cursor == cursor,
                AppleDuel.next_scene_at <= now,
            )
            .values(
                cursor=cursor + 1,
                next_scene_at=now
                + timedelta(seconds=settings.APPLE_DUEL_SCENE_SECONDS),
                status="COMPLETED" if cursor == 5 else "ACCEPTED",
            )
            .execution_options(synchronize_session=False)
        )
        if not changed.rowcount:
            return None
        if cursor == 5:
            await cls._release(session, duel.id)
        return cursor

    @classmethod
    async def release_scene(cls, session, duel_id, index, *, now=None):
        """Give a claimed-but-undelivered scene back so maintenance retries it.

        Called only after every Telegram fallback for the scene failed. The
        CAS guard rewinds exactly one claim: a worker that already claimed a
        newer scene (or finished/terminal duels) is never reordered, so this
        cannot duplicate, skip-forward or re-run settled results.
        """
        now = now or now_utc()
        changed = await session.execute(
            update(AppleDuel)
            .where(
                AppleDuel.id == duel_id,
                AppleDuel.status == "ACCEPTED",
                AppleDuel.cursor == index + 1,
            )
            .values(
                cursor=index,
                next_scene_at=now
                + timedelta(seconds=settings.APPLE_DUEL_SCENE_SECONDS),
            )
            .execution_options(synchronize_session=False)
        )
        return bool(changed.rowcount)
