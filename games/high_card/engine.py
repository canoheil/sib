"""High Card game engine."""

import random
from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from goje.core.config import settings
from goje.database.models import MatchPlayer
from goje.games.base import BaseGameEngine, GameResult


class HighCardEngine(BaseGameEngine):
    """High Card game engine (Standard 52-card concept, Solo only)."""

    game_code = "high_card"
    name_fa = "کارت بالا (High Card)"
    supports_solo = True
    supports_2v2 = False
    supports_4v4 = False
    turn_timeout_seconds = settings.HIGH_CARD_TIMEOUT

    CARD_NAMES = {
        2: "۲", 3: "۳", 4: "۴", 5: "۵", 6: "۶", 7: "۷", 8: "۸",
        9: "۹", 10: "۱۰", 11: "سرباز (J)", 12: "بی‌بی (Q)", 13: "شاه (K)", 14: "تک / آس (A)",
    }

    SUITS = ["♠️", "♥️", "♦️", "♣️"]

    def create_initial_state(self, players: List[MatchPlayer], mode: str = "solo") -> Dict[str, Any]:
        """Create initial state for High Card."""
        player_map = {}
        for p in players:
            player_map[str(p.user_id)] = {
                "team": p.team,
                "group_id": p.group_id,
                "name": p.user.display_name if p.user else "بازیکن",
                "card_rank": None,
                "card_suit": None,
                "locked": False,
            }

        return {
            "game": self.game_code,
            "mode": mode,
            "players": player_map,
            "finished": False,
        }

    def validate_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """Validate if player can draw a card."""
        pid_str = str(player_id)
        if pid_str not in game_state.get("players", {}):
            return False, "شما بازیکن این مسابقه نیستید."

        if game_state.get("finished", False):
            return False, "مسابقه به پایان رسیده است."

        if game_state["players"][pid_str].get("locked", False):
            return False, "شما قبلاً کارت خود را کشیده‌اید."

        return True, None

    def apply_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Draw a card for the player."""
        pid_str = str(player_id)

        # Allow passing predetermined rank in action for deterministic tests
        rank = action.get("rank") or random.randint(2, 14)
        suit = action.get("suit") or random.choice(self.SUITS)

        game_state["players"][pid_str]["card_rank"] = rank
        game_state["players"][pid_str]["card_suit"] = suit
        game_state["players"][pid_str]["locked"] = True

        all_locked = all(p["locked"] for p in game_state["players"].values())
        if all_locked:
            game_state["finished"] = True

        return game_state

    def is_finished(self, game_state: Dict[str, Any]) -> bool:
        return game_state.get("finished", False)

    def get_result(self, game_state: Dict[str, Any]) -> GameResult:
        """Higher rank wins. Suit is irrelevant. Equal rank = Draw."""
        players = list(game_state["players"].values())
        if len(players) < 2:
            return GameResult(winner_team=None, is_draw=True, summary="تعداد بازیکنان ناکافی است.", details={})

        p1, p2 = players[0], players[1]
        r1, r2 = p1["card_rank"], p2["card_rank"]

        card1_str = f"{p1['card_suit']} {self.CARD_NAMES.get(r1, str(r1))}"
        card2_str = f"{p2['card_suit']} {self.CARD_NAMES.get(r2, str(r2))}"

        if r1 == r2:
            summary = f"هر دو بازیکن کارت هم‌ارز {self.CARD_NAMES.get(r1)} کشیدند. نتیجه مساوی شد!"
            return GameResult(winner_team=None, is_draw=True, summary=summary, details={"p1": r1, "p2": r2})

        if r1 > r2:
            winner_team = p1["team"]
            summary = f"{p1['name']} با کارت {card1_str} بر کارت {card2_str} {p2['name']} پیروز شد!"
        else:
            winner_team = p2["team"]
            summary = f"{p2['name']} با کارت {card2_str} بر کارت {card1_str} {p1['name']} پیروز شد!"

        return GameResult(
            winner_team=winner_team,
            is_draw=False,
            summary=summary,
            details={"winner_team": winner_team, "p1": r1, "p2": r2},
        )

    def render_for_group(
        self,
        game_state: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int] = None,
    ) -> str:
        """Render High Card status."""
        players = list(game_state["players"].values())
        my_p = next((p for p in players if p["group_id"] == for_group_id), None)
        opp_p = next((p for p in players if p["group_id"] != for_group_id), None)

        if not my_p or not opp_p:
            return "اطلاعات بازی ناقص است."

        if self.is_finished(game_state):
            result = self.get_result(game_state)
            c1 = f"{my_p['card_suit']} {self.CARD_NAMES.get(my_p['card_rank'])}"
            c2 = f"{opp_p['card_suit']} {self.CARD_NAMES.get(opp_p['card_rank'])}"

            outcome = "🤝 **نتیجه: مساوی!**" if result.is_draw else (
                "🎉 **تیم شما پیروز شد!** 🍅🏆" if result.winner_team == my_p["team"] else "💔 **تیم حریف پیروز شد!** 🍅"
            )

            return (
                "🃏 **نتیجه کارت بالا (High Card)**\n\n"
                f"👤 **{my_p['name']}:** {c1}\n"
                f"👤 **{opp_p['name']}:** {c2}\n\n"
                f"{outcome}\n"
                f"📝 {result.summary}"
            )

        my_status = "✅ کارت شما کشیده و قفل شد (محرمانه تا پایان)" if my_p["locked"] else "⏳ برای کشیدن کارت دکمه زیر را لمس کنید:"
        opp_status = "✅ کارت خود را کشید" if opp_p["locked"] else "⏳ در انتظار کشیدن کارت..."

        return (
            "🃏 **مسابقه کارت بالا (High Card)** 🍅\n\n"
            f"👤 **شما ({my_p['name']}):** {my_status}\n"
            f"👤 **حریف ({opp_p['name']}):** {opp_status}\n\n"
            "⚠️ کارت‌ها تا زمان کشیدن هر دو بازیکن مخفی می‌مانند!"
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

        if player_data and player_data.get("locked"):
            c = f"{player_data['card_suit']} {self.CARD_NAMES.get(player_data['card_rank'])}"
            return InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=f"🔒 کارت شما: {c}", callback_data="noop")]
                ]
            )

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🃏 کشیدن کارت تصادفی", callback_data=f"hc_action:{match_id}:draw")]
            ]
        )
