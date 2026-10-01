"""Rock Paper Scissors game engine implementation."""

from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.core.config import settings
from sib.core.text import safe_name
from sib.database.models import MatchPlayer
from sib.games.base import BaseGameEngine, GameResult, is_friendly_state, team_label


class RockPaperScissorsEngine(BaseGameEngine):
    """Rock Paper Scissors — best of 3."""

    game_code = "rock_paper_scissors"
    name_fa = "سنگ، کاغذ، قیچی"
    supports_solo = True
    supports_2v2 = False
    supports_4v4 = False
    turn_timeout_seconds = settings.ROCK_PAPER_SCISSORS_TIMEOUT

    TOTAL_ROUNDS = 3
    WINS_NEEDED = 2

    CHOICES = {
        "rock": "🗿 سنگ",
        "paper": "📄 کاغذ",
        "scissors": "✂️ قیچی",
    }

    BEATS = {
        "rock": "scissors",
        "scissors": "paper",
        "paper": "rock",
    }

    def create_initial_state(
        self,
        players: List[MatchPlayer],
        mode: str = "solo",
    ) -> Dict[str, Any]:
        """Create initial state for a best-of-3 RPS match."""
        player_map = {}

        for p in players:
            player_map[str(p.user_id)] = {
                "team": p.team,
                "group_id": p.group_id,
                "name": safe_name(p.user.display_name if p.user else None, "بازیکن"),
                "choice": None,
                "locked": False,
                "score": 0,
            }

        return {
            "game": self.game_code,
            "mode": mode,
            "players": player_map,
            "round": 1,
            "total_rounds": self.TOTAL_ROUNDS,
            "finished": False,
        }

    def validate_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """Validate player choice."""
        pid_str = str(player_id)

        if pid_str not in game_state.get("players", {}):
            return False, "شما بازیکن این مسابقه نیستید."

        if game_state.get("finished", False):
            return False, "بازی به پایان رسیده است."

        player_data = game_state["players"][pid_str]

        if player_data.get("locked", False):
            return False, "شما قبلاً انتخاب خود را ثبت و قفل کرده‌اید."

        choice = action.get("choice")

        if choice not in self.CHOICES:
            return False, "انتخاب نامعتبر است. لطفاً بین سنگ، کاغذ یا قیچی انتخاب کنید."

        return True, None

    def _resolve_round(self, game_state: Dict[str, Any]) -> None:
        """Resolve the current round and prepare the next one if necessary."""
        players = list(game_state["players"].values())

        if len(players) < 2:
            game_state["finished"] = True
            return

        p1, p2 = players[0], players[1]
        c1, c2 = p1["choice"], p2["choice"]

        # Draw: no score change.
        if c1 == c2:
            pass

        elif self.BEATS[c1] == c2:
            p1["score"] += 1

        else:
            p2["score"] += 1

        # Someone has already won 2 rounds.
        if (
            p1["score"] >= self.WINS_NEEDED
            or p2["score"] >= self.WINS_NEEDED
        ):
            game_state["finished"] = True
            return

        # Three rounds completed.
        if game_state["round"] >= self.TOTAL_ROUNDS:
            game_state["finished"] = True
            return

        # Prepare next round.
        game_state["round"] += 1

        for player in players:
            player["choice"] = None
            player["locked"] = False

    def apply_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply a player's choice and resolve the round when both players chose."""
        pid_str = str(player_id)
        choice = action["choice"]

        game_state["players"][pid_str]["choice"] = choice
        game_state["players"][pid_str]["locked"] = True

        # Wait for the other player.
        all_locked = all(
            p["locked"]
            for p in game_state["players"].values()
        )

        if all_locked:
            self._resolve_round(game_state)

        return game_state

    def is_finished(self, game_state: Dict[str, Any]) -> bool:
        """Return True only when the entire best-of-3 match is finished."""
        return game_state.get("finished", False)

    def get_result(self, game_state: Dict[str, Any]) -> GameResult:
        """Evaluate final winner or draw."""
        players = list(game_state["players"].values())

        if len(players) < 2:
            return GameResult(
                winner_team=None,
                is_draw=True,
                summary="تعداد بازیکنان ناکافی است.",
                details={},
            )

        p1, p2 = players[0], players[1]
        s1, s2 = p1["score"], p2["score"]

        if s1 == s2:
            summary = (
                f"نتیجه نهایی {s1} - {s2} شد. "
                "مسابقه مساوی شد!"
            )

            return GameResult(
                winner_team=None,
                is_draw=True,
                summary=summary,
                details={
                    "p1_score": s1,
                    "p2_score": s2,
                    "rounds": game_state.get("round", self.TOTAL_ROUNDS),
                },
            )

        if s1 > s2:
            winner_team = p1["team"]
            winner_name = p1["name"]
        else:
            winner_team = p2["team"]
            winner_name = p2["name"]

        summary = (
            f"{winner_name} با نتیجه {max(s1, s2)} - {min(s1, s2)} "
            "مسابقه را برد!"
        )

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

    def render_for_group(
        self,
        game_state: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int] = None,
    ) -> str:
        """Render Persian status of the current RPS round."""
        players = list(game_state["players"].values())

        if is_friendly_state(game_state):
            team_players = {
                team: next((p for p in players if str(p.get("team")) == str(team)), None)
                for team in (1, 2)
            }
            team_one, team_two = team_players[1], team_players[2]
            if not team_one or not team_two:
                return "اطلاعات بازی دوستانه کامل نیست."
            score_one, score_two = team_one["score"], team_two["score"]
            team_one_label = team_label(game_state, 1)
            team_two_label = team_label(game_state, 2)
            if self.is_finished(game_state):
                result = self.get_result(game_state)
                if result.is_draw:
                    outcome = "🤝 **نتیجه‌ی بازی: مساوی شد.**"
                else:
                    winner = team_one_label if str(result.winner_team) in ("1", "A") else team_two_label
                    outcome = f"🎉 **{winner} پیروز شد!**"
                choice_one = self.CHOICES.get(team_one["choice"], "—")
                choice_two = self.CHOICES.get(team_two["choice"], "—")
                return (
                    "✂️ **نتیجه‌ی نهایی سنگ، کاغذ، قیچی**\n\n"
                    f"🏆 **امتیاز:** {score_one} - {score_two}\n"
                    f"👥 **{team_one_label}:** {team_one['name']}، {choice_one}\n"
                    f"👥 **{team_two_label}:** {team_two['name']}، {choice_two}\n\n"
                    f"{outcome}\n📝 {result.summary}"
                )

            status_one = "✅ انتخاب ثبت شده" if team_one["locked"] else "⏳ در انتظار انتخاب"
            status_two = "✅ انتخاب ثبت شده" if team_two["locked"] else "⏳ در انتظار انتخاب"
            return (
                "✂️ **بازی دوستانه‌ی سنگ، کاغذ، قیچی**\n\n"
                f"🎯 **راند {game_state.get('round', 1)} از {self.TOTAL_ROUNDS}**\n"
                f"🏆 **امتیاز:** {score_one} - {score_two}\n\n"
                f"👥 **{team_one_label}:** {team_one['name']}، {status_one}\n"
                f"👥 **{team_two_label}:** {team_two['name']}، {status_two}\n\n"
                "🔒 انتخاب‌های ثبت‌شده تا پایان راند مخفی می‌مونن."
            )

        my_p = next(
            (p for p in players if p["group_id"] == for_group_id),
            None,
        )
        opp_p = next(
            (p for p in players if p["group_id"] != for_group_id),
            None,
        )

        if not my_p or not opp_p:
            return "اطلاعات بازی ناقص است."

        round_no = game_state.get("round", 1)
        p1_score = players[0]["score"]
        p2_score = players[1]["score"]

        if self.is_finished(game_state):
            result = self.get_result(game_state)

            if result.is_draw:
                outcome_text = "🤝 **نتیجه مسابقه: مساوی!**"
            elif result.winner_team == my_p["team"]:
                outcome_text = "🎉 **تیم شما پیروز شد!** 🍏🏆"
            else:
                outcome_text = "💔 **تیم حریف پیروز شد!** 🍏"

            my_choice = self.CHOICES.get(my_p["choice"], "—")
            opp_choice = self.CHOICES.get(opp_p["choice"], "—")

            return (
                "✂️ **نتیجه نهایی سنگ، کاغذ، قیچی!**\n\n"
                f"🏁 **امتیاز نهایی:** {p1_score} - {p2_score}\n\n"
                f"👤 **{my_p['name']}:** {my_choice}\n"
                f"👤 **{opp_p['name']}:** {opp_choice}\n\n"
                f"{outcome_text}\n"
                f"📝 {result.summary}"
            )

        my_status = (
            "✅ انتخاب شما ثبت و قفل شد (در انتظار حریف...)"
            if my_p["locked"]
            else "⏳ لطفاً انتخاب کنید:"
        )

        opp_status = (
            "✅ حریف انتخاب کرد"
            if opp_p["locked"]
            else "⏳ در حال فکر کردن..."
        )

        return (
            "✂️ **بازی سنگ، کاغذ، قیچی!** 🍏\n\n"
            f"🎯 **راند {round_no} از {self.TOTAL_ROUNDS}**\n"
            f"🏆 **امتیاز:** {p1_score} - {p2_score}\n\n"
            f"👤 **شما ({my_p['name']}):** {my_status}\n"
            f"👤 **حریف ({opp_p['name']}):** {opp_status}\n\n"
            "⚠️ انتخاب شما تا پایان راند برای حریف محرمانه می‌ماند."
        )

    def get_keyboard(
        self,
        game_state: Dict[str, Any],
        match_id: int,
        for_player_id: int,
    ) -> InlineKeyboardMarkup:
        """Generate choice buttons for player."""
        pid_str = str(for_player_id)
        player_data = game_state.get("players", {}).get(pid_str)

        if self.is_finished(game_state):
            return InlineKeyboardMarkup(inline_keyboard=[])

        if is_friendly_state(game_state):
            # The board is shared by both teams. The callback actor, not the
            # keyboard payload, identifies whose hidden choice is recorded.
            return InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="🗿 سنگ", callback_data=f"rps_action:{match_id}:rock"),
                    InlineKeyboardButton(text="📄 کاغذ", callback_data=f"rps_action:{match_id}:paper"),
                    InlineKeyboardButton(text="✂️ قیچی", callback_data=f"rps_action:{match_id}:scissors"),
                ]]
            )

        if player_data and player_data.get("locked"):
            chosen = self.CHOICES.get(
                player_data.get("choice"),
                "",
            )

            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"🔒 انتخاب شما: {chosen}",
                            callback_data="noop",
                        )
                    ]
                ]
            )

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗿 سنگ",
                        callback_data=f"rps_action:{match_id}:rock",
                    ),
                    InlineKeyboardButton(
                        text="📄 کاغذ",
                        callback_data=f"rps_action:{match_id}:paper",
                    ),
                    InlineKeyboardButton(
                        text="✂️ قیچی",
                        callback_data=f"rps_action:{match_id}:scissors",
                    ),
                ]
            ]
        )
