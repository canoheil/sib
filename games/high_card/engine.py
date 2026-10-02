"""High Card game engine - Best of 5 with Swap/Keep mechanic."""

import random
from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.core.config import settings
from sib.core.text import safe_name
from sib.database.models import MatchPlayer
from sib.games.base import BaseGameEngine, GameResult


class HighCardEngine(BaseGameEngine):
    """High Card game engine (Best of 5, Swap or Keep mechanic, Solo only).

    Each round:
    1. Both players draw a hidden card (visible to self)
    2. Player chooses: Keep or Swap
    3. Cards are revealed, higher wins the round
    4. First to 3 wins the match (best of 5)
    """

    game_code = "high_card"
    name_fa = "کارت بالا (High Card)"
    supports_solo = True
    supports_2v2 = False
    supports_4v4 = False
    turn_timeout_seconds = settings.HIGH_CARD_TIMEOUT

    TOTAL_ROUNDS = 5
    WINS_NEEDED = 3

    CARD_NAMES = {
        2: "۲", 3: "۳", 4: "۴", 5: "۵", 6: "۶", 7: "۷", 8: "۸",
        9: "۹", 10: "۱۰", 11: "سرباز (J)", 12: "بی‌بی (Q)", 13: "شاه (K)", 14: "تک / آس (A)",
    }

    SUITS = ["♠️", "♥️", "♦️", "♣️"]

    def _draw_card(self, action: Optional[Dict[str, Any]] = None) -> Tuple[int, str]:
        """Draw a card. Accepts predetermined rank/suit for deterministic tests."""
        if action and action.get("rank") and action.get("suit"):
            return action["rank"], action["suit"]
        return random.randint(2, 14), random.choice(self.SUITS)

    def create_initial_state(self, players: List[MatchPlayer], mode: str = "solo") -> Dict[str, Any]:
        """Create initial state for High Card best-of-5 match."""
        player_map = {}
        for p in players:
            player_map[str(p.user_id)] = {
                "team": p.team,
                "group_id": p.group_id,
                "name": safe_name(p.user.display_name if p.user else None, "بازیکن"),
                "card_rank": None,
                "card_suit": None,
                "swapped": False,
                "locked": False,
                "score": 0,
            }

        return {
            "game": self.game_code,
            "mode": mode,
            "players": player_map,
            "round": 1,
            "total_rounds": self.TOTAL_ROUNDS,
            "last_round_result": None,
            "finished": False,
        }

    def validate_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """Validate player action (draw, keep, or swap)."""
        pid_str = str(player_id)
        if pid_str not in game_state.get("players", {}):
            return False, "شما بازیکن این مسابقه نیستید."

        if game_state.get("finished", False):
            return False, "مسابقه به پایان رسیده است."

        player_data = game_state["players"][pid_str]

        if player_data.get("locked", False):
            return False, "شما قبلاً تصمیم خود را ثبت کرده‌اید."

        # If card not drawn yet, action must be a draw
        if player_data["card_rank"] is None:
            if not action.get("draw"):
                return False, "ابتدا باید کارت بکشید."
            return True, None

        # Card is drawn, action must be keep or swap
        choice = action.get("choice")
        if choice not in ("keep", "swap"):
            return False, "انتخاب نامعتبر است. لطفاً نگه داشتن یا تعویض را انتخاب کنید."

        return True, None

    def _resolve_round(self, game_state: Dict[str, Any]) -> None:
        """Resolve the current round and prepare next if needed."""
        players = list(game_state["players"].values())
        if len(players) < 2:
            game_state["finished"] = True
            return

        p1, p2 = players[0], players[1]
        r1, r2 = p1["card_rank"], p2["card_rank"]

        # Determine round winner and store result for UX
        if r1 == r2:
            game_state["last_round_result"] = "draw"
        elif r1 > r2:
            p1["score"] += 1
            game_state["last_round_result"] = "p1"
        else:
            p2["score"] += 1
            game_state["last_round_result"] = "p2"

        # Check if match is over (someone reached 3 wins or max rounds played)
        if p1["score"] >= self.WINS_NEEDED or p2["score"] >= self.WINS_NEEDED:
            game_state["finished"] = True
            return

        if game_state["round"] >= self.TOTAL_ROUNDS:
            game_state["finished"] = True
            return

        # Prepare next round - reset per-round state
        game_state["round"] += 1
        for player in players:
            player["card_rank"] = None
            player["card_suit"] = None
            player["swapped"] = False
            player["locked"] = False

    def apply_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply draw, keep, or swap action."""
        pid_str = str(player_id)
        player_data = game_state["players"][pid_str]

        # Phase 1: Draw card if not yet drawn
        if player_data["card_rank"] is None:
            rank, suit = self._draw_card(action)
            player_data["card_rank"] = rank
            player_data["card_suit"] = suit
            # Don't lock - player still needs to decide keep/swap
            return game_state

        # Phase 2: Apply keep/swap decision
        choice = action.get("choice")

        if choice == "swap":
            rank, suit = self._draw_card()
            player_data["card_rank"] = rank
            player_data["card_suit"] = suit
            player_data["swapped"] = True

        player_data["locked"] = True

        # Check if both players are locked → resolve round
        all_locked = all(p["locked"] for p in game_state["players"].values())
        if all_locked:
            self._resolve_round(game_state)

        return game_state

    def is_finished(self, game_state: Dict[str, Any]) -> bool:
        return game_state.get("finished", False)

    def get_result(self, game_state: Dict[str, Any]) -> GameResult:
        """Determine match winner based on scores."""
        players = list(game_state["players"].values())
        if len(players) < 2:
            return GameResult(winner_team=None, is_draw=True, summary="تعداد بازیکنان ناکافی است.", details={})

        p1, p2 = players[0], players[1]
        s1, s2 = p1["score"], p2["score"]

        if s1 == s2:
            summary = f"نتیجه نهایی {s1} - {s2} شد. مسابقه مساوی شد!"
            return GameResult(
                winner_team=None,
                is_draw=True,
                summary=summary,
                details={"p1_score": s1, "p2_score": s2, "rounds": game_state.get("round", self.TOTAL_ROUNDS)},
            )

        if s1 > s2:
            winner_team = p1["team"]
            winner_name = p1["name"]
        else:
            winner_team = p2["team"]
            winner_name = p2["name"]

        summary = f"{winner_name} با نتیجه {max(s1, s2)} - {min(s1, s2)} مسابقه را برد!"

        return GameResult(
            winner_team=winner_team,
            is_draw=False,
            summary=summary,
            details={
                "winner_team": winner_team,
                "p1_score": s1,
                "p2_score": s2,
                "rounds": game_state.get("round", self.TOTAL_ROUNDS),
            },
        )

    def _card_str(self, player_data: Dict[str, Any]) -> str:
        """Format a player's card for display."""
        rank = player_data.get("card_rank")
        suit = player_data.get("card_suit")
        if rank is None:
            return "—"
        return f"{suit}{self.CARD_NAMES.get(rank, str(rank))}"

    def render_for_group(
        self,
        game_state: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int] = None,
    ) -> str:
        """Render High Card status with round tracking."""
        players = list(game_state["players"].values())
        my_p = next((p for p in players if p["group_id"] == for_group_id), None)
        opp_p = next((p for p in players if p["group_id"] != for_group_id), None)

        if not my_p or not opp_p:
            return "اطلاعات بازی ناقص است."

        round_no = game_state.get("round", 1)
        p1_score = players[0]["score"]
        p2_score = players[1]["score"]
        my_score = p1_score if my_p == players[0] else p2_score
        opp_score = p2_score if my_p == players[0] else p1_score

        if self.is_finished(game_state):
            result = self.get_result(game_state)
            my_card = self._card_str(my_p)
            opp_card = self._card_str(opp_p)

            if result.is_draw:
                outcome = "🤝 **نتیجه: مساوی!**"
            elif result.winner_team == my_p["team"]:
                outcome = "🎉 **شما پیروز شدید!** 🍏🏆"
            else:
                outcome = "💔 **حریف پیروز شد!** 🍏"

            return (
                "🃏 **نتیجه نهایی کارت بالا**\n\n"
                f"🏁 **امتیاز نهایی:** {my_score} - {opp_score}\n\n"
                f"👤 **شما:** {my_card}\n"
                f"👤 **حریف:** {opp_card}\n\n"
                f"{outcome}\n"
                f"📝 {result.summary}"
            )

        # Round in progress
        if my_p["card_rank"] is None:
            my_status = "⏳ کارت بکشید و تصمیم بگیرید:"
        elif not my_p["locked"]:
            my_card = self._card_str(my_p)
            my_status = f"🃏 **کارت شما:** {my_card}\nنگه می‌دارید یا تعویض می‌کنید؟"
        else:
            my_card = self._card_str(my_p)
            swap_note = " 🔄" if my_p["swapped"] else ""
            my_status = f"✅ {my_card}{swap_note} — تصمیم ثبت شد"

        opp_status = "✅ تصمیم گرفت" if opp_p["locked"] else "⏳ در انتظار..."

        # Last round result summary
        last_result_line = ""
        last = game_state.get("last_round_result")
        if last and round_no > 1:
            if last == "draw":
                last_result_line = "🤝 راند قبل مساوی شد\n\n"
            elif (last == "p1" and my_p == players[0]) or (last == "p2" and my_p == players[1]):
                last_result_line = "✅ راند قبل را بردید!\n\n"
            else:
                last_result_line = "❌ راند قبل را باختید\n\n"

        return (
            "🃏 **کارت بالا (High Card)** 🍏\n\n"
            f"🎯 **راند {round_no} از {self.TOTAL_ROUNDS}**\n"
            f"🏆 **امتیاز:** {my_score} - {opp_score}\n\n"
            f"{last_result_line}"
            f"👤 **شما:** {my_status}\n"
            f"👤 **حریف:** {opp_status}"
        )

    def get_keyboard(
        self,
        game_state: Dict[str, Any],
        match_id: int,
        for_player_id: int,
    ) -> InlineKeyboardMarkup:
        if self.is_finished(game_state):
            return InlineKeyboardMarkup(inline_keyboard=[])

        pid_str = str(for_player_id)
        player_data = game_state.get("players", {}).get(pid_str)

        if not player_data:
            return InlineKeyboardMarkup(inline_keyboard=[])

        # Phase 1: Card not drawn yet
        if player_data["card_rank"] is None:
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🃏 کشیدن کارت",
                        callback_data=f"hc_action:{match_id}:draw"
                    )]
                ]
            )

        # Phase 2: Card drawn, waiting for keep/swap
        if not player_data["locked"]:
            card_display = self._card_str(player_data)
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"✅ نگه داشتن",
                            callback_data=f"hc_action:{match_id}:keep"
                        ),
                        InlineKeyboardButton(
                            text="🔄 تعویض",
                            callback_data=f"hc_action:{match_id}:swap"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text=f"🃏 {card_display}",
                            callback_data="noop"
                        ),
                    ],
                ]
            )

        # Locked - waiting for opponent
        card_display = self._card_str(player_data)
        swap_note = " 🔄" if player_data["swapped"] else ""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"🔒 {card_display}{swap_note}",
                    callback_data="noop"
                )]
            ]
        )
