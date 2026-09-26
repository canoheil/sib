"""Apple Battle service (Phase 14) — «نبرد سیبی».

Separate matchmaking: a FIFO queue pairs players at the SAME card level.
Battle resolution is fully controlled:
- deterministic inputs (card stats, character hidden modifiers, passives),
- one round,
- seeded RNG with the seed DOCUMENTED in the battle record,
- the result is PRECOMPUTED and persisted (with the balance versions of
  the cards/characters used) BEFORE the narration is generated,
- the winner receives exactly +1 seedling (idempotent).

Narration is generated from the persisted result only — the story can
never change the outcome.
"""

import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.database.models import (
    AppleBattle,
    AppleBattleQueueEntry,
    AppleBattleStatus,
    Character,
    PlayerCard,
    SeedlingAward,
    SeedlingInventory,
)
from sib.services.config_store import ConfigService
from sib.services.player_cards import PlayerCardService
from sib.services.seedling import SeedlingService


class BattleError(ValueError):
    """Raised for invalid apple battle operations."""


class AppleBattleService:
    """Queue matchmaking, controlled result computation, narration."""

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------
    @classmethod
    async def enqueue(cls, session: AsyncSession, user_id: int, card_id: int) -> AppleBattleQueueEntry:
        card = await session.get(PlayerCard, card_id)
        if not card or card.user_id != user_id:
            raise BattleError("کارت موردنظر متعلق به شما نیست.")

        existing = await session.scalar(
            select(AppleBattleQueueEntry).where(AppleBattleQueueEntry.user_id == user_id)
        )
        if existing:
            raise BattleError("شما همین حالا در صف نبرد هستید!")

        now = datetime.now(timezone.utc)
        timeout = int(await ConfigService.get(session, "apple_battle_queue_timeout_seconds"))
        entry = AppleBattleQueueEntry(
            user_id=user_id,
            card_id=card_id,
            level=card.level,
            queued_at=now,
            expires_at=now + timedelta(seconds=timeout),
        )
        session.add(entry)
        await session.flush()
        log_event(Events.APPLE_BATTLE_EVENT, {"action": "enqueued", "user_id": user_id, "level": card.level})
        return entry

    @classmethod
    async def dequeue(cls, session: AsyncSession, user_id: int) -> bool:
        entry = await session.scalar(
            select(AppleBattleQueueEntry).where(AppleBattleQueueEntry.user_id == user_id)
        )
        if not entry:
            return False
        await session.delete(entry)
        await session.flush()
        log_event(Events.APPLE_BATTLE_EVENT, {"action": "dequeued", "user_id": user_id})
        return True

    # ------------------------------------------------------------------
    # Matchmaking (separate system, same-level only, FIFO)
    # ------------------------------------------------------------------
    @classmethod
    async def try_match(cls, session: AsyncSession) -> Optional[AppleBattle]:
        now = datetime.now(timezone.utc)
        # Drop expired entries first.
        expired = (
            await session.scalars(
                select(AppleBattleQueueEntry).where(AppleBattleQueueEntry.expires_at <= now)
            )
        ).all()
        for entry in expired:
            await session.delete(entry)
        if expired:
            await session.flush()

        entries = (
            await session.scalars(
                select(AppleBattleQueueEntry).order_by(
                    AppleBattleQueueEntry.level.asc(),
                    AppleBattleQueueEntry.id.asc(),
                )
            )
        ).all()

        seen_levels: Dict[int, AppleBattleQueueEntry] = {}
        for entry in entries:
            if entry.level in seen_levels:
                first = seen_levels.pop(entry.level)
                await session.delete(first)
                await session.delete(entry)
                await session.flush()
                battle = await cls._create_battle(session, first, entry, now)
                return battle
            seen_levels[entry.level] = entry
        return None

    @classmethod
    async def _create_battle(
        cls,
        session: AsyncSession,
        first: AppleBattleQueueEntry,
        second: AppleBattleQueueEntry,
        now: datetime,
    ) -> AppleBattle:
        battle = AppleBattle(
            player1_id=first.user_id,
            player2_id=second.user_id,
            player1_card_id=first.card_id,
            player2_card_id=second.card_id,
            level=first.level,
            status=AppleBattleStatus.PENDING,
            matched_at=now,
            expires_at=now + timedelta(hours=1),
        )
        session.add(battle)
        await session.flush()
        log_event(
            Events.APPLE_BATTLE_EVENT,
            {"action": "matched", "battle_id": battle.id, "p1": first.user_id, "p2": second.user_id},
        )
        return battle

    # ------------------------------------------------------------------
    # Controlled result computation (precomputed + persisted)
    # ------------------------------------------------------------------
    @classmethod
    async def compute_and_persist_result(cls, session: AsyncSession, battle_id: int) -> AppleBattle:
        battle = await session.get(AppleBattle, battle_id)
        if not battle:
            raise BattleError("نبرد پیدا نشد.")
        if battle.status != AppleBattleStatus.PENDING:
            return battle  # idempotent

        card1 = await session.get(PlayerCard, battle.player1_card_id)
        card2 = await session.get(PlayerCard, battle.player2_card_id)
        if not card1 or not card2:
            battle.status = AppleBattleStatus.CANCELLED
            await session.flush()
            raise BattleError("کارت یکی از بازیکنان دیگر موجود نیست.")

        char1 = await session.get(Character, card1.character_id)
        char2 = await session.get(Character, card2.character_id)

        score1 = cls._effective_score(card1, char1)
        score2 = cls._effective_score(card2, char2)

        # Controlled RNG: documented seed, persisted with the result.
        seed = uuid.uuid4().hex
        rng = random.Random(seed)
        p1_win = 1.0 / (1.0 + 10 ** ((score2 - score1) / 60.0))
        roll = rng.random()
        winner_is_p1 = roll < p1_win
        winner_id = battle.player1_id if winner_is_p1 else battle.player2_id

        balance_version = (char1.balance_version if char1 else 1) + (
            char2.balance_version if char2 else 1
        )

        result = {
            "result_version": battle.result_version,
            "winner_id": winner_id,
            "scores": {"player1": score1, "player2": score2},
            "p1_win_probability": round(p1_win, 4),
            "roll": round(roll, 6),
            "rng_seed": seed,
            "balance_version": balance_version,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

        battle.result_json = json.dumps(result, ensure_ascii=False)
        battle.rng_seed = seed
        battle.balance_version = balance_version
        battle.winner_id = winner_id
        battle.status = AppleBattleStatus.RESULT_READY
        await session.flush()

        log_event(
            Events.APPLE_BATTLE_EVENT,
            {
                "action": "result_computed",
                "battle_id": battle.id,
                "winner_id": winner_id,
                "rng_seed": seed,
                "balance_version": balance_version,
            },
        )
        return battle

    @staticmethod
    def _effective_score(card: PlayerCard, character: Optional[Character]) -> float:
        base = card.power * 1.0 + card.speed * 1.05 + card.intelligence * 0.95
        if character:
            hidden = character.hidden_modifier_params
            for key, value in hidden.items():
                if isinstance(value, (int, float)):
                    base *= 1.0 + float(value) * 0.1  # controlled, small effects
        return round(base, 2)

    # ------------------------------------------------------------------
    # Persian narration (10-15 seconds of reading)
    # ------------------------------------------------------------------
    @classmethod
    async def narrate(cls, session: AsyncSession, battle_id: int) -> str:
        battle = await session.get(AppleBattle, battle_id)
        if not battle or not battle.result_json:
            raise BattleError("نتیجه‌ای برای روایت وجود ندارد.")

        result = json.loads(battle.result_json)
        card1 = await session.get(PlayerCard, battle.player1_card_id)
        card2 = await session.get(PlayerCard, battle.player2_card_id)
        char1 = await session.get(Character, card1.character_id) if card1 else None
        char2 = await session.get(Character, card2.character_id) if card2 else None
        name1 = char1.name_fa if char1 else "جنگجوی ناشناس"
        name2 = char2.name_fa if char2 else "جنگجوی ناشناس"

        winner_id = result["winner_id"]
        winner_name = name1 if winner_id == battle.player1_id else name2
        loser_name = name2 if winner_id == battle.player1_id else name1

        rng = random.Random(f"narrative:{battle.rng_seed}")
        openings = [
            "در دل باغ‌های افسانه‌ای سیب، دو جنگجو رودرروی هم ایستادند...",
            "هوا سنگین بود و برگ‌ها در سکوت به تماشا ایستاده بودند...",
            "شیپور نبرد نواخته شد و زمین لرزید...",
        ]
        middles = [
            f"{name1} با تمام قدرت حمله کرد و {name2} با هوشمندی ضربه را دفع کرد؛ اما طوفان ادامه یافت.",
            f"ضربه‌ای مهیب میان {name1} و {name2} رد و بدل شد؛ شکوفه‌های سیب در هوا پرپر شدند.",
            f"لحظه‌ای نفس‌گیر فرا رسید؛ {name1} و {name2} هیچ‌کدام کوتاه نیامدند...",
        ]
        endings = [
            f"و سرانجام، {winner_name} با یک ضربه‌ی نهایی، پیروز میدان شد! 🍎",
            f"در آخرین ثانیه، {winner_name} بر {loser_name} غلبه کرد و کلاه‌پیروزی را بر سر گذاشت! 🏆",
            f"پیروزی از آنِ {winner_name} شد؛ تماشاگران با شور و شادی فریاد زدند! 🎉",
        ]
        story = " ".join(
            [rng.choice(openings), rng.choice(middles), rng.choice(endings)]
        )
        stats_line = (
            f"\n\n📊 امتیاز نبرد: {name1} «{result['scores']['player1']}» — "
            f"{name2} «{result['scores']['player2']}»"
        )
        return story + stats_line

    # ------------------------------------------------------------------
    # Completion + seedling award (idempotent)
    # ------------------------------------------------------------------
    @classmethod
    async def complete(cls, session: AsyncSession, battle_id: int) -> AppleBattle:
        battle = await session.get(AppleBattle, battle_id)
        if not battle:
            raise BattleError("نبرد پیدا نشد.")
        if battle.status == AppleBattleStatus.COMPLETED:
            return battle  # idempotent
        if battle.status != AppleBattleStatus.RESULT_READY:
            raise BattleError("نتیجه هنوز آماده نیست.")

        winner_id = battle.winner_id
        if winner_id is None:
            raise BattleError("برنده مشخص نیست.")

        awarded = False
        if battle.seedlings_awarded == 0:
            awarded = await cls._award_seedling(session, battle.id, winner_id)
            battle.seedlings_awarded = 1

        battle.status = AppleBattleStatus.COMPLETED
        battle.completed_at = datetime.now(timezone.utc)
        await session.flush()

        log_event(
            Events.APPLE_BATTLE_EVENT,
            {"action": "completed", "battle_id": battle.id, "winner_id": winner_id, "seedling": awarded},
        )
        return battle

    @staticmethod
    async def _award_seedling(session: AsyncSession, battle_id: int, winner_id: int) -> bool:
        """+1 seedling to the winner, idempotent per (battle, user)."""
        existing = await session.scalar(
            select(SeedlingAward.id).where(
                SeedlingAward.source == "battle",
                SeedlingAward.source_ref_id == battle_id,
                SeedlingAward.user_id == winner_id,
            )
        )
        if existing is not None:
            return False

        inv = await session.scalar(
            select(SeedlingInventory)
            .where(SeedlingInventory.user_id == winner_id)
            .with_for_update()
        )
        if not inv:
            inv = SeedlingInventory(user_id=winner_id, total=0)
            session.add(inv)
            await session.flush()
        inv.total += 1
        session.add(
            SeedlingAward(
                match_id=None,
                user_id=winner_id,
                amount=1,
                reason="apple_battle_win",
                source="battle",
                source_ref_id=battle_id,
            )
        )
        await session.flush()
        return True

    # ------------------------------------------------------------------
    # Leaderboard (separate from group leaderboard)
    # ------------------------------------------------------------------
    @classmethod
    async def battle_leaderboard(cls, session: AsyncSession, limit: int = 20) -> List[Tuple[int, int, int]]:
        """Return (user_id, wins, battles) sorted by wins desc."""
        from sqlalchemy import func

        wins = (
            await session.execute(
                select(
                    AppleBattle.winner_id,
                    func.count(AppleBattle.id).label("wins"),
                )
                .where(AppleBattle.status == AppleBattleStatus.COMPLETED)
                .where(AppleBattle.winner_id.is_not(None))
                .group_by(AppleBattle.winner_id)
                .order_by(func.count(AppleBattle.id).desc())
                .limit(limit)
            )
        ).all()
        battles = dict(
            (
                await session.execute(
                    select(AppleBattle.player1_id, func.count(AppleBattle.id))
                    .where(AppleBattle.status == AppleBattleStatus.COMPLETED)
                    .group_by(AppleBattle.player1_id)
                )
            ).all()
        )
        battles2 = dict(
            (
                await session.execute(
                    select(AppleBattle.player2_id, func.count(AppleBattle.id))
                    .where(AppleBattle.status == AppleBattleStatus.COMPLETED)
                    .group_by(AppleBattle.player2_id)
                )
            ).all()
        )
        result = []
        for user_id, win_count in wins:
            total = battles.get(user_id, 0) + battles2.get(user_id, 0)
            result.append((user_id, int(win_count), int(total)))
        return result
