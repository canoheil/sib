"""Bot strategy for Tic-Tac-Toe."""

from typing import Any, Dict


class TicTacToeBotStrategy:
    """Simple tactical strategy for Tic-Tac-Toe."""

    game_code = "tic_tac_toe"

    def choose_action(
        self,
        state: Dict[str, Any],
        bot_player_id: int,
        difficulty: str = "normal",
    ) -> Dict[str, Any]:
        board = state["board"]
        bot_symbol = state["players"][str(bot_player_id)]["symbol"]
        opponent_symbol = "O" if bot_symbol == "X" else "X"

        winning_cell = self._find_winning_move(board, bot_symbol)
        if winning_cell is not None:
            return {"cell": winning_cell}

        blocking_cell = self._find_winning_move(board, opponent_symbol)
        if blocking_cell is not None:
            return {"cell": blocking_cell}

        if board[4] is None:
            return {"cell": 4}

        for cell in (0, 2, 6, 8):
            if board[cell] is None:
                return {"cell": cell}

        for cell, value in enumerate(board):
            if value is None:
                return {"cell": cell}

        raise ValueError("No valid Tic-Tac-Toe move available.")

    @staticmethod
    def _find_winning_move(board: list, symbol: str):
        winning_lines = (
            (0, 1, 2),
            (3, 4, 5),
            (6, 7, 8),
            (0, 3, 6),
            (1, 4, 7),
            (2, 5, 8),
            (0, 4, 8),
            (2, 4, 6),
        )

        for a, b, c in winning_lines:
            values = (board[a], board[b], board[c])

            if values.count(symbol) == 2 and values.count(None) == 1:
                return (a, b, c)[values.index(None)]

        return None
