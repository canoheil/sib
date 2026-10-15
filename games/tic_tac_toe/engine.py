"""Tic-Tac-Toe game engine."""

import random
from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardMarkup

from sib.core.config import settings
from sib.core.text import safe_name
from sib.database.models import MatchPlayer
from sib.games.base import BaseGameEngine, GameResult
from sib.keyboards.common import danger_button, primary_button, success_button


class TicTacToeEngine(BaseGameEngine):
    """Tic-Tac-Toe (دوز) game engine (Solo only, 3x3 grid)."""

    game_code = "tic_tac_toe"
    name_fa = "دوز (Tic-Tac-Toe)"
    supports_solo = True
    supports_2v2 = False
    supports_4v4 = False
    turn_timeout_seconds = settings.TIC_TAC_TOE_TIMEOUT

    WINNING_LINES = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),  # Rows
        (0, 3, 6), (1, 4, 7), (2, 5, 8),  # Columns
        (0, 4, 8), (2, 4, 6),              # Diagonals
    ]

    def create_initial_state(self, players: List[MatchPlayer], mode: str = "solo") -> Dict[str, Any]:
        """Initialize 3x3 board and randomize starting player/X mark."""
        player_list = list(players)
        random.shuffle(player_list)

        p_x, p_o = player_list[0], player_list[1]

        player_map = {
            str(p_x.user_id): {
                "symbol": "X",
                "team": p_x.team,
                "group_id": p_x.group_id,
                "name": safe_name(p_x.user.display_name if p_x.user else None, "بازیکن X"),
            },
            str(p_o.user_id): {
                "symbol": "O",
                "team": p_o.team,
                "group_id": p_o.group_id,
                "name": safe_name(p_o.user.display_name if p_o.user else None, "بازیکن O"),
            },
        }

        return {
            "game": self.game_code,
            "mode": mode,
            "board": [None] * 9,  # 0 to 8
            "players": player_map,
            "current_player_id": p_x.user_id,  # X always moves first
            "finished": False,
            "winner_team": None,
            "is_draw": False,
        }

    def validate_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        if game_state.get("finished", False):
            return False, "بازی به پایان رسیده است."

        if game_state.get("current_player_id") != player_id:
            return False, "هنوز نوبت شما نشده است."

        cell = action.get("cell")
        if cell is None or not (0 <= cell <= 8):
            return False, "خانه انتخاب شده نامعتبر است."

        if game_state["board"][cell] is not None:
            return False, "این خانه قبلاً پر شده است."

        return True, None

    def apply_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Dict[str, Any]:
        pid_str = str(player_id)
        symbol = game_state["players"][pid_str]["symbol"]
        cell = action["cell"]

        game_state["board"][cell] = symbol

        # Check win
        board = game_state["board"]
        winner_symbol = None
        for a, b, c in self.WINNING_LINES:
            if board[a] is not None and board[a] == board[b] == board[c]:
                winner_symbol = board[a]
                break

        if winner_symbol:
            game_state["finished"] = True
            game_state["is_draw"] = False
            winner_pid = next(p_id for p_id, p in game_state["players"].items() if p["symbol"] == winner_symbol)
            game_state["winner_team"] = game_state["players"][winner_pid]["team"]
        elif all(c is not None for c in board):
            # Full board with no winner = Draw
            game_state["finished"] = True
            game_state["is_draw"] = True
            game_state["winner_team"] = None
        else:
            # Switch turn to the other player
            other_pid = next(int(p_id) for p_id in game_state["players"].keys() if int(p_id) != player_id)
            game_state["current_player_id"] = other_pid

        return game_state

    def is_finished(self, game_state: Dict[str, Any]) -> bool:
        return game_state.get("finished", False)

    def get_result(self, game_state: Dict[str, Any]) -> GameResult:
        if game_state.get("is_draw"):
            return GameResult(winner_team=None, is_draw=True, summary="صفحه پر شد و نتیجه مساوی شد!", details={})

        winner_team = game_state.get("winner_team")
        winner_name = next((p["name"] for p in game_state["players"].values() if p["team"] == winner_team), "تیم برنده")
        summary = f"{winner_name} با تکمیل خط سه تایی پیروز شد!"
        return GameResult(winner_team=winner_team, is_draw=False, summary=summary, details={"winner_team": winner_team})

    def _turn_status(
        self,
        game_state: Dict[str, Any],
        my_p: Dict[str, Any],
        opp_p: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int],
    ) -> str:
        """Build a clear, symbol-aware turn status line.

        - Your turn (solo):  `🍎 نوبت شماست! یک خانه خالی را لمس کنید.`
        - Your turn (shared group): `🍎 نوبت PlayerName است! یک خانه خالی را لمس کنید.`
        - Teammate's turn: `🍎 نوبت TeammateName است! یک خانه خالی را لمس کنید.`
        - Opponent:   `⏳ نوبت حریف (🍏 PlayerName)...`

        In shared group messages (team mode), we never say "your turn" because
        the teammate reads the same message. Instead, we name the current player.
        """
        icons = {"X": "🍎", "O": "🍏"}
        current_pid = game_state.get("current_player_id")
        current_p = game_state["players"].get(str(current_pid), {})
        current_icon = icons.get(current_p.get("symbol", ""), "")

        # Check if this is a shared group message (multiple players in same group)
        members = [
            p for p in game_state["players"].values()
            if p.get("group_id") == for_group_id
        ]
        is_shared_group = len(members) > 1

        # Check if viewer is on the same team as current player
        viewer_team = my_p.get("team") if my_p else None
        current_team = current_p.get("team")
        is_same_team = viewer_team == current_team

        if current_pid == for_player_id:
            # It's the viewer's turn
            if is_shared_group:
                # Shared group: name the current player instead of "your turn"
                return f"{current_icon} **نوبت {current_p.get('name', 'بازیکن')} است!** یک خانه خالی را لمس کنید."
            else:
                return f"{current_icon} **نوبت شماست!** یک خانه خالی را لمس کنید."
        elif is_same_team:
            # Teammate's turn: name them
            return f"{current_icon} **نوبت {current_p.get('name', 'بازیکن')} است!** یک خانه خالی را لمس کنید."
        else:
            # Opponent's turn
            return f"⏳ نوبت حریف ({current_icon} {current_p.get('name', 'حریف')})..."

    def render_for_group(
        self,
        game_state: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int] = None,
    ) -> str:
        board = game_state["board"]
        players = list(game_state["players"].values())
        my_p = next((p for p in players if p["group_id"] == for_group_id), None)
        opp_p = next((p for p in players if p["group_id"] != for_group_id), None)

        if not my_p or not opp_p:
            return "اطلاعات بازی ناقص است."

        # Display symbols
        icons = {None: "⬜", "X": "🍎", "O": "🍏"}
        grid_rows = []
        for r in range(3):
            row_str = " ".join(icons[board[r * 3 + c]] for c in range(3))
            grid_rows.append(row_str)
        board_art = "\n".join(grid_rows)

        if self.is_finished(game_state):
            result = self.get_result(game_state)
            outcome = "🤝 **نتیجه: مساوی!**" if result.is_draw else (
                "🎉 **برنده شدید!** 🍏🏆" if result.winner_team == my_p["team"] else "💔 **بازی را واگذار کردید!** 🍏"
            )
            return (
                "🍎🍏 **پایان بازی دوز (Tic-Tac-Toe)**\n\n"
                f"{board_art}\n\n"
                f"{outcome}\n"
                f"📝 {result.summary}"
            )

        turn_text = self._turn_status(game_state, my_p, opp_p, for_group_id, for_player_id)

        return (
            "🍎🍏 **بازی دوز (Tic-Tac-Toe)** 🍏\n\n"
            f"👤 شما: {my_p['name']} ({icons[my_p['symbol']]})\n"
            f"👤 حریف: {opp_p['name']} ({icons[opp_p['symbol']]})\n\n"
            f"{board_art}\n\n"
            f"{turn_text}"
        )

    def get_keyboard(
        self,
        game_state: Dict[str, Any],
        match_id: int,
        for_player_id: int,
    ) -> InlineKeyboardMarkup:
        if self.is_finished(game_state):
            return InlineKeyboardMarkup(inline_keyboard=[])

        board = game_state["board"]
        is_my_turn = (game_state.get("current_player_id") == for_player_id)
        icons = {None: "⬜", "X": "🍎", "O": "🍏"}

        buttons = []
        for r in range(3):
            row = []
            for c in range(3):
                idx = r * 3 + c
                symbol = board[idx]
                label = icons[symbol]
                cb = f"ttt_action:{match_id}:{idx}" if (symbol is None and is_my_turn) else "noop"
                if symbol == "X":
                    # 🍎 owner: red, matching the apple emoji on the button.
                    row.append(danger_button(text=label, callback_data="noop"))
                elif symbol == "O":
                    # 🍏 owner: green.
                    row.append(success_button(text=label, callback_data="noop"))
                else:
                    # Empty cells stay blue and are only actionable on your turn.
                    row.append(primary_button(text=label, callback_data=cb))
            buttons.append(row)

        return InlineKeyboardMarkup(inline_keyboard=buttons)
