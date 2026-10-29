"""Apple Clash (Group War) domain service. Caller owns commit/rollback."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
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
from sib.services.castle_service import (
    CastleService,
    apply_castle_buff,
    level_floor,
    level_for_total,
)
from sib.services.invasion_v2.damage import compute_damage
from sib.services.items import ItemService
from sib.services import disease_service
from sib.services.lock_order import lock_user_row
from sib.services.player_cards import PlayerCardService

# C2-9: War Blitz - a war is a fast, intense one-hour event.
WAR_DURATION = timedelta(hours=1)
# C2-6: the DEFENDER of a war always receives this shield when the war ends,
# whether it won, lost or drew. The ATTACKER never receives one.
# C2-9: shortened to 2 hours for the Blitz format.
PEACE_SHIELD_DURATION = timedelta(hours=2)
# The winner plunders at most this share of the loser's ``total_donated`` (see
# ``war_loot``: never so much that the loser would lose its level).
WAR_LOOT_PERCENT = 15
ATTACK_ENERGY_COST = 20
ATTACK_COOLDOWN_SECONDS = 30
# C2-8.5: an absolute ceiling per player per war. Energy can be refilled with
# healing potions, so energy alone never bounded the number of attacks.
MAX_ATTACKS_PER_WAR = 10
MAX_WAR_ENERGY = 100
HEALING_POTION_ENERGY = 50

# C2-8.5: user-facing alerts for the attack gate. Persian only (rule).
ATTACK_LIMIT_MESSAGE = "🛑 شما به سقف حداکثر ۱۰ حمله در این جنگ رسیده‌اید!"
COOLDOWN_MESSAGE = "⏳ لطفاً ۳۰ ثانیه صبر کنید."
ENERGY_MESSAGE = "⚡️ انرژی جنگی کافی ندارید!"
FULL_ENERGY_MESSAGE = "⚡️ انرژی شما در حال حاضر کامل است!"
SICK_WAR_MESSAGE = "🦠 شما بیمار هستید و نمی‌توانید در جنگ شرکت کنید. ابتدا درمان شوید."


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def shield_remaining_seconds(
    shield_until: Optional[datetime], *, now: Optional[datetime] = None
) -> Optional[int]:
    """Seconds left on a peace shield, or ``None`` when it is not active.

    A ``shield_until`` in the past is simply an expired shield: the castle is
    no longer protected (the column is never cleared by a timer, only by the
    settlement or by breaking the shield when the group attacks).
    """
    if shield_until is None:
        return None
    now = now or datetime.now(timezone.utc)
    left = (_utc(shield_until) - _utc(now)).total_seconds()
    return int(left) if left > 0 else None


def has_active_shield(castle: GroupCastle, *, now: Optional[datetime] = None) -> bool:
    """True when ``castle`` is still under its peace shield at ``now``."""
    return shield_remaining_seconds(castle.shield_until, now=now) is not None


def war_loot(loser_total_donated: int, loser_level: int) -> int:
    """Apples plundered from the loser: 15% of its treasury, capped to keep its level.

    ::

        intended_loot = total_donated * 15 // 100          # == int(total_donated * 0.15)
        max_safe_loot = total_donated - level_floor(level)  # held above its level's threshold
        actual_loot   = min(intended_loot, max_safe_loot)   # and never below zero

    "Castle levels never downgrade": the loser always keeps at least the
    ``level_floor`` of its CURRENT level, so a lost war cannot demote it. A
    castle that has only just reached its level (or a maxed castle: donations
    stop at exactly the level-10 floor) has little or nothing above the floor
    and loses little or nothing.

    ``intended_loot`` is ``int(total_donated * 0.15)`` computed in integers: the
    same number for every value an INT column can hold (0 .. 2**31 - 1, checked
    exhaustively), but without binary-float rounding. ``loser_level`` is the
    loser's stored level; a level the total cannot support (a negative
    ``max_safe_loot``) yields 0 rather than a negative loot.
    """
    total = max(0, int(loser_total_donated))
    intended_loot = total * WAR_LOOT_PERCENT // 100
    max_safe_loot = total - level_floor(loser_level)
    return max(0, min(intended_loot, max_safe_loot))


class WarError(ValueError):
    pass


@dataclass(frozen=True)
class WarOutcome:
    """What one finished war settled; the input of the result announcement.

    ``winner_id`` / ``loser_id`` are group ids (a group id is its Telegram chat
    id). Both are ``None`` for a draw, which has no loot; the peace shield is
    independent of the result and goes to the defender (C2-6).
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
    async def require_healthy_user(session, user_id: int) -> None:
        """Serialize the health check with infection/cure before war actions."""
        # Keep the refreshed row alive for is_sick's session.get under MySQL's
        # repeatable-read isolation, even if the session saw an older snapshot.
        locked_disease = await disease_service._disease_for_update(session, user_id)
        sick = await disease_service.is_sick(session, user_id)
        del locked_disease
        if sick:
            raise WarError(SICK_WAR_MESSAGE)

    @staticmethod
    async def active_shield(
        session, group_id: int, *, now: Optional[datetime] = None
    ) -> Optional[datetime]:
        """The group's peace-shield deadline, or ``None`` when it has none.

        Returns the stored ``shield_until`` only while it is still in the
        future; an expired timestamp reads as no shield at all.
        """
        now = now or datetime.now(timezone.utc)
        castle = await session.get(GroupCastle, group_id)
        if castle is None or not has_active_shield(castle, now=now):
            return None
        return _utc(castle.shield_until)

    @staticmethod
    async def break_shield(
        session, group_id: int, *, now: Optional[datetime] = None
    ) -> bool:
        """Break a group's peace shield immediately (rule: attacking ends it).

        The castle row is locked, the shield is set to ``NULL`` and the caller's
        transaction is flushed. Returns whether a shield was actually broken,
        so a stale confirmation click is harmless.
        """
        now = now or datetime.now(timezone.utc)
        castle = await session.get(
            GroupCastle, group_id, with_for_update=True, populate_existing=True
        )
        if castle is None or not has_active_shield(castle, now=now):
            return False
        castle.shield_until = None
        await session.flush()
        return True

    @staticmethod
    async def find_target_groups(
        session, group_id: int, *, now: Optional[datetime] = None
    ) -> List[Tuple[GroupCastle, str]]:
        """Query up to 5 candidate enemy groups with castle level delta <= 3, not in war, and no active shield.

        C2-6: a group under an active peace shield is never listed as a target
        (it is protected), and a shielded group cannot browse targets before it
        has confirmed breaking its own shield.
        """
        now = now or datetime.now(timezone.utc)
        castle = await session.scalar(
            select(GroupCastle).where(GroupCastle.group_id == group_id)
        )
        if castle is None:
            raise WarError("قلعه‌ای برای این گروه یافت نشد.")
        if castle.in_war:
            raise WarError("گروه شما در حال حاضر در یک نبرد فعال است!")
        if has_active_shield(castle, now=now):
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
        user_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> GroupWar:
        """Lock both castles in ASC order, verify availability, set in_war, and create the war.

        The deadline is ``now + WAR_DURATION`` (C2-9: one hour, Blitz mode).
        """
        now = now or datetime.now(timezone.utc)
        if attacker_group_id == defender_group_id:
            raise WarError("امکان اعلان جنگ به گروه خودی وجود ندارد.")
        # Internal callers with no actor retain their existing behavior; the
        # Telegram declaration path always supplies the initiating user.
        if user_id is not None:
            await WarService.require_healthy_user(session, user_id)

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
        if has_active_shield(defender_castle, now=now):
            raise WarError("گروه مدافع تحت پوشش سپر صلح است.")
        # C2-6: declaring a war breaks the attacker's own peace shield at this
        # instant. The UI asks for confirmation first; this is the enforcement,
        # so no caller can attack while still protected.
        if has_active_shield(attacker_castle, now=now):
            attacker_castle.shield_until = None

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

        * the higher damage wins; a tie has no winner and no loot;
        * the winner plunders ``war_loot(loser.total_donated, loser.level)``:
          15% rounded down, but never more than the loser holds above the
          floor of its current level, so a lost war cannot demote it. The loot
          moves from the loser's ``total_donated`` to the winner's and is
          recorded in ``looted_apples``;
        * C2-6: the DEFENDER always gets the peace shield
          (``PEACE_SHIELD_DURATION``, C2-9: 2 hours) when the war ends - on a
          win, on a loss and on a draw. The ATTACKER never gets one (a
          leftover shield, if any, is cleared), so it can be attacked again
          immediately;
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
            # Treasury and level of the loser as they are NOW (read under the locks).
            loot = war_loot(loser_castle.total_donated, loser_castle.level)
            if loot:
                loser_castle.total_donated -= loot
                winner_castle.total_donated += loot
                # The cap leaves the loser at or above the floor of its level,
                # so its ``level`` is not touched: a war never lowers a
                # castle's level. The winner may cross a threshold; re-derive
                # its level the way CastleService.donate does - never lower it.
                winner_castle.level = max(
                    winner_castle.level, level_for_total(winner_castle.total_donated)
                )

        for castle in castles:
            castle.in_war = False
            if castle.group_id == war.defender_id:
                # The defender is always shielded when the war ends, whatever
                # the result. A draw grants it too (``defender_id`` is fixed).
                castle.shield_until = now + PEACE_SHIELD_DURATION
            else:
                # The attacker is never shielded: it can be attacked at once.
                castle.shield_until = None
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
        """Get or create the participant row, locked, with 100 energy and 0 attacks.

        ``populate_existing`` refreshes an instance that is already in the
        session's identity map, so every caller decides on the values that are
        actually stored - never on a stale in-memory copy. The ``FOR UPDATE``
        lock is what serializes concurrent attack clicks for the same player.
        """
        # Status screens enroll implicitly, and existing participants can
        # become sick mid-war, so check both new and existing participants.
        await WarService.require_healthy_user(session, user_id)
        part = await session.scalar(
            select(WarParticipant)
            .where(
                WarParticipant.war_id == war_id,
                WarParticipant.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if part is None:
            part = WarParticipant(
                war_id=war_id,
                user_id=user_id,
                group_id=group_id,
                energy=MAX_WAR_ENERGY,
                total_damage=0,
                attack_count=0,
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
        """Execute an attack or defense hit.

        The attack gate runs under row locks, in this order:

        1. the participant row is locked (``SELECT ... FOR UPDATE``);
        2. ``attack_count < 10`` - an absolute cap, independent of energy;
        3. 30-second cooldown since the STORED ``last_attack_at``;
        4. at least 20 energy left.

        Only then are 20 energy and one attack spent, ``last_attack_at`` is
        stamped, the damage is added to the war and a ``war_attacks`` row is
        written. Everything is part of the caller's transaction, which the
        handler commits immediately after this call returns; the returned
        energy is therefore already the value the next click will read.

        Returns ``(damage, remaining_energy, card_name)``.
        """
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

        # 1. Locked participant row (fresh values, FOR UPDATE held).
        part = await cls.get_or_create_participant(session, war_id, user_id, group_id)

        # 2. Hard cap: 10 attacks per player per war, whatever the energy is.
        #    Checked before any resource is spent, so a capped player loses
        #    nothing by clicking (no energy, no buff, no potion).
        if part.attack_count >= MAX_ATTACKS_PER_WAR:
            raise WarError(ATTACK_LIMIT_MESSAGE)

        # 3. 30-second cooldown, measured against the STORED timestamp.
        if part.last_attack_at is not None:
            last_at = (
                part.last_attack_at.replace(tzinfo=timezone.utc)
                if part.last_attack_at.tzinfo is None
                else part.last_attack_at
            )
            elapsed = (now - last_at).total_seconds()
            if elapsed < ATTACK_COOLDOWN_SECONDS:
                raise WarError(COOLDOWN_MESSAGE)

        # 4. Energy check
        if part.energy < ATTACK_ENERGY_COST:
            raise WarError(ENERGY_MESSAGE)

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

        # 5-7. Spend the attack: energy, cap counter, cooldown stamp and damage
        #      are written together in the caller's transaction.
        part.attack_count += 1
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
        """Drink 1 healing potion to restore 50 energy, capped at 100.

        The potion row is locked and checked first (a player with none is told
        so and loses nothing), then the participant row is locked. At full
        energy the potion is NOT consumed. Otherwise exactly one potion is
        removed from the inventory and the energy is capped with
        ``min(100, current + 50)`` - drinking at 70 gives 100, never 120. The
        caller commits the inventory decrement and the new energy together.

        Returns the updated war energy.
        """
        await WarService.require_healthy_user(session, user_id)
        potion_count = await ItemService.get_total(
            session, user_id, "healing_potion", for_update=True
        )
        if potion_count < 1:
            raise WarError(
                "شما معجون شفا ندارید! می‌توانید آن را از فروشگاه تهیه کنید."
            )

        part = await cls.get_or_create_participant(session, war_id, user_id, group_id)
        if part.energy >= MAX_WAR_ENERGY:
            raise WarError(FULL_ENERGY_MESSAGE)

        await ItemService.consume(session, user_id, "healing_potion", 1)
        part.energy = min(MAX_WAR_ENERGY, part.energy + HEALING_POTION_ENERGY)
        await session.flush()
        return part.energy
