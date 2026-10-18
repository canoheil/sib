"""Apple Clash (Group War) domain service. Caller owns commit/rollback."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
from math import ceil
from typing import List, Optional, Tuple
from sqlalchemy import func, or_, select

from sib.database.models import (
    Character,
    Group,
    GroupCastle,
    GroupWar,
    PlayerCard,
    WarAttack,
    WarParticipant,
)
from sib.services.card_progression import card_attributes
from sib.services.castle_service import CastleService, apply_castle_buff, level_for_total
from sib.services.invasion_v2.damage import compute_damage
from sib.services.items import ItemService
from sib.services.sib_claim import fa_digits
from sib.services.lock_order import lock_user_row
from sib.services.player_cards import PlayerCardService

WAR_DURATION = timedelta(hours=5)
PEACE_SHIELD_DURATION = timedelta(hours=12)
# Share of the loser's ``total_donated`` the winner plunders when a war ends.
WAR_LOOT_PERCENT = 15
ATTACK_ENERGY_COST = 20
ATTACK_COOLDOWN_SECONDS = 30


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def war_loot(loser_total_donated: int) -> int:
    """Apples plundered from the loser: 15% of its ``total_donated``, rounded down.

    This is ``int(total_donated * 0.15)`` computed in integers: the result is
    identical for every value an INT column can hold (0 .. 2**31 - 1, checked
    exhaustively), but no binary-float rounding is involved.
    """
    return max(0, int(loser_total_donated)) * WAR_LOOT_PERCENT // 100


class WarError(ValueError):
    pass


@dataclass(frozen=True)
class WarOutcome:
    """What one finished war settled; the input of the result announcement.

    ``winner_id`` / ``loser_id`` are group ids (a group id is its Telegram chat
    id). Both are ``None`` for a draw, which has no loot and no peace shield.
    """

    war_id: int
    attacker_id: int
    defender_id: int
    winner_id: Optional[int]
    loser_id: Optional[int]
    loot: int

    @property
    def is_draw(self) -> bool:
        return self.winner_id is None


class WarService:
    @staticmethod
    async def find_target_groups(
        session, group_id: int, *, now: Optional[datetime] = None
    ) -> List[Tuple[GroupCastle, str]]:
        """Query up to 5 candidate enemy groups with castle level delta <= 3, not in war, and no active shield."""
        now = now or datetime.now(timezone.utc)
        castle = await session.scalar(
            select(GroupCastle).where(GroupCastle.group_id == group_id)
        )
        if castle is None:
            raise WarError("قلعه‌ای برای این گروه یافت نشد.")
        if castle.in_war:
            raise WarError("گروه شما در حال حاضر در یک نبرد فعال است!")
        if castle.shield_until and _utc(castle.shield_until) > now:
            raise WarError("قلعهٔ شما تحت پوشش سپر صلح است!")

        min_level = max(1, castle.level - 3)
        max_level = min(10, castle.level + 3)

        query = (
            select(GroupCastle, Group.title)
            .join(Group, Group.id == GroupCastle.group_id)
            .where(
                GroupCastle.group_id != group_id,
                Group.is_active.is_(True),
                GroupCastle.level.between(min_level, max_level),
                GroupCastle.in_war.is_(False),
                or_(
                    GroupCastle.shield_until.is_(None), GroupCastle.shield_until <= now
                ),
            )
            .order_by(func.random())
            .limit(5)
        )
        rows = (await session.execute(query)).all()
        return [(r[0], r[1]) for r in rows]

    @staticmethod
    async def declare_war(
        session,
        attacker_group_id: int,
        defender_group_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> GroupWar:
        """Lock both castles in ASC order, verify availability, set in_war, and create 5h GroupWar."""
        now = now or datetime.now(timezone.utc)
        if attacker_group_id == defender_group_id:
            raise WarError("امکان اعلان جنگ به گروه خودی وجود ندارد.")

        first_id, second_id = sorted([attacker_group_id, defender_group_id])
        castles = list(
            (
                await session.scalars(
                    select(GroupCastle)
                    .where(GroupCastle.group_id.in_([first_id, second_id]))
                    .order_by(GroupCastle.group_id)
                    .with_for_update()
                )
            ).all()
        )
        castle_map = {c.group_id: c for c in castles}
        attacker_castle = castle_map.get(attacker_group_id)
        defender_castle = castle_map.get(defender_group_id)

        if not attacker_castle or not defender_castle:
            raise WarError("قلعهٔ یکی از گروه‌ها یافت نشد.")
        if attacker_castle.in_war:
            raise WarError("گروه مهاجم در حال حاضر در نبرد است.")
        if defender_castle.in_war:
            raise WarError("گروه مدافع در حال حاضر در نبرد است.")
        if attacker_castle.shield_until and _utc(attacker_castle.shield_until) > now:
            raise WarError("گروه مهاجم تحت پوشش سپر صلح است.")
        if defender_castle.shield_until and _utc(defender_castle.shield_until) > now:
            raise WarError("گروه مدافع تحت پوشش سپر صلح است.")

        attacker_castle.in_war = True
        defender_castle.in_war = True

        war = GroupWar(
            attacker_id=attacker_group_id,
            defender_id=defender_group_id,
            status="active",
            attacker_damage=0,
            defender_damage=0,
            started_at=now,
            ends_at=now + WAR_DURATION,
            looted_apples=0,
        )
        session.add(war)
        await session.flush()
        return war

    @staticmethod
    async def finish_war(session, war_id: int, *, now=None) -> Optional[WarOutcome]:
        """Settle a due war exactly once; return what was settled (None: nothing due).

        Everything happens in the caller's transaction, under row locks: both
        castle rows (group-id order, as in ``declare_war``) and then the war row
        are read with ``SELECT ... FOR UPDATE`` and refreshed from the database.
        Attacks lock only the war row and cannot pass the stored ``ends_at``
        deadline; donations lock the castle row. Once the locks are held the
        damage totals and both treasuries are final, so loot is computed from
        current values and nothing can change before the caller commits. The
        war is re-checked under its lock, so a second or concurrent run is a
        no-op (never a double loot).

        * the higher damage wins; a tie has no winner, no loot and no shield;
        * the winner plunders ``war_loot(loser.total_donated)`` (15%, rounded
          down): it moves from the loser's ``total_donated`` to the winner's
          and is recorded in ``looted_apples``;
        * only the loser gets the 12 hour peace shield;
        * ``in_war`` is cleared on both castles and the war becomes finished.
        """
        now = now or datetime.now(timezone.utc)
        sides = (
            await session.execute(
                select(GroupWar.attacker_id, GroupWar.defender_id).where(
                    GroupWar.id == war_id
                )
            )
        ).first()
        if sides is None:
            return None
        castles = list(
            (
                await session.scalars(
                    select(GroupCastle)
                    .where(GroupCastle.group_id.in_(sides))
                    .order_by(GroupCastle.group_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        war = await session.scalar(
            select(GroupWar)
            .where(GroupWar.id == war_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if war is None:
            return None
        ends_at = _utc(war.ends_at)
        if war.status != "active" or ends_at > now:
            return None

        winner_id = loser_id = None
        if war.attacker_damage != war.defender_damage:
            if war.attacker_damage > war.defender_damage:
                winner_id, loser_id = war.attacker_id, war.defender_id
            else:
                winner_id, loser_id = war.defender_id, war.attacker_id
            war.winner_id = winner_id

        by_group = {castle.group_id: castle for castle in castles}
        winner_castle = by_group.get(winner_id)
        loser_castle = by_group.get(loser_id)
        loot = 0
        if winner_castle is not None and loser_castle is not None:
            loot = war_loot(loser_castle.total_donated)
            if loot:
                loser_castle.total_donated -= loot
                winner_castle.total_donated += loot
                # ``level`` is derived from ``total_donated`` and
                # CastleService.donate keeps the two in step. Do the same here,
                # so a stored level never disagrees with its own total (a loser
                # may therefore drop a level; a winner may gain one).
                loser_castle.level = level_for_total(loser_castle.total_donated)
                winner_castle.level = level_for_total(winner_castle.total_donated)

        for castle in castles:
            castle.in_war = False
            if castle.group_id == loser_id:
                castle.shield_until = now + PEACE_SHIELD_DURATION
        war.looted_apples = loot
        war.status = "finished"
        await session.flush()
        return WarOutcome(
            war_id=war.id,
            attacker_id=war.attacker_id,
            defender_id=war.defender_id,
            winner_id=winner_id,
            loser_id=loser_id,
            loot=loot,
        )

    @classmethod
    async def finish_expired_wars(
        cls,
        session,
        *,
        now=None,
        outcomes: Optional[List[WarOutcome]] = None,
    ) -> int:
        """Maintenance entry point; caller commits expiration, loot and shield together.

        Returns how many wars were settled. ``outcomes`` optionally collects
        what each of them decided, so the caller can announce the results AFTER
        its commit while the historic integer return value is preserved.
        """
        now = now or datetime.now(timezone.utc)
        ids = (
            await session.scalars(
                select(GroupWar.id)
                .where(GroupWar.status == "active", GroupWar.ends_at <= now)
                .order_by(GroupWar.id)
            )
        ).all()
        finished = 0
        for war_id in ids:
            outcome = await cls.finish_war(session, war_id, now=now)
            if outcome is None:
                continue
            finished += 1
            if outcomes is not None:
                outcomes.append(outcome)
        return finished

    @staticmethod
    async def get_active_war(session, group_id: int) -> Optional[GroupWar]:
        """Get currently active war for a given group."""
        return await session.scalar(
            select(GroupWar)
            .where(
                or_(
                    GroupWar.attacker_id == group_id,
                    GroupWar.defender_id == group_id,
                ),
                GroupWar.status == "active",
            )
            .order_by(GroupWar.id.desc())
            .limit(1)
        )

    @staticmethod
    async def get_or_create_participant(
        session, war_id: int, user_id: int, group_id: int
    ) -> WarParticipant:
        """Get or create participant row under row lock with 100 energy default."""
        part = await session.scalar(
            select(WarParticipant)
            .where(
                WarParticipant.war_id == war_id,
                WarParticipant.user_id == user_id,
            )
            .with_for_update()
        )
        if part is None:
            part = WarParticipant(
                war_id=war_id,
                user_id=user_id,
                group_id=group_id,
                energy=100,
                total_damage=0,
            )
            session.add(part)
            await session.flush()
        return part

    @classmethod
    async def attack(
        cls,
        session,
        war_id: int,
        user_id: int,
        group_id: int,
        *,
        is_buff_used: bool = False,
        now: Optional[datetime] = None,
    ) -> Tuple[int, int, str]:
        """Execute an attack or defense hit. Costs 20 energy, checks 30s cooldown. Returns (damage, remaining_energy, card_name)."""
        now = now or datetime.now(timezone.utc)
        await lock_user_row(session, user_id)

        war = await session.scalar(
            select(GroupWar).where(GroupWar.id == war_id).with_for_update()
        )
        if not war or war.status != "active":
            raise WarError("این نبرد فعال نیست.")
        if now >= _utc(war.ends_at):
            raise WarError("زمان نبرد به پایان رسیده است.")
        if group_id not in (war.attacker_id, war.defender_id):
            raise WarError("گروه شما در این نبرد حضور ندارد.")

        target_group_id = (
            war.defender_id if group_id == war.attacker_id else war.attacker_id
        )

        part = await cls.get_or_create_participant(session, war_id, user_id, group_id)

        # 30-second cooldown check
        if part.last_attack_at is not None:
            last_at = (
                part.last_attack_at.replace(tzinfo=timezone.utc)
                if part.last_attack_at.tzinfo is None
                else part.last_attack_at
            )
            elapsed = (now - last_at).total_seconds()
            if elapsed < ATTACK_COOLDOWN_SECONDS:
                remaining_sec = ceil(ATTACK_COOLDOWN_SECONDS - elapsed)
                raise WarError(
                    f"برای ضربهٔ بعدی باید {fa_digits(remaining_sec)} ثانیه دیگر صبر کنید."
                )

        # Energy check
        if part.energy < ATTACK_ENERGY_COST:
            raise WarError("انرژی جنگی شما کافی نیست! (حداقل ۲۰ انرژی نیاز است).")

        # Sib-Buff item check
        if is_buff_used:
            buff_count = await ItemService.get_total(
                session, user_id, "sib_buff", for_update=True
            )
            if buff_count < 1:
                raise WarError("شما سیب باف ندارید! از فروشگاه تهیه کنید.")

        # Main Card check
        card = await PlayerCardService.get_main_card(session, user_id)
        if card is None:
            raise WarError("ابتدا باید کارت اصلی خود را در پروفایل تنظیم کنید.")

        character = await session.get(Character, card.character_id)
        if character is None:
            raise WarError("اطلاعات کارت اصلی یافت نشد.")

        # Damage calculation: Main Card + Castle Buff
        power = card_attributes(card, character).power
        damage, crit = compute_damage(power, random.Random())
        castle_buff = await CastleService.get_invasion_buff(session, group_id)
        damage = apply_castle_buff(damage, castle_buff)

        if is_buff_used:
            damage *= 2
            await ItemService.consume(session, user_id, "sib_buff", 1)

        # Deduct attack energy & record
        part.energy -= ATTACK_ENERGY_COST
        part.total_damage += damage
        part.last_attack_at = now

        # Add damage to appropriate pool
        if group_id == war.attacker_id:
            war.attacker_damage += damage
        else:
            war.defender_damage += damage

        attack = WarAttack(
            war_id=war.id,
            user_id=user_id,
            source_group_id=group_id,
            target_group_id=target_group_id,
            damage=damage,
            is_buff_used=is_buff_used,
            created_at=now,
        )
        session.add(attack)
        await session.flush()

        return damage, part.energy, character.name_fa

    @classmethod
    async def use_healing_potion(
        cls, session, war_id: int, user_id: int, group_id: int
    ) -> int:
        """Drink 1 healing potion to restore 50 energy (capped at 100). Returns updated energy."""
        await lock_user_row(session, user_id)
        potion_count = await ItemService.get_total(
            session, user_id, "healing_potion", for_update=True
        )
        if potion_count < 1:
            raise WarError(
                "شما معجون شفا ندارید! می‌توانید آن را از فروشگاه تهیه کنید."
            )

        part = await cls.get_or_create_participant(session, war_id, user_id, group_id)
        if part.energy >= 100:
            raise WarError("انرژی جنگی شما پر است (۱۰۰ از ۱۰۰).")

        await ItemService.consume(session, user_id, "healing_potion", 1)
        part.energy = min(100, part.energy + 50)
        await session.flush()
        return part.energy
