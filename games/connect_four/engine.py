"""Connect Four game engine."""

from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.core.config import settings
from sib.core.text import safe_name
from sib.database.models import MatchPlayer
from sib.games.base import BaseGameEngine, GameResult


class ConnectFourEngine(BaseGameEngine):
    """Connect Four (چهار در یک خط) game engine (7 columns x 6 rows).

    Supports: Solo (1v1), 2v2, 4v4.
    In team modes, player turns alternate between teams: A1 -> B1 -> A2 -> B2 -> ...
    """

    game_code = "connect_four"
    name_fa = "چهار در یک خط (Connect Four)"
    supports_solo = True
    supports_2v2 = True
    supports_4v4 = True
    turn_timeout_seconds = settings.CONNECT_FOUR_TIMEOUT

    COLS = 7
    ROWS = 6

    def create_initial_state(self, players: List[MatchPlayer], mode: str = "solo") -> Dict[str, Any]:
        team_a = [p for p in players if str(p.team) in ("1", "A")]
        team_b = [p for p in players if str(p.team) in ("2", "B")]

        if not team_a and not team_b and len(players) >= 2:
            team_a = [players[0]]
            team_b = [players[1]]

        # Interleave players: A1, B1, A2, B2, ...
        turn_order: List[int] = []
        max_len = max(len(team_a), len(team_b))
        for i in range(max_len):
            if i < len(team_a):
                turn_order.append(team_a[i].user_id)
            if i < len(team_b):
                turn_order.append(team_b[i].user_id)

        player_map = {}
        for p in players:
            is_team_a = str(p.team) in ("1", "A") or (team_a and p.user_id == team_a[0].user_id)
            symbol = "🍎" if is_team_a else "🍏"
            player_map[str(p.user_id)] = {
                "team": p.team,
                "team_code": "A" if is_team_a else "B",
                "group_id": p.group_id,
                "name": safe_name(p.user.display_name if p.user else None, f"بازیکن {p.user_id}"),
                "symbol": symbol,
            }

        board = [[None for _ in range(self.COLS)] for _ in range(self.ROWS)]

        return {
            "game": self.game_code,
            "mode": mode,
            "board": board,
            "players": player_map,
            "turn_order": turn_order,
            "current_turn_index": 0,
            "current_player_id": turn_order[0] if turn_order else None,
            "finished": False,
            "winner_team": None,
            "is_draw": False,
            "moves_count": 0,
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

        col = action.get("col")
        if col is None or not (0 <= col < self.COLS):
            return False, "ستون انتخاب شده نامعتبر است."

        board = game_state["board"]
        if board[0][col] is not None:
            return False, "این ستون پر شده است! ستون دیگری را انتخاب کنید."

        return True, None

    def apply_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Dict[str, Any]:
        col = action["col"]
        board = game_state["board"]
        pid_str = str(player_id)
        team_val = game_state["players"][pid_str]["team"]
        team_code = game_state["players"][pid_str].get("team_code", "A")

        placed_row = -1
        for r in range(self.ROWS - 1, -1, -1):
            if board[r][col] is None:
                board[r][col] = team_code
                placed_row = r
                break

        game_state["moves_count"] = game_state.get("moves_count", 0) + 1

        if self._check_win(board, placed_row, col, team_code):
            game_state["finished"] = True
            game_state["winner_team"] = team_val
            game_state["is_draw"] = False
        elif game_state["moves_count"] >= self.ROWS * self.COLS:
            game_state["finished"] = True
            game_state["is_draw"] = True
            game_state["winner_team"] = None
        else:
            turn_order = game_state["turn_order"]
            curr_idx = game_state["current_turn_index"]
            next_idx = (curr_idx + 1) % len(turn_order)
            game_state["current_turn_index"] = next_idx
            game_state["current_player_id"] = turn_order[next_idx]

        return game_state

    def _check_win(self, board: List[List[Optional[str]]], row: int, col: int, team_code: str) -> bool:
        directions = [
            (0, 1),   # Horizontal
            (1, 0),   # Vertical
            (1, 1),   # Diagonal down-right \
            (1, -1),  # Diagonal down-left /
        ]

        for dr, dc in directions:
            count = 1
            r, c = row + dr, col + dc
            while 0 <= r < self.ROWS and 0 <= c < self.COLS and board[r][c] == team_code:
                count += 1
                r += dr
                c += dc

            r, c = row - dr, col - dc
            while 0 <= r < self.ROWS and 0 <= c < self.COLS and board[r][c] == team_code:
                count += 1
                r -= dr
                c -= dc

            if count >= 4:
                return True

        return False

    def is_finished(self, game_state: Dict[str, Any]) -> bool:
        return game_state.get("finished", False)

    def get_result(self, game_state: Dict[str, Any]) -> GameResult:
        if game_state.get("is_draw"):
            return GameResult(
                winner_team=None,
                is_draw=True,
                summary="صفحه پر شد و هیچ تیمی موفق به خط ۴ تایی نشد! مساوی.",
                details={},
            )

        winner_team = game_state.get("winner_team")
        is_a = str(winner_team) in ("1", "A")
        team_name = f"تیم {'سیب قرمز 🍎 (A)' if is_a else 'سیب سبز 🍏 (B)'}"
        summary = f"{team_name} با ۴ مهره متوالی پیروز شد! 🍏🎉"
        return GameResult(
            winner_team=winner_team,
            is_draw=False,
            summary=summary,
            details={"winner_team": winner_team},
        )

    def _turn_status(
        self,
        game_state: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int],
    ) -> str:
        """Build a clear, team-aware turn status line.

        Solo:
          - Your turn:    `🍎 نوبت شماست! یک ستون را انتخاب کنید.`
          - Opponent:     `⏳ نوبت حریف (🍏 PlayerName)...`

        Team (2v2 / 4v4):
          - Your turn (you are the active player, solo group):
              `🍎 نوبت شماست! یک ستون را انتخاب کنید.`
          - Your turn (shared group with teammate):
              `🍎 نوبت PlayerName است! یک ستون را انتخاب کنید.`
          - Your team's turn (teammate is active):
              `🍎 نوبت تیم شما: 👤 TeammateName`
          - Opponent team's turn:
              `⏳ نوبت تیم حریف: 👤 PlayerName (🍏)`
        """
        current_pid = game_state.get("current_player_id")
        current_p = game_state["players"].get(str(current_pid), {})
        current_team_code = current_p.get("team_code", "")
        current_symbol = current_p.get("symbol", "")
        current_name = current_p.get("name", "حریف")

        # Find the viewer's team
        my_p = next(
            (p for p in game_state["players"].values() if p["group_id"] == for_group_id),
            None,
        )
        my_team_code = my_p.get("team_code", "") if my_p else ""
        is_team_mode = len(game_state["players"]) > 2

        # Check if this is a shared group message (multiple players in same group)
        members = [
            p for p in game_state["players"].values()
            if p.get("group_id") == for_group_id
        ]
        is_shared_group = len(members) > 1

        if not is_team_mode:
            # Solo mode: simple your-turn / opponent-turn
            if current_pid == for_player_id:
                if is_shared_group:
                    return f"{current_symbol} **نوبت {current_name} است!** یک ستون را انتخاب کنید."
                return f"{current_symbol} **نوبت شماست!** یک ستون را انتخاب کنید."
            return f"⏳ نوبت حریف ({current_symbol} {current_name})..."

        # Team mode: distinguish your turn, teammate's turn, opponent's turn
        if current_pid == for_player_id:
            if is_shared_group:
                return f"{current_symbol} **نوبت {current_name} است!** یک ستون را انتخاب کنید."
            return f"{current_symbol} **نوبت شماست!** یک ستون را انتخاب کنید."

        if current_team_code == my_team_code:
            # Teammate's turn
            return f"{current_symbol} **نوبت تیم شما:** 👤 {current_name}"

        # Opponent team's turn
        return f"⏳ نوبت تیم حریف: 👤 {current_name} ({current_symbol})"

    def render_for_group(
        self,
        game_state: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int] = None,
    ) -> str:
        board = game_state["board"]
        cell_icons = {None: "⬜", "A": "🍎", "B": "🍏"}

        board_lines = []
        for r in range(self.ROWS):
            row_str = "".join(cell_icons[board[r][c]] for c in range(self.COLS))
            board_lines.append(row_str)
        board_lines.append("1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣")
        board_art = "\n".join(board_lines)

        team_a_names = [p["name"] for p in game_state["players"].values() if p.get("team_code") == "A"]
        team_b_names = [p["name"] for p in game_state["players"].values() if p.get("team_code") == "B"]

        team_names = game_state.get("team_names", {})
        team_a_title = team_names.get("1", "🍎 تیم سیب")
        team_b_title = team_names.get("2", "🍏 تیم سیب سبز")
        team_info = (
            f"⚔️ **{team_a_title}:** {', '.join(team_a_names)}\n"
            f"🆚 **{team_b_title}:** {', '.join(team_b_names)}"
        )

        if self.is_finished(game_state):
            result = self.get_result(game_state)
            my_team = next((p["team"] for p in game_state["players"].values() if p["group_id"] == for_group_id), None)
            if result.is_draw:
                outcome = "🤝 **نتیجه: مساوی!**"
            elif result.winner_team == my_team:
                outcome = "🎉 **گروه شما پیروز شد!** 🍏🏆"
            else:
                outcome = "💔 **گروه حریف پیروز شد!** 🍏"

            return (
                "🍎🍏 **پایان بازی چهار در یک خط (Connect Four)**\n\n"
                f"{board_art}\n\n"
                f"{team_info}\n\n"
                f"{outcome}\n"
                f"📝 {result.summary}"
            )

        turn_msg = self._turn_status(game_state, for_group_id, for_player_id)

        return (
            "🍎🍏 **چهار در یک خط (Connect Four)** 🍏\n\n"
            f"{board_art}\n\n"
            f"{team_info}\n\n"
            f"{turn_msg}"
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

        col_buttons = []
        col_labels = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]
        for c in range(self.COLS):
            is_full = (board[0][c] is not None)
            label = "🚫" if is_full else col_labels[c]
            cb = f"c4_action:{match_id}:{c}" if (not is_full and is_my_turn) else "noop"
            col_buttons.append(InlineKeyboardButton(text=label, callback_data=cb))

        return InlineKeyboardMarkup(inline_keyboard=[col_buttons])
