"""Bot strategy for Battleship."""

import random
from typing import Any, Dict, List, Set, Tuple


class BattleshipBotStrategy:
    """Hunt/target strategy for Battleship.

    - Target mode: if there are un-sunk hits on the enemy fleet, fire at the
      neighbouring cells, preferring to extend a detected line of hits.
    - Hunt mode: otherwise fire at a random cell that has not been shot yet.

    The strategy never returns a coordinate that was already shot at, so the
    action is always valid against the engine rules.
    """

    game_code = "battleship"

    DIRECTIONS = ((0, 1), (1, 0), (0, -1), (-1, 0))

    def choose_action(
        self,
        state: Dict[str, Any],
        bot_player_id: int,
        difficulty: str = "normal",
    ) -> Dict[str, Any]:
        team_code = state["players"][str(bot_player_id)].get("team_code", "A")
        shots_taken: Dict[str, str] = state["teams"][team_code]["shots_taken"]

        taken: Set[Tuple[int, int]] = {
            tuple(int(x) for x in key.split(",")) for key in shots_taken
        }

        candidates = self._target_candidates(state, shots_taken, taken)
        if candidates:
            return {"r": candidates[0][0], "c": candidates[0][1]}

        open_cells = [
            (r, c)
            for r in range(state.get("grid_size", 8))
            for c in range(state.get("grid_size", 8))
            if (r, c) not in taken
        ]
        if not open_cells:
            raise ValueError("No valid Battleship target available.")

        # Checkerboard parity hunt covers the fleet faster on average.
        parity = [
            cell for cell in open_cells if (cell[0] + cell[1]) % 2 == 0
        ] or open_cells
        r, c = random.choice(parity)
        return {"r": r, "c": c}

    def _target_candidates(
        self,
        state: Dict[str, Any],
        shots_taken: Dict[str, str],
        taken: Set[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        grid_size = state.get("grid_size", 8)
        hits = {
            tuple(int(x) for x in key.split(","))
            for key, value in shots_taken.items()
            if value == "hit"
        }

        # Drop hits that already belong to a fully sunk ship.
        sunk_hits: Set[Tuple[int, int]] = set()
        for team in state.get("teams", {}).values():
            for ship in team.get("ships", []):
                if ship.get("sunk"):
                    sunk_hits.update(tuple(cell) for cell in ship.get("hits", []))
        active_hits = hits - sunk_hits

        line_extensions: List[Tuple[int, int]] = []
        neighbours: List[Tuple[int, int]] = []

        for r, c in active_hits:
            for dr, dc in self.DIRECTIONS:
                nr, nc = r + dr, c + dc
                if not (0 <= nr < grid_size and 0 <= nc < grid_size):
                    continue
                if (nr, nc) in taken:
                    # Keep extending a line through consecutive hits.
                    if (nr, nc) in active_hits:
                        er, ec = nr + dr, nc + dc
                        if (
                            0 <= er < grid_size
                            and 0 <= ec < grid_size
                            and (er, ec) not in taken
                        ):
                            line_extensions.append((er, ec))
                    continue
                neighbours.append((nr, nc))

        return line_extensions or neighbours
