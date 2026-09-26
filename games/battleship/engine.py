"""Battleship game engine."""

import random
from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.core.config import settings
from sib.core.text import safe_name
from sib.database.models import MatchPlayer
from sib.games.base import BaseGameEngine, GameResult


class BattleshipEngine(BaseGameEngine):
    """Battleship (جنگ کشتی‌ها) game engine (8x8 grid).

    Supports: Solo (1v1), 2v2, 4v4.
    Fleet per team: 1x4, 2x3, 2x2 = 5 ships (14 cells total).
    Alternating shots between teams, with rotating player turns in 2v2/4v4.
    """

    game_code = "battleship"
    name_fa = "جنگ کشتی‌ها (Battleship)"
    supports_solo = True
    supports_2v2 = True
    supports_4v4 = True
    turn_timeout_seconds = settings.BATTLESHIP_TIMEOUT

    GRID_SIZE = 8
    SHIP_SIZES = [4, 3, 3, 2, 2]  # Total 14 cells

    @classmethod
    def generate_fleet(cls, custom_seed: Optional[int] = None) -> List[Dict[str, Any]]:
        """Randomly places 5 ships on an 8x8 grid without overlap."""
        rng = random.Random(custom_seed) if custom_seed is not None else random
        occupied = set()
        ships = []

        for idx, size in enumerate(cls.SHIP_SIZES):
            placed = False
            attempts = 0
            while not placed and attempts < 1000:
                attempts += 1
                orientation = rng.choice(["H", "V"])
                if orientation == "H":
                    r = rng.randint(0, cls.GRID_SIZE - 1)
                    c = rng.randint(0, cls.GRID_SIZE - size)
                    cells = [(r, c + i) for i in range(size)]
                else:
                    r = rng.randint(0, cls.GRID_SIZE - size)
                    c = rng.randint(0, cls.GRID_SIZE - 1)
                    cells = [(r + i, c) for i in range(size)]

                if not any((cr, cc) in occupied for cr, cc in cells):
                    for cell in cells:
                        occupied.add(cell)
                    ships.append({
                        "id": idx + 1,
                        "size": size,
                        "cells": [list(cell) for cell in cells],
                        "hits": [],
                        "sunk": False,
                    })
                    placed = True

            if not placed:
                raise RuntimeError("Could not place ships on 8x8 grid")

        return ships

    def create_initial_state(
        self,
        players: List[MatchPlayer],
        mode: str = "solo",
        fleet_seed_a: Optional[int] = None,
        fleet_seed_b: Optional[int] = None,
    ) -> Dict[str, Any]:
        team_a = [p for p in players if str(p.team) in ("1", "A")]
        team_b = [p for p in players if str(p.team) in ("2", "B")]

        if not team_a and not team_b and len(players) >= 2:
            team_a = [players[0]]
            team_b = [players[1]]

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
            player_map[str(p.user_id)] = {
                "team": p.team,
                "team_code": "A" if is_team_a else "B",
                "group_id": p.group_id,
                "name": safe_name(p.user.display_name if p.user else None, f"بازیکن {p.user_id}"),
            }

        fleet_a = self.generate_fleet(fleet_seed_a)
        fleet_b = self.generate_fleet(fleet_seed_b)

        return {
            "game": self.game_code,
            "mode": mode,
            "players": player_map,
            "turn_order": turn_order,
            "current_turn_index": 0,
            "current_player_id": turn_order[0] if turn_order else None,
            "teams": {
                "A": {
                    "ships": fleet_a,
                    "shots_taken": {},
                    "remaining_cells": 14,
                },
                "B": {
                    "ships": fleet_b,
                    "shots_taken": {},
                    "remaining_cells": 14,
                },
            },
            "last_event": None,
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

        r = action.get("r")
        c = action.get("c")
        if r is None or c is None or not (0 <= r < self.GRID_SIZE and 0 <= c < self.GRID_SIZE):
            return False, "مختصات شلیک نامعتبر است."

        pid_str = str(player_id)
        team_code = game_state["players"][pid_str].get("team_code", "A")
        shots_taken = game_state["teams"][team_code]["shots_taken"]

        coord_key = f"{r},{c}"
        if coord_key in shots_taken:
            return False, "به این خانه قبلاً شلیک کرده‌اید! خانه دیگری را هدف بگیرید."

        return True, None

    def apply_action(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        action: Dict[str, Any],
    ) -> Dict[str, Any]:
        pid_str = str(player_id)
        player_info = game_state["players"][pid_str]
        team_val = player_info["team"]
        team_code = player_info.get("team_code", "A")
        opp_team_code = "B" if team_code == "A" else "A"

        r = action["r"]
        c = action["c"]
        coord = [r, c]
        coord_key = f"{r},{c}"

        opp_fleet = game_state["teams"][opp_team_code]["ships"]
        hit_ship = None
        for ship in opp_fleet:
            if coord in ship["cells"]:
                hit_ship = ship
                break

        if hit_ship is not None:
            hit_ship["hits"].append(coord)
            game_state["teams"][team_code]["shots_taken"][coord_key] = "hit"
            game_state["teams"][opp_team_code]["remaining_cells"] -= 1

            if len(hit_ship["hits"]) == hit_ship["size"]:
                hit_ship["sunk"] = True
                event_msg = f"💥🎯 شلیک {player_info['name']} به هدف خورد و کشتی {hit_ship['size']} خانه‌ای دشمن غرق شد!"
            else:
                event_msg = f"💥 شلیک {player_info['name']} به یکی از کشتی‌های دشمن اصابت کرد!"
        else:
            game_state["teams"][team_code]["shots_taken"][coord_key] = "miss"
            event_msg = f"🌊 شلیک {player_info['name']} به آب خورد (خطا)!"

        game_state["last_event"] = event_msg

        if game_state["teams"][opp_team_code]["remaining_cells"] <= 0:
            game_state["finished"] = True
            game_state["winner_team"] = team_val
            game_state["is_draw"] = False
        else:
            turn_order = game_state["turn_order"]
            curr_idx = game_state["current_turn_index"]
            next_idx = (curr_idx + 1) % len(turn_order)
            game_state["current_turn_index"] = next_idx
            game_state["current_player_id"] = turn_order[next_idx]

        return game_state

    def is_finished(self, game_state: Dict[str, Any]) -> bool:
        return game_state.get("finished", False)

    def get_result(self, game_state: Dict[str, Any]) -> GameResult:
        winner_team = game_state.get("winner_team")
        is_a = str(winner_team) in ("1", "A")
        team_name = "تیم A" if is_a else "تیم B"
        summary = f"{team_name} تمامی ناوگان دشمن را غرق کرد و پیروز نبرد دریایی شد! 🚢💥🍏"
        return GameResult(
            winner_team=winner_team,
            is_draw=False,
            summary=summary,
            details={"winner_team": winner_team},
        )

    def render_for_group(
        self,
        game_state: Dict[str, Any],
        for_group_id: int,
        for_player_id: Optional[int] = None,
    ) -> str:
        my_p = next((p for p in game_state["players"].values() if p["group_id"] == for_group_id), None)
        team_code = my_p.get("team_code", "A") if my_p else "A"
        opp_team_code = "B" if team_code == "A" else "A"

        my_remaining = game_state["teams"][team_code]["remaining_cells"]
        opp_remaining = game_state["teams"][opp_team_code]["remaining_cells"]
        my_shots = game_state["teams"][team_code]["shots_taken"]

        grid_rows = []
        for r in range(self.GRID_SIZE):
            row_symbols = []
            for c in range(self.GRID_SIZE):
                state = my_shots.get(f"{r},{c}")
                if state == "hit":
                    row_symbols.append("💥")
                elif state == "miss":
                    row_symbols.append("🌊")
                else:
                    row_symbols.append("🟦")
            grid_rows.append("".join(row_symbols))
        radar_art = "\n".join(grid_rows)

        last_ev = game_state.get("last_event") or "هنوز شلیکی انجام نشده است."

        if self.is_finished(game_state):
            result = self.get_result(game_state)
            is_winner = (my_p and result.winner_team == my_p["team"])
            outcome = "🎉 **گروه شما پیروز شد!** 🍏🚢" if is_winner else "💔 **گروه حریف پیروز شد!** 🍏"
            return (
                "🚢 **پایان نبرد دریایی (Battleship)**\n\n"
                f"{radar_art}\n\n"
                f"📊 خانه‌های باقی‌مانده شما: {my_remaining}/14\n"
                f"📊 خانه‌های باقی‌مانده حریف: {opp_remaining}/14\n\n"
                f"{outcome}\n"
                f"📝 {result.summary}"
            )

        curr_pid = game_state.get("current_player_id")
        curr_p = game_state["players"].get(str(curr_pid), {})
        is_my_turn = (curr_pid == for_player_id)

        turn_msg = "🟢 **نوبت شلیک شماست!** یک مختصات روی رادار زیر را هدف بگیرید:" if is_my_turn else f"⏳ در انتظار شلیک {curr_p.get('name', 'بازیکن')}..."

        return (
            "🚢 **جنگ کشتی‌ها (Battleship)** 🍏\n\n"
            f"🎯 **رادار شلیک‌های شما به سمت دشمن:**\n"
            f"{radar_art}\n\n"
            f"🛡️ سلول‌های سالم ناوگان شما: {my_remaining}/14\n"
            f"🎯 سلول‌های سالم ناوگان دشمن: {opp_remaining}/14\n\n"
            f"📢 آخرین رویداد: {last_ev}\n\n"
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

        pid_str = str(for_player_id)
        player_info = game_state["players"].get(pid_str)
        if not player_info:
            return InlineKeyboardMarkup(inline_keyboard=[])

        team_code = player_info.get("team_code", "A")
        my_shots = game_state["teams"][team_code]["shots_taken"]
        is_my_turn = (game_state.get("current_player_id") == for_player_id)

        buttons = []
        for r in range(self.GRID_SIZE):
            row = []
            for c in range(self.GRID_SIZE):
                state = my_shots.get(f"{r},{c}")
                if state == "hit":
                    label = "💥"
                    cb = "noop"
                elif state == "miss":
                    label = "🌊"
                    cb = "noop"
                else:
                    label = "🟦"
                    cb = f"bs_action:{match_id}:{r}:{c}" if is_my_turn else "noop"
                row.append(InlineKeyboardButton(text=label, callback_data=cb))
            buttons.append(row)

        return InlineKeyboardMarkup(inline_keyboard=buttons)
