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

Phase 2A — cinematic narration delivery: every plan beat is sent as its OWN
temporary Telegram message (photo + short caption) by
``sib.services.apple_battle_narration``, and the temporary messages are
deleted when the battle ends. The board message is never used to accumulate
the story any more; it becomes the UNCHANGED final report once the plan is
complete (see ``_narration_loop``).
"""

import asyncio
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.logging import Events, log_event, logger
from sib.database.models import (
    AppleBattle,
    AppleBattleQueueEntry,
    AppleBattleStatus,
    AppleTransaction,
    Character,
    Game,
    Group,
    Match,
    MatchPlayer,
    MatchState,
    PlayerCard,
    SeedlingAward,
    SeedlingInventory,
)
from sib.database.connection import get_session
from sib.services.card_progression import (
    CardAttributes,
    attributes_at_level,
    card_attributes,
)


class BattleError(ValueError):
    """Apple Battle domain error (kept for compatibility)."""


class AppleBattleService:
    """Precomputed Apple Battle logic + shared-match presentation."""

    _ACTIVE_NARRATION_TASKS: Dict[int, asyncio.Task] = {}
    _NARRATION_RUNNING: set[int] = set()

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
    def _card_stats(card: PlayerCard, character: Optional[Character]) -> CardAttributes:
        """Attributes of an owned card: character base + level progression."""
        if character is None:
            # Orphaned card (cannot happen with FK-backed data): neutral base.
            return attributes_at_level(CardAttributes(20, 20, 20), card.level)
        return card_attributes(card, character)

    @classmethod
    def _effective_score(cls, card: PlayerCard, character: Optional[Character]) -> float:
        stats = cls._card_stats(card, character)
        return cls._score_for_stats(
            stats.power, stats.speed, stats.intelligence, character
        )

    @staticmethod
    def _score_for_stats(
        power: int, speed: int, intelligence: int, character: Optional[Character]
    ) -> float:
        """The (unchanged) effective-score formula for explicit attributes."""
        base = power * 1.0 + speed * 1.05 + intelligence * 0.95
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
        winner_id: int,
        seed: str,
        level: int = 1,
        level1: Optional[int] = None,
        level2: Optional[int] = None,
        is_bot_match: bool = False,
    ) -> List[Dict[str, Any]]:
        """Deterministic staged narrative beats (seeded, never changes outcome)."""
        rng = random.Random(f"plan:{seed}")
        winner_name = name1 if winner_id == player1_id else name2
        winner_card = card_name1 if winner_id == player1_id else card_name2
        loser_name = name2 if winner_id == player1_id else name1
        lvl1 = level1 if level1 is not None else level
        lvl2 = level2 if level2 is not None else level

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
            f"⚔️ {name1} با **{card_name1}** (سطح {lvl1})\n"
            f"⚔️ {name2} با **{card_name2}** (سطح {lvl2})\n\n"
            f"{rng.choice(intros)}"
        )
        reward_line = (
            "🎁 پاداش پیروزی: ۵ سیب 🍏"
            if is_bot_match
            else "🎁 پاداش پیروزی: ۱ نهال 🌱 + ۵ سیب 🍏"
        )
        finale_text = (
            f"{rng.choice(endings)}\n\n"
            f"🏆 **برنده: {winner_name}** با **{winner_card}**\n"
            f"{reward_line}"
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

        from sib.services.apple_battle_scenes import AppleBattleSceneLibrary

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
            stats = cls._card_stats(card, char_objs[p.user_id])
            cards_map[str(p.user_id)] = {
                "card_id": card.id,
                "name": char_objs[p.user_id].name_fa
                if char_objs[p.user_id]
                else "جنگجوی ناشناس",
                # Phase 2B: narration scene-library cast key (e.g. "crimson").
                # Persisted with the precomputed state so scene selection is
                # restart-safe and needs no extra lookup for new battles.
                "cast": AppleBattleSceneLibrary.cast_key_for_code(
                    getattr(char_objs[p.user_id], "code", None)
                    if char_objs[p.user_id]
                    else None
                ),
                "power": stats.power,
                "speed": stats.speed,
                "intelligence": stats.intelligence,
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
            level1=c1.level,
            level2=c2.level,
            winner_id=winner_id,
            seed=seed,
            is_bot_match=bool(getattr(match, "is_bot_match", False)),
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
            # Phase 2A: every beat — including the intro — is delivered as its
            # OWN temporary narration message, so narration starts at 0. The
            # board message is no longer used to accumulate the story; it only
            # becomes the unchanged final report when the battle ends.
            "cursor": 0,
            "current_player_id": None,
        }

        # Loadouts are consumed by this battle.
        for uid in loadouts:
            if loadouts[uid] is not None:
                await session.delete(loadouts[uid])
        await session.flush()
        return state

    # ------------------------------------------------------------------
    # Persisted-result inspection & recovery helpers
    # ------------------------------------------------------------------
    @classmethod
    async def is_apple_battle_match(cls, session: AsyncSession, match: Match) -> bool:
        """Return True if ``match`` is an Apple Battle match."""
        game = match.__dict__.get("game")
        if game is not None and getattr(game, "code", None):
            return game.code == "apple_battle"
        if match.game_id:
            code = await session.scalar(
                select(Game.code).where(Game.id == match.game_id)
            )
            if code == "apple_battle":
                return True
        if match.id:
            linked = await session.scalar(
                select(AppleBattle.id)
                .where(AppleBattle.match_id == match.id)
                .limit(1)
            )
            if linked is not None:
                return True
        return False

    @classmethod
    async def _rehydrate_players_map(
        cls,
        session: AsyncSession,
        match: Match,
    ) -> Dict[str, Dict[str, Any]]:
        players = list(match.__dict__.get("players") or [])
        if not players and match.id:
            rows = (
                await session.scalars(
                    select(MatchPlayer)
                    .where(MatchPlayer.match_id == match.id)
                    .options(selectinload(MatchPlayer.user))
                )
            ).all()
            players = list(rows)
        players_map: Dict[str, Dict[str, Any]] = {}
        for p in sorted(players, key=lambda mp: mp.team):
            user_obj = p.__dict__.get("user")
            name = (
                user_obj.display_name
                if user_obj and getattr(user_obj, "display_name", None)
                else f"بازیکن {p.user_id}"
            )
            players_map[str(p.user_id)] = {
                "name": name,
                "group_id": p.group_id,
                "team": p.team,
            }
        return players_map

    @classmethod
    async def extract_persisted_state(
        cls,
        session: AsyncSession,
        match: Match,
    ) -> Optional[Dict[str, Any]]:
        """Return persisted Apple Battle state with its fixed winner, or None if no result exists.

        Never recomputes or randomizes the battle result. If ``match.game_state``
        already contains ``battle.winner_id``, it is returned (filling in any
        missing structural fields deterministically from persisted rows). If
        ``match.game_state`` lacks a result, falls back to an existing
        ``AppleBattle`` row linked by ``AppleBattle.match_id == match.id`` with
        a non-null ``winner_id``.
        """
        raw_state: Dict[str, Any] = {}
        if match.game_state:
            try:
                parsed = json.loads(match.game_state)
                if isinstance(parsed, dict):
                    raw_state = parsed
            except Exception:
                raw_state = {}

        battle_block = raw_state.get("battle")
        winner_id = (
            battle_block.get("winner_id")
            if isinstance(battle_block, dict)
            else None
        )

        battle_row: Optional[AppleBattle] = None
        if match.id:
            battle_row = await session.scalar(
                select(AppleBattle)
                .where(AppleBattle.match_id == match.id)
                .order_by(AppleBattle.id.asc())
                .limit(1)
            )

        if winner_id is None:
            if battle_row is None or battle_row.winner_id is None:
                return None
            winner_id = battle_row.winner_id

        winner_id = int(winner_id)
        players_map = raw_state.get("players")
        if not isinstance(players_map, dict) or not players_map:
            players_map = await cls._rehydrate_players_map(session, match)
        if not players_map and battle_row is not None:
            players_map = {
                str(battle_row.player1_id): {
                    "name": f"بازیکن {battle_row.player1_id}",
                    "group_id": match.group1_id,
                    "team": 1,
                },
                str(battle_row.player2_id): {
                    "name": f"بازیکن {battle_row.player2_id}",
                    "group_id": match.group2_id,
                    "team": 2,
                },
            }

        cards_map = raw_state.get("cards")
        if not isinstance(cards_map, dict) or not cards_map:
            cards_map = {}
            if battle_row is not None:
                for uid, cid in (
                    (battle_row.player1_id, battle_row.player1_card_id),
                    (battle_row.player2_id, battle_row.player2_card_id),
                ):
                    card = await session.get(PlayerCard, cid) if cid else None
                    char = (
                        await session.get(Character, card.character_id)
                        if card
                        else None
                    )
                    if card:
                        stats = cls._card_stats(card, char)
                        cards_map[str(uid)] = {
                            "card_id": card.id,
                            "name": char.name_fa if char else "جنگجوی ناشناس",
                            "power": stats.power,
                            "speed": stats.speed,
                            "intelligence": stats.intelligence,
                            "level": card.level,
                        }
                    else:
                        cards_map[str(uid)] = {
                            "card_id": cid or 0,
                            "name": "جنگجوی ناشناس",
                            "power": 10,
                            "speed": 10,
                            "intelligence": 10,
                            "level": battle_row.level or 1,
                        }

        team_ids = sorted(
            players_map.keys(),
            key=lambda k: players_map[k].get("team", 1),
        )
        p1_id = int(team_ids[0]) if len(team_ids) >= 1 else (
            battle_row.player1_id if battle_row else winner_id
        )
        p2_id = int(team_ids[1]) if len(team_ids) >= 2 else (
            battle_row.player2_id if battle_row else winner_id
        )
        loser_id = p2_id if winner_id == p1_id else p1_id

        if not isinstance(battle_block, dict):
            battle_block = {}
        seed = (
            battle_block.get("seed")
            or (battle_row.rng_seed if battle_row else None)
            or "recovered-seed"
        )
        balance_version = int(
            battle_block.get("balance_version")
            or (battle_row.balance_version if battle_row else 1)
            or 1
        )
        result_payload = battle_block.get("result")
        if not isinstance(result_payload, dict):
            if battle_row and battle_row.result_json:
                try:
                    result_payload = json.loads(battle_row.result_json)
                except Exception:
                    result_payload = {"winner_id": winner_id, "rng_seed": seed}
            else:
                result_payload = {"winner_id": winner_id, "rng_seed": seed}

        winner_name = (
            players_map.get(str(winner_id), {}).get("name")
            or f"بازیکن {winner_id}"
        )
        battle_data = {
            **battle_block,
            "winner_id": winner_id,
            "loser_id": battle_block.get("loser_id", loser_id),
            "seed": seed,
            "balance_version": balance_version,
            "summary": battle_block.get(
                "summary",
                f"نبرد سیبی با پیروزی {winner_name} پایان یافت.",
            ),
            "result": result_payload,
        }

        plan = raw_state.get("plan")
        if not isinstance(plan, list) or not plan:
            c1_info = cards_map.get(str(p1_id), {})
            c2_info = cards_map.get(str(p2_id), {})
            lvl1 = int(c1_info.get("level") or (battle_row.level if battle_row else 1) or 1)
            lvl2 = int(c2_info.get("level") or (battle_row.level if battle_row else 1) or 1)
            plan = cls._compose_plan(
                player1_id=p1_id,
                player2_id=p2_id,
                name1=players_map.get(str(p1_id), {}).get("name", f"بازیکن {p1_id}"),
                name2=players_map.get(str(p2_id), {}).get("name", f"بازیکن {p2_id}"),
                card_name1=c1_info.get("name", "جنگجوی ناشناس"),
                card_name2=c2_info.get("name", "جنگجوی ناشناس"),
                level=lvl1,
                level1=lvl1,
                level2=lvl2,
                winner_id=winner_id,
                seed=seed,
                is_bot_match=bool(getattr(match, "is_bot_match", False)),
            )

        if "cursor" in raw_state and raw_state["cursor"] is not None:
            cursor = int(raw_state["cursor"])
        elif battle_row and battle_row.status == AppleBattleStatus.COMPLETED:
            cursor = len(plan)
        else:
            cursor = 1

        return {
            **raw_state,
            "players": players_map,
            "cards": cards_map,
            "battle": battle_data,
            "plan": plan,
            "cursor": cursor,
            "current_player_id": None,
        }

    @classmethod
    async def has_persisted_result(cls, session: AsyncSession, match: Match) -> bool:
        """Return True if an Apple Battle result/winner has already been persisted."""
        return (await cls.extract_persisted_state(session, match)) is not None

    @classmethod
    async def finalize_persisted_match(
        cls,
        session: AsyncSession,
        match: Match,
    ) -> bool:
        """Finalize an Apple Battle match from its persisted result without recomputing."""
        state = await cls.extract_persisted_state(session, match)
        if state is None:
            return False
        match.game_state = json.dumps(state, ensure_ascii=False)
        battle = await cls.record_battle(session, match, state)
        await cls.finalize_match(session, match, battle.id)
        return True

    @classmethod
    async def recover_active_matches(
        cls,
        session: AsyncSession,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Recover active Apple Battle matches on startup.

        - If no persisted result exists: left untouched for normal timeout handling.
        - If narration is already complete (``cursor >= len(plan)``): finalized
          immediately and idempotently.
        - If narration is incomplete (``cursor < len(plan)``): preserves the
          persisted result and cursor, records the ``AppleBattle`` row if needed,
          refreshes ``turn_deadline`` so generic timeout does not kill it, and
          returns the match id so narration can be re-armed after commit.
        """
        from sib.games.apple_battle.engine import AppleBattleEngine

        if now is None:
            now = datetime.now(timezone.utc)
        engine = AppleBattleEngine()

        stmt = (
            select(Match)
            .join(Game, Match.game_id == Game.id)
            .where(
                Match.state == MatchState.ACTIVE,
                Game.code == "apple_battle",
            )
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.group1).selectinload(Group.stats),
                selectinload(Match.group2).selectinload(Group.stats),
                selectinload(Match.game),
            )
            .with_for_update()
        )
        matches = list((await session.scalars(stmt)).all())

        resumed_ids: List[int] = []
        finalized_ids: List[int] = []

        for match in matches:
            state = await cls.extract_persisted_state(session, match)
            if state is None:
                continue

            match.game_state = json.dumps(state, ensure_ascii=False)
            battle = await cls.record_battle(session, match, state)

            if engine.is_finished(state):
                await cls.finalize_match(session, match, battle.id)
                finalized_ids.append(match.id)
            else:
                match.turn_deadline = now + timedelta(
                    seconds=engine.turn_timeout_seconds
                )
                resumed_ids.append(match.id)

        await session.flush()
        return {
            "resumed": resumed_ids,
            "finalized": len(finalized_ids),
            # Phase 2A: the ids whose temporary narration messages still need
            # cleanup (deleting them is best-effort and never part of settlement).
            "finalized_ids": finalized_ids,
        }

    # ------------------------------------------------------------------
    # Battle record (leaderboard history) + match finalization
    # ------------------------------------------------------------------
    @classmethod
    async def record_battle(
        cls,
        session,
        match: Match,
        state: Optional[Dict[str, Any]] = None,
    ) -> AppleBattle:
        """Create (idempotent per match) the AppleBattle history row."""
        existing = await session.scalar(
            select(AppleBattle)
            .where(AppleBattle.match_id == match.id)
            .order_by(AppleBattle.id.asc())
            .limit(1)
            .with_for_update()
        )
        if existing:
            return existing

        if state is None:
            state = await cls.extract_persisted_state(session, match)
        if not state:
            raise BattleError("نتیجه نبرد سیبی یافت نشد.")

        battle = state["battle"]
        cards = state.get("cards") or {}
        players = state.get("players") or {}
        team_ids = sorted(
            players.keys(), key=lambda k: players[k].get("team", 1)
        )
        p1_id, p2_id = int(team_ids[0]), int(team_ids[1])
        c1_info = cards.get(str(p1_id), {})
        c2_info = cards.get(str(p2_id), {})
        level = int(c1_info.get("level", 1))

        row = AppleBattle(
            player1_id=p1_id,
            player2_id=p2_id,
            player1_card_id=int(c1_info.get("card_id", 0)),
            player2_card_id=int(c2_info.get("card_id", 0)),
            level=level,
            match_id=match.id,
            status=AppleBattleStatus.RESULT_READY,
            winner_id=int(battle["winner_id"]),
            result_json=json.dumps(battle.get("result", {}), ensure_ascii=False),
            rng_seed=str(battle.get("seed", "recovered-seed")),
            balance_version=int(battle.get("balance_version", 1)),
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
        battle_id: Optional[int] = None,
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

        state = await cls.extract_persisted_state(session, match)
        if state is None:
            state = json.loads(match.game_state or "{}")

        plan = state.get("plan")
        if isinstance(plan, list) and plan and int(state.get("cursor", 0)) < len(plan):
            state["cursor"] = len(plan)
        match.game_state = json.dumps(state, ensure_ascii=False)

        canonical_battle = await session.scalar(
            select(AppleBattle)
            .where(AppleBattle.match_id == match.id)
            .order_by(AppleBattle.id.asc())
            .limit(1)
            .with_for_update()
        )
        battle = canonical_battle
        if battle is None and battle_id is not None:
            battle = await session.get(AppleBattle, battle_id)
        if battle is None and (state.get("battle") or {}).get("winner_id") is not None:
            battle = await cls.record_battle(session, match, state)

        if battle is not None:
            battle_id = battle.id

        engine = AppleBattleEngine()
        result = engine.get_result(state)

        await MatchResultService.apply_match_result(session, match, result)

        if battle is not None:
            now_ts = datetime.now(timezone.utc)
            seedlings_awarded = 0 if match.is_bot_match else 1
            if battle.status != AppleBattleStatus.COMPLETED:
                await session.execute(
                    update(AppleBattle)
                    .where(
                        AppleBattle.id == battle.id,
                        AppleBattle.status != AppleBattleStatus.COMPLETED,
                    )
                    .values(
                        status=AppleBattleStatus.COMPLETED,
                        completed_at=now_ts,
                        seedlings_awarded=seedlings_awarded,
                    )
                )
                battle.status = AppleBattleStatus.COMPLETED
                battle.completed_at = now_ts
                battle.seedlings_awarded = seedlings_awarded
                await session.flush()

        winner_id = (state.get("battle") or {}).get("winner_id") or (
            battle.winner_id if battle is not None else None
        )
        if winner_id and battle_id is not None:
            battle_ids_for_match = list(
                (
                    await session.scalars(
                        select(AppleBattle.id).where(
                            AppleBattle.match_id == match.id
                        )
                    )
                ).all()
            )
            if battle_id not in battle_ids_for_match:
                battle_ids_for_match.append(battle_id)

            existing_apple_tx = await session.scalar(
                select(AppleTransaction.id)
                .where(
                    AppleTransaction.user_id == int(winner_id),
                    AppleTransaction.reason == "apple_battle_win",
                    AppleTransaction.ref_type == "apple_battle",
                    AppleTransaction.ref_id.in_(battle_ids_for_match),
                )
                .limit(1)
            )
            if existing_apple_tx is None:
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
    def narration_task(cls, match_id: int) -> Optional[asyncio.Task]:
        """Return the active narration task for ``match_id`` if still running."""
        task = cls._ACTIVE_NARRATION_TASKS.get(match_id)
        if task is not None and not task.done():
            return task
        return None

    @classmethod
    def is_narration_scheduled(cls, match_id: int) -> bool:
        """Return True if a narration worker is currently active for ``match_id``."""
        return (
            cls.narration_task(match_id) is not None
            or match_id in cls._NARRATION_RUNNING
        )

    @classmethod
    def schedule_narration(
        cls,
        match_id: int,
        bot: Any = None,
        *,
        beat_delay: Optional[float] = None,
    ) -> Optional[asyncio.Task]:
        """Schedule staged narration for ``match_id`` at most once concurrently."""
        existing = cls.narration_task(match_id)
        if existing is not None:
            return existing
        if match_id in cls._NARRATION_RUNNING:
            return None

        task = asyncio.create_task(
            cls.run_narration(bot, match_id, beat_delay=beat_delay)
        )
        cls._ACTIVE_NARRATION_TASKS[match_id] = task

        def _cleanup(done_task: asyncio.Task) -> None:
            if cls._ACTIVE_NARRATION_TASKS.get(match_id) is done_task:
                cls._ACTIVE_NARRATION_TASKS.pop(match_id, None)

        task.add_done_callback(_cleanup)
        return task

    @classmethod
    async def cancel_narration(cls, match_id: int) -> None:
        """Cancel the in-flight narration task for ``match_id``, if any."""
        task = cls._ACTIVE_NARRATION_TASKS.pop(match_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        cls._NARRATION_RUNNING.discard(match_id)

    @classmethod
    async def cancel_all_narrations(cls) -> None:
        """Cancel all in-flight narration tasks (used on shutdown/tests)."""
        tasks = list(cls._ACTIVE_NARRATION_TASKS.values())
        cls._ACTIVE_NARRATION_TASKS.clear()
        cls._NARRATION_RUNNING.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @classmethod
    async def run_narration(
        cls,
        bot,
        match_id: int,
        *,
        beat_delay: Optional[float] = None,
    ) -> None:
        """Advance the battle plan beat by beat (~3s), then finalize."""
        from sib.games.apple_battle.engine import BEAT_DELAY_SECONDS

        if match_id in cls._NARRATION_RUNNING:
            existing = cls.narration_task(match_id)
            current = asyncio.current_task()
            if existing is not None and existing is not current:
                try:
                    await existing
                except (asyncio.CancelledError, Exception):
                    pass
            return

        current = asyncio.current_task()
        cls._NARRATION_RUNNING.add(match_id)
        if current is not None and cls.narration_task(match_id) is None:
            cls._ACTIVE_NARRATION_TASKS[match_id] = current

        delay = BEAT_DELAY_SECONDS if beat_delay is None else beat_delay
        try:
            await cls._narration_loop(bot, match_id, delay)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            logger.exception(f"Apple battle narration failed for match {match_id}: {e}")
        finally:
            cls._NARRATION_RUNNING.discard(match_id)
            if (
                current is not None
                and cls._ACTIVE_NARRATION_TASKS.get(match_id) is current
            ):
                cls._ACTIVE_NARRATION_TASKS.pop(match_id, None)

    @staticmethod
    def _narration_targets(match: Match) -> List[Tuple[int, int]]:
        """Deduped ``(chat_id, group_no)`` targets for the live narration.

        A friendly-style match where both sides share ONE group must receive
        each beat exactly once.
        """
        targets: List[Tuple[int, int]] = []
        seen: set[int] = set()
        for group_id, group_no in (
            (match.group1_id, 1),
            (match.group2_id, 2),
        ):
            if group_id and group_id not in seen:
                seen.add(group_id)
                targets.append((group_id, group_no))
        return targets

    @classmethod
    async def _narration_loop(cls, bot, match_id: int, beat_delay: float) -> None:
        """Present the precomputed plan as SEPARATE temporary messages.

        Phase 2A: each beat is sent as its own photo + caption message (never
        appended by editing one message). The plan cursor is persisted BEFORE
        the send, so a restart resumes at the next unsent beat and can never
        duplicate a narration event. When the plan is finished the temporary
        messages are deleted (best effort) and the UNCHANGED final report is
        rendered onto the existing board message.
        """
        from sib.games.apple_battle.engine import AppleBattleEngine
        from sib.services.apple_battle_narration import AppleBattleNarration

        engine = AppleBattleEngine()
        battle_id: Optional[int] = None
        last_cursor: Optional[int] = None

        while True:
            await asyncio.sleep(beat_delay)

            finished = False
            beat_index: Optional[int] = None
            beat_state: Optional[Dict[str, Any]] = None
            targets: List[Tuple[int, int]] = []
            report_targets: List[Tuple[int, int]] = []
            report_texts: Dict[int, str] = {}

            async with get_session() as session:
                match = await session.scalar(
                    select(Match)
                    .where(Match.id == match_id)
                    .options(
                        selectinload(Match.players).selectinload(MatchPlayer.user),
                        selectinload(Match.group1).selectinload(Group.stats),
                        selectinload(Match.group2).selectinload(Group.stats),
                        selectinload(Match.game),
                    )
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                if (
                    not match
                    or match.state != MatchState.ACTIVE
                    or match.result_applied
                ):
                    return

                state = await cls.extract_persisted_state(session, match)
                if not state:
                    return

                current_cursor = int(state.get("cursor", 0))
                if last_cursor is not None and current_cursor != last_cursor:
                    # Another worker already advanced or finalized this battle.
                    return

                if battle_id is None:
                    battle = await cls.record_battle(session, match, state)
                    battle_id = battle.id

                if engine.is_finished(state):
                    finished = True
                    for group_id, msg_id in (
                        (match.group1_id, match.group1_message_id),
                        (match.group2_id, match.group2_message_id),
                    ):
                        if group_id and msg_id:
                            report_targets.append((group_id, msg_id))
                else:
                    # CLAIM the beat before sending it: the advanced cursor is
                    # durably persisted first, so a restart resumes at the next
                    # beat and the same beat is never sent twice.
                    state = engine.apply_action(state, 0, {"type": "advance"})
                    beat_index = current_cursor
                    beat_state = state
                    last_cursor = int(state.get("cursor", 0))
                    match.game_state = json.dumps(state, ensure_ascii=False)
                    await session.flush()
                    targets = cls._narration_targets(match)

            if finished:
                # 1. Remove the temporary narration messages FIRST — while the
                #    match is still ACTIVE, so a crash during teardown is
                #    recovered on restart (recovery finalizes the battle and
                #    cleans up whatever is left). Best effort: failures are
                #    logged and never touch settlement.
                await AppleBattleNarration.cleanup(bot, match_id)

                # 2. Settlement (idempotent). Runs only after cleanup, so a
                #    Telegram problem can never delay or break the result.
                async with get_session() as session:
                    match = await session.scalar(
                        select(Match)
                        .where(Match.id == match_id)
                        .options(
                            selectinload(Match.players).selectinload(MatchPlayer.user),
                            selectinload(Match.group1).selectinload(Group.stats),
                            selectinload(Match.group2).selectinload(Group.stats),
                            selectinload(Match.game),
                        )
                        .execution_options(populate_existing=True)
                        .with_for_update()
                    )
                    if not match or match.result_applied:
                        return
                    state = await cls.extract_persisted_state(session, match)
                    if not state:
                        return
                    await cls.finalize_match(session, match, battle_id)
                    # 3. The existing final Apple Battle report, unchanged.
                    report_texts = {
                        group_id: engine.render_for_group(state, group_id, None)
                        for group_id, _msg_id in report_targets
                    }

                await cls._edit_boards(
                    bot, report_targets, report_texts, engine, match_id, True
                )
                return

            if beat_index is not None and beat_state is not None and targets:
                await AppleBattleNarration.send_beat(
                    bot,
                    match_id=match_id,
                    state=beat_state,
                    beat_index=beat_index,
                    targets=targets,
                )

    @staticmethod
    async def _winner_photo(match_id: int):
        """Winner's framed card art for the finale swap (None when unavailable)."""
        try:
            from sib.database.connection import get_session
            from sib.services.character_art import photo_arg, resolve_character_photo

            async with get_session() as session:
                battle = await session.scalar(
                    select(AppleBattle)
                    .where(AppleBattle.match_id == match_id)
                    .order_by(AppleBattle.id.asc())
                    .limit(1)
                )
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
                file_id, path = await resolve_character_photo(session, character)
                return photo_arg(file_id, path)
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
        if not edit_targets:
            return
        if bot is None:
            try:
                from sib.services.maintenance.service import MaintenanceService

                bot = MaintenanceService._bot
            except Exception:
                bot = None
        if bot is None:
            return

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
