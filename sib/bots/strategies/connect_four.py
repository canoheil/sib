"""Bot strategy for Connect Four."""

from typing import Any, Dict, List, Optional


class ConnectFourBotStrategy:
    """Tactical Connect Four bot strategy."""

    game_code = "connect_four"

    def choose_action(
        self,
        state: Dict[str, Any],
        bot_player_id: int,
        difficulty: str = "normal",
    ) -> Dict[str, Any]:
        board = state["board"]
        bot_team = state["players"][str(bot_player_id)]["team_code"]
        opponent_team = "B" if bot_team == "A" else "A"

        # 1. Win immediately if possible.
        winning_col = self._find_winning_column(board, bot_team)
        if winning_col is not None:
            return {"col": winning_col}

        # 2. Block the opponent's immediate win.
        blocking_col = self._find_winning_column(board, opponent_team)
        if blocking_col is not None:
            return {"col": blocking_col}

        # Challenging bots also avoid moves that hand the opponent an
        # immediate win. This is a one-ply safety check, not a search engine:
        # it makes the existing solo bot sharper without making it unbeatable.
        candidate_columns = [
            col for col in (3, 2, 4, 1, 5, 0, 6)
            if self._is_valid_column(board, col)
        ]
        if difficulty in {"challenging", "hard"}:
            safe_columns = []
            for col in candidate_columns:
                simulated = [row[:] for row in board]
                row = self._drop_piece(simulated, col, bot_team)
                if row is None:
                    continue
                if self._find_winning_column(simulated, opponent_team) is None:
                    safe_columns.append(col)
            if safe_columns:
                return {"col": safe_columns[0]}

        if candidate_columns:
            return {"col": candidate_columns[0]}
        raise ValueError("No valid Connect Four move available.")

    @classmethod
    def _find_winning_column(
        cls,
        board: List[List[Optional[str]]],
        team: str,
    ) -> Optional[int]:
        for col in range(7):
            if not cls._is_valid_column(board, col):
                continue

            simulated = [row[:] for row in board]
            row = cls._drop_piece(simulated, col, team)

            if row is not None and cls._check_win(
                simulated, row, col, team
            ):
                return col

        return None

    @staticmethod
    def _is_valid_column(
        board: List[List[Optional[str]]],
        col: int,
    ) -> bool:
        return 0 <= col < 7 and board[0][col] is None

    @staticmethod
    def _drop_piece(
        board: List[List[Optional[str]]],
        col: int,
        team: str,
    ) -> Optional[int]:
        for row in range(len(board) - 1, -1, -1):
            if board[row][col] is None:
                board[row][col] = team
                return row

        return None

    @staticmethod
    def _check_win(
        board: List[List[Optional[str]]],
        row: int,
        col: int,
        team: str,
    ) -> bool:
        rows = len(board)
        cols = len(board[0])

        directions = (
            (0, 1),
            (1, 0),
            (1, 1),
            (1, -1),
        )

        for dr, dc in directions:
            count = 1

            r, c = row + dr, col + dc
            while (
                0 <= r < rows
                and 0 <= c < cols
                and board[r][c] == team
            ):
                count += 1
                r += dr
                c += dc

            r, c = row - dr, col - dc
            while (
                0 <= r < rows
                and 0 <= c < cols
                and board[r][c] == team
            ):
                count += 1
                r -= dr
                c -= dc

            if count >= 4:
                return True

        return False
