"""Apple Battle (نبرد سیبی) — staged narrative battles on SHARED matchmaking.

Audit finding (2026-09-27): the original implementation built its OWN queue
(``AppleBattleQueueEntry``) and its OWN pairing (``try_match``/``_create_battle``),
duplicating the matchmaking service. Per the redesign decision Apple Battle now
uses the REAL shared matchmaking pipeline end to end:

    pick card (loadout) → solo Party → MatchmakingService.enqueue_party
    → find_and_create_match (cross-group, atomic) → shared Match + acceptance
    → precomputed battle plan in match.game_state → staged narrative
    → MatchResultService.apply_match_result (seedling/stats/parties)
    → +5 apples for the winner → cleanup

The win formula is UNCHANGED (locked decision):

    effective = (Power + 1.05*Speed + 0.95*Intelligence)
                * product(1 + hidden_param*0.1)      [hidden character mods]
    p(player1 wins) = 1 / (1 + 10 ** ((score2 - score1) / 60))
    winner = seeded persisted RNG draw against p

The outcome is computed and persisted (in ``match.game_state``) BEFORE any
narration and never changes after the fact. Narration only advances the plan
cursor; the result is fixed.
"""

import asyncio
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.logging import Events, log_event, logger
from sib.database.models import (
    AppleBattle,
    AppleBattleQueueEntry,
    AppleBattleStatus,
    Character,
    Match,
    MatchPlayer,
    MatchState,
    PlayerCard,
    SeedlingAward,
    SeedlingInventory,
)
from sib.database.connection import get_session


class BattleError(ValueError):
    """Apple Battle domain error (kept for compatibility)."""


class AppleBattleService:
    """Precomputed Apple Battle logic + shared-match presentation."""

    # ------------------------------------------------------------------
    # Card loadout ("queue entry" table reused as a persistent selection)
    # ------------------------------------------------------------------
    @classmethod
    async def set_loadout(
        cls,
        session,
        user_id: int,
        card_id: int,
    ) -> AppleBattleQueueEntry:
        """Select the battle card (exactly one loadout per user).

        Ownership is enforced here (root cause): a user can never load
        someone else's card. Re-selecting replaces the loadout (upsert).
        """
        card = await session.get(PlayerCard, card_id)
        if card is None or card.user_id != user_id:
            raise BattleError("کارت موردنظر متعلق به شما نیست.")
        entry = await session.scalar(
            select(AppleBattleQueueEntry).where(AppleBattleQueueEntry.user_id == user_id)
        )
        now = datetime.now(timezone.utc)
        if entry:
            entry.card_id = card.id
            entry.level = card.level
            entry.queued_at = now
        else:
            entry = AppleBattleQueueEntry(
                user_id=user_id, card_id=card.id, level=card.level, queued_at=now
            )
            session.add(entry)
        await session.flush()
        return entry

    # Kept name for compatibility; this is now the loadout setter.
    enqueue = set_loadout

    @classmethod
    async def get_loadout(cls, session, user_id: int) -> Optional[AppleBattleQueueEntry]:
        return await session.scalar(
            select(AppleBattleQueueEntry).where(AppleBattleQueueEntry.user_id == user_id)
        )

    @classmethod
    async def dequeue(cls, session, user_id: int) -> bool:
        """Clear the selected card (explicit user action only)."""
        entry = await session.scalar(
            select(AppleBattleQueueEntry).where(AppleBattleQueueEntry.user_id == user_id)
        )
        if not entry:
            return False
        await session.delete(entry)
        await session.flush()
        return True

    # ------------------------------------------------------------------
    # Precomputed plan (formula UNCHANGED)
    # ------------------------------------------------------------------
    @staticmethod
    def _effective_score(card: PlayerCard, character: Optional[Character]) -> float:
        base = card.power * 1.0 + card.speed * 1.05 + card.intelligence * 0.95
        if character:
            hidden = character.hidden_modifier_params
            for key, value in hidden.items():
                if isinstance(value, (int, float)):
                    base *= 1.0 + float(value) * 0.1  # controlled, small effects
        return round(base, 2)

    @classmethod
    def _compose_plan(
        cls,
        *,
        player1_id: int,
        player2_id: int,
        name1: str,
        name2: str,
        card_name1: str,
        card_name2: str,
        level: int,
        winner_id: int,
        seed: str,
    ) -> List[Dict[str, Any]]:
        """Deterministic staged narrative beats (seeded, never changes outcome)."""
        rng = random.Random(f"plan:{seed}")
        winner_name = name1 if winner_id == player1_id else name2
        winner_card = card_name1 if winner_id == player1_id else card_name2
        loser_name = name2 if winner_id == player1_id else name1

        intros = [
            "در دل باغ‌های افسانه‌ای سیب، دو جنگجو رودرروی هم ایستادند...",
            "هوا سنگین بود و برگ‌ها در سکوت به تماشا ایستاده بودند...",
            "شیپور نبرد نواخته شد و زمین لرزید...",
        ]
        rounds_a = [
            f"{name1} با تمام قدرت حمله کرد و {name2} با هوشمندی ضربه را دفع کرد؛ اما طوفان ادامه یافت.",
            f"ضربه‌ای مهیب میان {name1} و {name2} رد و بدل شد؛ شکوفه‌های سیب در هوا پرپر شدند.",
            f"لحظه‌ای نفس‌گیر فرا رسید؛ {name1} و {name2} هیچ‌کدام کوتاه نیامدند...",
        ]
        rounds_b = [
            f"{name2} موضع گرفت و {name1} با چرخشی سریع از کنار ضربه گذشت؛ تماشاگران میخکوب شدند.",
            f"صدای برخورد دو کارت پیچید؛ {name1} و {name2} همچنان ایستاده بودند.",
            f"نفس‌ها در سینه حبس شد؛ {name1} و {name2} برای آخرین فرصت آماده می‌شدند...",
        ]
        rounds_c = [
            f"زمین نبرد لرزید؛ {name1} و {name2} آخرین نیروی خود را جمع کردند.",
            f"برگ‌های سیب باران شد؛ {name1} و {name2} در میان طوفان جنگیدند.",
            f"لحظه‌ی سرنوشت نزدیک می‌شد؛ {name1} در برابر {name2}...",
        ]
        endings = [
            f"و سرانجام، {winner_name} با یک ضربه‌ی نهایی، پیروز میدان شد! 🍎",
            f"در آخرین ثانیه، {winner_name} بر {loser_name} غلبه کرد و کلاه‌پیروزی را بر سر گذاشت! 🏆",
            f"پیروزی از آنِ {winner_name} شد؛ تماشاگران با شور و شادی فریاد زدند! 🎉",
        ]

        intro_text = (
            "🍎 **نبرد سیبی آغاز شد!** 🍎\n\n"
            f"⚔️ {name1} با **{card_name1}** (سطح {level})\n"
            f"⚔️ {name2} با **{card_name2}** (سطح {level})\n\n"
            f"{rng.choice(intros)}"
        )
        finale_text = (
            f"{rng.choice(endings)}\n\n"
            f"🏆 **برنده: {winner_name}** با **{winner_card}**\n"
            "🎁 پاداش پیروزی: ۱ نهال 🌱 + ۵ سیب 🍏"
        )
        return [
            {"phase": "intro", "text": intro_text},
            {"phase": "round", "round": 1, "text": rng.choice(rounds_a)},
            {"phase": "round", "round": 2, "text": rng.choice(rounds_b)},
            {"phase": "round", "round": 3, "text": rng.choice(rounds_c)},
            {"phase": "finale", "text": finale_text},
        ]

    @classmethod
    async def build_match_state(cls, session, match: Match) -> Optional[Dict[str, Any]]:
        """Build the FULL precomputed engine state for a freshly found match.

        Cards come from both players' loadouts. The outcome is computed with
        the UNCHANGED formula and seeded RNG, and returned as ``match.game_state``
        content — persisted at match-found time, BEFORE any narration.
        Returns None when a loadout/card is missing.
        """
        players = list(match.players)
        if len(players) != 2:
            return None

        players_map: Dict[str, Any] = {}
        cards_map: Dict[str, Any] = {}
        loadouts: Dict[int, Optional[AppleBattleQueueEntry]] = {}
        card_objs: Dict[int, Optional[PlayerCard]] = {}
        char_objs: Dict[int, Optional[Character]] = {}

        for p in players:
            name = (
                p.user.display_name
                if getattr(p, "user", None) and p.user.display_name
                else f"بازیکن {p.user_id}"
            )
            players_map[str(p.user_id)] = {
                "name": name,
                "group_id": p.group_id,
                "team": p.team,
            }
            loadout = await cls.get_loadout(session, p.user_id)
            loadouts[p.user_id] = loadout
            card = await session.get(PlayerCard, loadout.card_id) if loadout else None
            card_objs[p.user_id] = card
            char_objs[p.user_id] = (
                await session.get(Character, card.character_id) if card else None
            )
            if not card:
                return None
            cards_map[str(p.user_id)] = {
                "card_id": card.id,
                "name": char_objs[p.user_id].name_fa
                if char_objs[p.user_id]
                else "جنگجوی ناشناس",
                "power": card.power,
                "speed": card.speed,
                "intelligence": card.intelligence,
                "level": card.level,
            }

        p1 = min(players, key=lambda p: p.team)
        p2 = max(players, key=lambda p: p.team)
        c1, ch1 = card_objs[p1.user_id], char_objs[p1.user_id]
        c2, ch2 = card_objs[p2.user_id], char_objs[p2.user_id]

        score1 = cls._effective_score(c1, ch1)
        score2 = cls._effective_score(c2, ch2)

        # Controlled RNG: documented seed, persisted with the result.
        seed = uuid.uuid4().hex
        rng = random.Random(seed)
        p1_win = 1.0 / (1.0 + 10 ** ((score2 - score1) / 60.0))
        roll = rng.random()
        winner_is_p1 = roll < p1_win
        winner_id = p1.user_id if winner_is_p1 else p2.user_id
        loser_id = p2.user_id if winner_is_p1 else p1.user_id

        balance_version = (ch1.balance_version if ch1 else 1) + (
            ch2.balance_version if ch2 else 1
        )

        result = {
            "result_version": 1,
            "winner_id": winner_id,
            "scores": {"player1": score1, "player2": score2},
            "p1_win_probability": round(p1_win, 4),
            "roll": round(roll, 6),
            "rng_seed": seed,
            "balance_version": balance_version,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

        plan = cls._compose_plan(
            player1_id=p1.user_id,
            player2_id=p2.user_id,
            name1=players_map[str(p1.user_id)]["name"],
            name2=players_map[str(p2.user_id)]["name"],
            card_name1=cards_map[str(p1.user_id)]["name"],
            card_name2=cards_map[str(p2.user_id)]["name"],
            level=c1.level,
            winner_id=winner_id,
            seed=seed,
        )

        state = {
            "players": players_map,
            "cards": cards_map,
            "battle": {
                "winner_id": winner_id,
                "loser_id": loser_id,
                "win_probability": round(p1_win, 4),
                "seed": seed,
                "balance_version": balance_version,
                "summary": f"نبرد سیبی با پیروزی {players_map[str(winner_id)]['name']} پایان یافت.",
                "result": result,
            },
            "plan": plan,
            "cursor": 1,  # intro is visible from the very first render
            "current_player_id": None,
        }

        # Loadouts are consumed by this battle.
        for uid in loadouts:
            if loadouts[uid] is not None:
                await session.delete(loadouts[uid])
        await session.flush()
        return state

    # ------------------------------------------------------------------
    # Battle record (leaderboard history) + match finalization
    # ------------------------------------------------------------------
    @classmethod
    async def record_battle(cls, session, match: Match, state: Dict[str, Any]) -> AppleBattle:
        """Create (idempotent per match) the AppleBattle history row."""
        existing = await session.scalar(
            select(AppleBattle).where(AppleBattle.match_id == match.id)
        )
        if existing:
            return existing

        battle = state["battle"]
        cards = state["cards"]
        team_ids = sorted(
            state["players"].keys(), key=lambda k: state["players"][k]["team"]
        )
        p1_id, p2_id = int(team_ids[0]), int(team_ids[1])
        level = int(cards[str(p1_id)]["level"])

        row = AppleBattle(
            player1_id=p1_id,
            player2_id=p2_id,
            player1_card_id=int(cards[str(p1_id)]["card_id"]),
            player2_card_id=int(cards[str(p2_id)]["card_id"]),
            level=level,
            match_id=match.id,
            status=AppleBattleStatus.RESULT_READY,
            winner_id=battle["winner_id"],
            result_json=json.dumps(battle["result"], ensure_ascii=False),
            rng_seed=battle["seed"],
            balance_version=battle["balance_version"],
            matched_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        session.add(row)
        await session.flush()
        return row

    @classmethod
    async def finalize_match(
        cls,
        session,
        match: Match,
        battle_id: int,
    ) -> None:
        """Finish the shared match + award the winner (idempotent).

        - Outcome/statistics/seedling/party release go through the SHARED
          ``MatchResultService.apply_match_result`` (exactly ONE seedling via
          the normal match-win ledger).
        - Winner additionally receives +5 apples (idempotent per battle).
        - Loser receives nothing (locked decision).
        """
        from sib.games.apple_battle.engine import AppleBattleEngine
        from sib.services.apple_wallet import AppleWalletService
        from sib.services.match_result import MatchResultService

        state = json.loads(match.game_state or "{}")
        engine = AppleBattleEngine()
        result = engine.get_result(state)

        await MatchResultService.apply_match_result(session, match, result)

        battle = await session.get(AppleBattle, battle_id)
        if battle and battle.status != AppleBattleStatus.COMPLETED:
            battle.status = AppleBattleStatus.COMPLETED
            battle.completed_at = datetime.now(timezone.utc)
            battle.seedlings_awarded = 1
            await session.flush()

        winner_id = state.get("battle", {}).get("winner_id")
        if winner_id:
            await AppleWalletService.earn(
                session,
                user_id=int(winner_id),
                amount=5,
                reason="apple_battle_win",
                ref_type="apple_battle",
                ref_id=battle_id,
                idempotency_key=f"apple_battle_win:{battle_id}",
            )
            log_event(
                Events.APPLE_BATTLE_EVENT,
                {
                    "action": "match_completed",
                    "battle_id": battle_id,
                    "match_id": match.id,
                    "winner_id": winner_id,
                },
            )

    # ------------------------------------------------------------------
    # Staged narration runner (asyncio task; never blocks handlers)
    # ------------------------------------------------------------------
    @classmethod
    async def run_narration(cls, bot, match_id: int) -> None:
        """Advance the battle plan beat by beat (~3s), then finalize."""
        from sib.games.apple_battle.engine import BEAT_DELAY_SECONDS

        try:
            await cls._narration_loop(bot, match_id, BEAT_DELAY_SECONDS)
        except Exception as e:  # pragma: no cover - defensive
            logger.exception(f"Apple battle narration failed for match {match_id}: {e}")

    @classmethod
    async def _narration_loop(cls, bot, match_id: int, beat_delay: float) -> None:
        from sib.games.apple_battle.engine import AppleBattleEngine

        engine = AppleBattleEngine()
        battle_id: Optional[int] = None

        while True:
            await asyncio.sleep(beat_delay)

            edit_targets: List[Tuple[int, int]] = []
            texts: Dict[int, str] = {}
            finished = False

            async with get_session() as session:
                match = await session.scalar(
                    select(Match)
                    .where(Match.id == match_id)
                    .options(selectinload(Match.players).selectinload(MatchPlayer.user))
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                if (
                    not match
                    or match.state != MatchState.ACTIVE
                    or not match.game_state
                ):
                    return

                state = json.loads(match.game_state)
                if battle_id is None:
                    battle = await cls.record_battle(session, match, state)
                    battle_id = battle.id

                if engine.is_finished(state):
                    await cls.finalize_match(session, match, battle_id)
                    finished = True
                else:
                    state = engine.apply_action(state, 0, {"type": "advance"})
                    match.game_state = json.dumps(state, ensure_ascii=False)
                    if engine.is_finished(state):
                        # Last beat just became visible; finalize next round
                        # so viewers see the finale first.
                        pass
                    await session.flush()

                for group_id, msg_id in (
                    (match.group1_id, match.group1_message_id),
                    (match.group2_id, match.group2_message_id),
                ):
                    if group_id and msg_id:
                        edit_targets.append((group_id, msg_id))
                        texts[group_id] = engine.render_for_group(
                            state, group_id, None
                        )

            await cls._edit_boards(bot, edit_targets, texts, engine, match_id, finished)

            if finished:
                return

    @staticmethod
    async def _winner_photo(match_id: int):
        """Winner's framed card art for the finale swap (None when unavailable)."""
        try:
            from sib.database.connection import get_session
            from sib.services.character_art import photo_arg, artwork_path

            async with get_session() as session:
                battle = await session.get(AppleBattle, match_id)
                if not battle or battle.status != AppleBattleStatus.COMPLETED:
                    return None
                winner_card_id = None
                if battle.winner_id == battle.player1_id:
                    winner_card_id = battle.player1_card_id
                elif battle.winner_id == battle.player2_id:
                    winner_card_id = battle.player2_card_id
                card = await session.get(PlayerCard, winner_card_id) if winner_card_id else None
                character = await session.get(Character, card.character_id) if card else None
                if character is None:
                    return None
                path = artwork_path(character)
                return photo_arg(None, path)
        except Exception:
            return None

    @classmethod
    async def _edit_boards(
        cls,
        bot,
        edit_targets: List[Tuple[int, int]],
        texts: Dict[int, str],
        engine,
        match_id: int,
        finished: bool,
    ) -> None:
        from sib.services.game_sync import final_keyboard
        from sib.services.telegram_ui import safe_edit_screen

        winner_photo = await cls._winner_photo(match_id) if finished else None
        replaced: List[Tuple[int, int]] = []
        for group_id, msg_id in edit_targets:
            kb = final_keyboard() if finished else engine.get_keyboard(
                {}, match_id, 0
            )
            new_msg_id = await safe_edit_screen(
                bot,
                group_id,
                msg_id,
                texts[group_id],
                kb,
                parse_mode="Markdown",
                photo=winner_photo,
            )
            if new_msg_id is None:
                logger.warning(
                    f"Apple battle board edit failed for group {group_id}"
                )
            elif new_msg_id != msg_id:
                # The renderer replaced the message (text↔photo): persist
                # the LIVE message id so no stale id lingers in the match.
                replaced.append((group_id, new_msg_id))
        if replaced:
            try:
                from sib.database.connection import get_session
                from sib.database.models import Match

                async with get_session() as session:
                    match = await session.get(Match, match_id)
                    if match is not None:
                        for group_id, new_msg_id in replaced:
                            if group_id == match.group1_id:
                                match.group1_message_id = new_msg_id
                            elif group_id == match.group2_id:
                                match.group2_message_id = new_msg_id
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    f"Could not persist replaced board ids for match {match_id}: {exc}"
                )

    # ------------------------------------------------------------------
    # Legacy precompute + narration + completion (kept, idempotent)
    # ------------------------------------------------------------------
    @classmethod
    async def compute_and_persist_result(cls, session, battle_id: int) -> AppleBattle:
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
    # Completion + rewards (idempotent)
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

        # Winner reward: +5 apples (idempotent per battle). Loser: nothing.
        from sib.services.apple_wallet import AppleWalletService

        await AppleWalletService.earn(
            session,
            user_id=winner_id,
            amount=5,
            reason="apple_battle_win",
            ref_type="apple_battle",
            ref_id=battle.id,
            idempotency_key=f"apple_battle_win:{battle.id}",
        )

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

        battles = (
            await session.execute(
                select(
                    AppleBattle.player1_id,
                    func.count(AppleBattle.id).label("battles"),
                )
                .where(AppleBattle.status == AppleBattleStatus.COMPLETED)
                .group_by(AppleBattle.player1_id)
            )
        ).all()
        battles2 = (
            await session.execute(
                select(
                    AppleBattle.player2_id,
                    func.count(AppleBattle.id).label("battles"),
                )
                .where(AppleBattle.status == AppleBattleStatus.COMPLETED)
                .group_by(AppleBattle.player2_id)
            )
        ).all()

        battle_count: Dict[int, int] = {}
        for uid, cnt in battles:
            battle_count[uid] = battle_count.get(uid, 0) + cnt
        for uid, cnt in battles2:
            battle_count[uid] = battle_count.get(uid, 0) + cnt

        return [(uid, w, battle_count.get(uid, 0)) for uid, w in wins]
