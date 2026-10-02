"""Apple Battle game engine (نبرد سیبی).

A fully PRECOMPUTED, staged narrative battle driven by the shared match
machinery:

- ``ab_find`` (matchmaking) pre-seeds ``match.game_state`` with the complete
  battle plan (cards, hidden-modifier scores, seeded RNG outcome and the
  narrative beats).
- Acceptance initializes nothing extra; the engine just advances the plan
  beat by beat via ``apply_action``.
- ``after_start`` schedules the auto-presentation loop (3s between beats).
  Phase 2A: each beat is delivered as a SEPARATE temporary photo message
  (``AppleBattleNarration``); the board message is only rendered as the final
  report when the plan finishes.
- When the last beat is shown the match is finished and the standard
  ``MatchResultService`` applies the outcome (seedling, stats, party release).

The win formula is EXACTLY the audited Phase 14 formula (never changed):

    effective = (Power + 1.05*Speed + 0.95*Intelligence) * character modifiers
    p(player1 wins) = 1 / (1 + 10 ** ((score2 - score1) / 60))
    winner = seeded persisted RNG draw against p
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.database.models import MatchPlayer
from sib.games.base import BaseGameEngine, GameResult

# Seconds between narrative beats ("~۳ ثانیه" per spec).
BEAT_DELAY_SECONDS = 3.0

# Backstop so a stalled narration loop still gets resolved by the shared
# timeout recovery (much longer than any normal battle presentation).
BATTLE_TIMEOUT_SECONDS = 600


class AppleBattleEngine(BaseGameEngine):
    """Staged narrative engine over a precomputed battle plan."""

    game_code = "apple_battle"
    name_fa = "نبرد سیبی"
    supports_solo = True
    supports_2v2 = False
    supports_4v4 = False
    turn_timeout_seconds = BATTLE_TIMEOUT_SECONDS

    # ------------------------------------------------------------------
    # State machine core
    # ------------------------------------------------------------------
    def create_initial_state(self, players: List[MatchPlayer], mode: str) -> Dict[str, Any]:
        """Fallback state builder.

        Real Apple Battle matches ALWAYS carry a pre-seeded ``game_state``
        (built by ``AppleBattleService.build_match_state`` at match-found
        time). This fallback only exists for API completeness and renders a
        single terminal note instead of inventing any battle data.
        """
        players_map: Dict[str, Any] = {}
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
        return {
            "players": players_map,
            "cards": {},
            "battle": {},
            "plan": [
                {
                    "phase": "finale",
                    "text": "⚠️ اطلاعات این نبرد در دسترس نیست.",
                }
            ],
            "cursor": 1,
            "current_player_id": None,
        }

    def validate_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        if self.is_finished(game_state):
            return False, "این نبرد به پایان رسیده است"
        if action.get("type") != "advance":
            return False, "حرکت نامعتبر است"
        players = game_state.get("players", {})
        if str(player_id) not in players:
            return False, "فقط نبردگران می‌توانند نبرد را جلو ببرند"
        return True, None

    def apply_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Dict[str, Any]:
        plan = game_state.get("plan", [])
        cursor = int(game_state.get("cursor", 0))
        if cursor < len(plan):
            game_state["cursor"] = cursor + 1
        if self.is_finished(game_state):
            game_state["current_player_id"] = None
        return game_state

    def is_finished(self, game_state: Dict[str, Any]) -> bool:
        return int(game_state.get("cursor", 0)) >= len(game_state.get("plan", []))

    def get_result(self, game_state: Dict[str, Any]) -> GameResult:
        battle = game_state.get("battle", {}) or {}
        winner_id = battle.get("winner_id")
        players = game_state.get("players", {})
        winner_team: Optional[int] = None
        is_draw = True
        if winner_id is not None and str(winner_id) in players:
            winner_team = int(players[str(winner_id)]["team"])
            is_draw = False
        summary = battle.get("summary") or "نبرد سیبی به پایان رسید."
        return GameResult(
            winner_team=winner_team,
            is_draw=is_draw,
            summary=summary,
            details={
                "game": self.game_code,
                "winner_id": winner_id,
                "loser_id": battle.get("loser_id"),
                "win_probability": battle.get("win_probability"),
                "seed": battle.get("seed"),
            },
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render_for_group(
        self,
        game_state: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int] = None,
    ) -> str:
        plan = game_state.get("plan", [])
        cursor = int(game_state.get("cursor", 0))
        shown: List[str] = [b.get("text", "") for b in plan[:cursor]]
        header = "🍎 **نبرد سیبی** 🍎\n"
        if not shown:
            body = "در انتظار شروع نبرد..."
        else:
            body = "\n\n".join(shown)
        if self.is_finished(game_state):
            footer = "\n\n✅ این نبرد به پایان رسید."
        else:
            footer = "\n\n⏳ نبرد ادامه دارد..."
        return f"{header}\n{body}{footer}"

    def get_keyboard(
        self,
        game_state: Dict[str, Any],
        match_id: int,
        for_player_id: int,
    ) -> InlineKeyboardMarkup:
        # Staged presentation is automatic; viewers only need a manual
        # refresh for stale boards (shared, always-valid).
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 بروزرسانی",
                        callback_data=f"game_refresh:{match_id}",
                    )
                ]
            ]
        )

    # ------------------------------------------------------------------
    # Auto-presentation hook
    # ------------------------------------------------------------------
    async def after_start(self, bot: Any, match_id: int) -> None:
        """Schedule the staged narration loop (non-blocking)."""
        from sib.services.apple_battle import AppleBattleService

        AppleBattleService.schedule_narration(match_id, bot=bot)
