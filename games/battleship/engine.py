"""Battleship game engine."""

import random
from typing import Any, Dict, List, Optional, Tuple
from aiogram.types import InlineKeyboardMarkup

from sib.core.config import settings
from sib.core.text import safe_name
from sib.database.models import MatchPlayer
from sib.games.base import BaseGameEngine, GameResult
from sib.keyboards.common import danger_button, primary_button, success_button


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

    # Friendly matches ("بازی رفیقانه") share ONE group message between both
    # teams, so the per-side radar may never be rendered there: the shooting
    # grid and the guess map are private to one team. This flag tells the
    # generic dispatcher to present the neutral public board in the group and
    # deliver the targeting grid as a private (ephemeral/DM) panel instead.
    friendly_private_radar = True

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
            "last_shot": None,
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

        sunk_size: Optional[int] = None
        if hit_ship is not None:
            hit_ship["hits"].append(coord)
            game_state["teams"][team_code]["shots_taken"][coord_key] = "hit"
            game_state["teams"][opp_team_code]["remaining_cells"] -= 1

            if len(hit_ship["hits"]) == hit_ship["size"]:
                hit_ship["sunk"] = True
                sunk_size = int(hit_ship["size"])
                event_msg = f"💥🎯 شلیک {player_info['name']} به هدف خورد و کشتی {hit_ship['size']} خانه‌ای دشمن غرق شد!"
            else:
                event_msg = f"💥 شلیک {player_info['name']} به یکی از کشتی‌های دشمن اصابت کرد!"
            game_state["last_shot"] = {"r": r, "c": c, "result": "hit", "by": player_id}
        else:
            game_state["teams"][team_code]["shots_taken"][coord_key] = "miss"
            event_msg = f"🌊 شلیک {player_info['name']} به آب خورد (خطا)!"
            game_state["last_shot"] = {"r": r, "c": c, "result": "miss", "by": player_id}

        game_state["last_event"] = event_msg

        # Ordered, attributed public history. The shared friendly board renders
        # ONLY this (never a radar), so both teams read the same facts.
        game_state.setdefault("shot_log", []).append(
            {
                "by": player_id,
                "name": player_info["name"],
                "team_code": team_code,
                "r": r,
                "c": c,
                "result": game_state["last_shot"]["result"],
                "sunk_size": sunk_size,
            }
        )

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


    # Cell state is rendered in text AND in the button label; Telegram button
    # style is a separate visual layer (all playable cells stay blue/primary):
    #   unselected  -> ·  (visually minimal)
    #   miss/water  -> 🌊 (wave emoji)
    #   hit         -> 💥 (explosion emoji)
    #   sunk        -> 🔥 (fire emoji)
    EMPTY = "·"
    MISS = "🌊"
    HIT = "💥"
    SUNK = "🔥"

    @staticmethod
    def _coord_label(r: int, c: int) -> str:
        """Convert (row, col) to human-readable coordinate like 'C4'."""
        return f"{chr(ord('A') + c)}{r + 1}"

    def radar_labels(self, game_state: Dict[str, Any], team_code: str) -> Dict[str, str]:
        """Labels for this team's radar (their shots at the enemy fleet)."""
        opp = "B" if team_code == "A" else "A"
        shots = game_state["teams"][team_code]["shots_taken"]
        sunk_cells = set()
        for ship in game_state["teams"][opp]["ships"]:
            if ship.get("sunk"):
                for cell in ship["cells"]:
                    sunk_cells.add(f"{cell[0]},{cell[1]}")
        labels: Dict[str, str] = {}
        for key, val in shots.items():
            # Always show the shot result, even if the cell later got sunk.
            # Priority: SUNK > HIT > MISS
            if key in sunk_cells:
                labels[key] = self.SUNK
            elif val == "hit":
                labels[key] = self.HIT
            else:
                labels[key] = self.MISS
        # Ensure all cells that were shot have a label, even if somehow missing from dict
        for key in shots:
            if key not in labels:
                labels[key] = self.MISS  # Default to miss if somehow not categorized
        return labels

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
        labels = self.radar_labels(game_state, team_code)

        # Build grid with coordinate labels:
        #    A B C D E F G H
        #  1 · · 💥 · · · · ·
        #  2 🌊 · · · · · · ·
        #  ...
        col_header = "  " + " ".join(chr(ord("A") + c) for c in range(self.GRID_SIZE))
        grid_rows = [col_header]
        for r in range(self.GRID_SIZE):
            row_symbols = []
            for c in range(self.GRID_SIZE):
                row_symbols.append(labels.get(f"{r},{c}", self.EMPTY))
            grid_rows.append(f"{r + 1} " + " ".join(row_symbols))
        radar_art = "\n".join(grid_rows)

        # Last shot confirmation (compact, Persian UI style)
        last_shot = game_state.get("last_shot")
        last_shot_line = ""
        if last_shot:
            coord = self._coord_label(last_shot["r"], last_shot["c"])
            if last_shot.get("result") == "hit":
                last_shot_line = f"\n🎯 {coord}\n💥 برخورد!\n"
            else:
                last_shot_line = f"\n🎯 {coord}\n🌊 خطا!\n"

        last_ev = game_state.get("last_event") or "هنوز شلیکی انجام نشده است."
        team_names = game_state.get("team_names", {})
        roster_a = ", ".join(
            p["name"] for p in game_state["players"].values()
            if p.get("team_code") == "A"
        )
        roster_b = ", ".join(
            p["name"] for p in game_state["players"].values()
            if p.get("team_code") == "B"
        )
        teams_line = (
            f"⚔️ **{team_names.get('1', 'تیم شما')}:** {roster_a}\n"
            f"🆚 **{team_names.get('2', 'تیم حریف')}:** {roster_b}"
        )

        if self.is_finished(game_state):
            result = self.get_result(game_state)
            is_winner = (my_p and result.winner_team == my_p["team"])
            outcome = "🎉 **گروه شما پیروز شد!** 🍏🚢" if is_winner else "💔 **گروه حریف پیروز شد!** 🍏"
            return (
                "🚢 **پایان نبرد دریایی (Battleship)**\n\n"
                f"{teams_line}\n\n"
                f"{radar_art}\n\n"
                f"📊 خانه‌های باقی‌مانده شما: {my_remaining}/14\n"
                f"📊 خانه‌های باقی‌مانده حریف: {opp_remaining}/14\n\n"
                f"{last_shot_line}"
                f"{outcome}\n"
                f"📝 {result.summary}"
            )

        curr_pid = game_state.get("current_player_id")
        curr_p = game_state["players"].get(str(curr_pid), {})
        is_my_turn = (curr_pid == for_player_id)

        if is_my_turn:
            from sib.games.base import turn_line
            turn_msg = turn_line(
                game_state, for_group_id, for_player_id,
                "یک مختصات روی رادار زیر را هدف بگیرید:",
                "یک مختصات روی رادار زیر را هدف بگیرید:",
            )
        else:
            turn_msg = f"⏳ در انتظار شلیک {curr_p.get('name', 'بازیکن')}..."

        return (
            "🚢 **جنگ کشتی‌ها (Battleship)** 🍏\n\n"
            f"{teams_line}\n\n"
            f"🎯 **رادار شلیک‌های شما به سمت دشمن:**\n"
            f"{radar_art}\n\n"
            f"🛡️ سلول‌های سالم ناوگان شما: {my_remaining}/14\n"
            f"🎯 سلول‌های سالم ناوگان دشمن: {opp_remaining}/14\n\n"
            f"{last_shot_line}"
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
        labels = self.radar_labels(game_state, team_code)
        is_my_turn = (game_state.get("current_player_id") == for_player_id)

        # Header row: column labels A-H, with the non-playable corner marked red.
        header_row = [danger_button(text=" ", callback_data="noop")]
        for c in range(self.GRID_SIZE):
            header_row.append(
                success_button(text=chr(ord("A") + c), callback_data="noop")
            )
        buttons = [header_row]

        # Data rows: green row label + blue playable cell buttons.
        for r in range(self.GRID_SIZE):
            row = [success_button(text=str(r + 1), callback_data="noop")]
            for c in range(self.GRID_SIZE):
                key = f"{r},{c}"
                if key in labels:
                    # Shot cells keep their outcome label inside a blue button.
                    row.append(primary_button(text=labels[key], callback_data="noop"))
                else:
                    # Empty cells remain blue and actionable only on your turn.
                    cb = f"bs_action:{match_id}:{r}:{c}" if is_my_turn else "noop"
                    row.append(primary_button(text=self.EMPTY, callback_data=cb))
            buttons.append(row)

        return InlineKeyboardMarkup(inline_keyboard=buttons)

    # ------------------------------------------------------------------
    # Friendly-match surfaces
    #
    # A friendly match shares ONE group message between both teams, so the
    # public board must be viewer-NEUTRAL (it may not show any team's radar)
    # and the targeting grid is delivered as a private panel instead.
    # ------------------------------------------------------------------

    DEFAULT_TEAM_LABELS = {"A": "🍎 تیم سرخ", "B": "🍏 تیم سبز"}
    PUBLIC_LOG_LIMIT = 10
    RADAR_TITLE_YOUR_TURN = "🎯 **رادار شما (نوبت شماست)**"
    RADAR_TITLE_WAITING = "🎯 **رادار شما**"

    # ---- small pure helpers -------------------------------------------

    @staticmethod
    def _player_info(
        game_state: Dict[str, Any], player_id: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        if player_id is None:
            return None
        return (game_state.get("players") or {}).get(str(player_id))

    def team_code_of(
        self, game_state: Dict[str, Any], player_id: Optional[int]
    ) -> Optional[str]:
        """Team code ('A'/'B') of a player, or None when they are not in it."""
        info = self._player_info(game_state, player_id)
        if not info:
            return None
        return info.get("team_code", "A")

    def _roster(self, game_state: Dict[str, Any], team_code: str) -> str:
        names = [
            p.get("name", "بازیکن")
            for p in (game_state.get("players") or {}).values()
            if p.get("team_code", "A") == team_code
        ]
        return "، ".join(names) if names else "—"

    def _team_label(self, game_state: Dict[str, Any], team_code: str) -> str:
        """Friendly team name (🍎 تیم سرخ / 🍏 تیم سبز) for a team code."""
        fallback = self.DEFAULT_TEAM_LABELS.get(
            team_code, self.DEFAULT_TEAM_LABELS["A"]
        )
        team_numbers = {
            str(p.get("team"))
            for p in (game_state.get("players") or {}).values()
            if p.get("team_code", "A") == team_code
        }
        names = game_state.get("team_names") or {}
        for number in sorted(team_numbers):
            label = names.get(number)
            if label:
                return label
        return fallback

    def _remaining(self, game_state: Dict[str, Any], team_code: str) -> int:
        return int(game_state["teams"][team_code].get("remaining_cells", 0))

    def _current_player(self, game_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._player_info(game_state, game_state.get("current_player_id"))

    def _grid_art(self, labels: Dict[str, str]) -> str:
        col_header = "  " + " ".join(
            chr(ord("A") + c) for c in range(self.GRID_SIZE)
        )
        rows = [col_header]
        for r in range(self.GRID_SIZE):
            symbols = [
                labels.get(f"{r},{c}", self.EMPTY) for c in range(self.GRID_SIZE)
            ]
            rows.append(f"{r + 1} " + " ".join(symbols))
        return "\n".join(rows)

    def shot_result_text(
        self, result: str, sunk_size: Optional[int] = None
    ) -> str:
        """Public wording of one shot result (no side-specific words)."""
        if result == "hit":
            if sunk_size:
                return f"🔥 غرق شد ({sunk_size} خانه)"
            return "💥 برخورد"
        return "🌊 آب"

    def _shot_log_entries(self, game_state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Ordered shot history; legacy states fall back to ``last_shot``."""
        log = game_state.get("shot_log")
        if isinstance(log, list):
            return [entry for entry in log if isinstance(entry, dict)]
        legacy = game_state.get("last_shot")
        if legacy:
            return [
                {
                    "by": legacy.get("by"),
                    "name": "بازیکن",
                    "r": legacy.get("r"),
                    "c": legacy.get("c"),
                    "result": legacy.get("result"),
                    "sunk_size": None,
                }
            ]
        return []

    def public_log_lines(self, game_state: Dict[str, Any]) -> List[str]:
        """Rendered public history lines (newest last, capped)."""
        entries = self._shot_log_entries(game_state)
        shown = entries[-self.PUBLIC_LOG_LIMIT :]
        lines = []
        for entry in shown:
            r, c = entry.get("r"), entry.get("c")
            if r is None or c is None:
                continue
            who = entry.get("name") or "بازیکن"
            result = self.shot_result_text(
                entry.get("result", "miss"), entry.get("sunk_size")
            )
            lines.append(
                f"• {who} به {self._coord_label(int(r), int(c))} شلیک کرد: {result}"
            )
        return lines

    def last_shot_of(
        self, game_state: Dict[str, Any], player_id: int
    ) -> Optional[Dict[str, Any]]:
        """The most recent shot played BY this player (private panel use)."""
        for entry in reversed(self._shot_log_entries(game_state)):
            if entry.get("by") is not None and int(entry["by"]) == int(player_id):
                return entry
        return None

    # ---- surface 1: the neutral public group board ---------------------

    def render_public(
        self,
        game_state: Dict[str, Any],
        *,
        finished: Optional[bool] = None,
    ) -> str:
        """Viewer-neutral group board: rosters, history, turn. NO radar.

        Everything rendered here is public information: both teams' remaining
        hull cells (derivable from public hits), the ordered shot history and
        whose turn it is. No grid, no ``شما``/``حریف`` side wording.
        """
        is_finished = self.is_finished(game_state) if finished is None else finished
        team_a = self._team_label(game_state, "A")
        team_b = self._team_label(game_state, "B")

        lines = [
            "🚢 **جنگ کشتی‌ها (Battleship) — بازی رفیقانه**",
            "",
            f"⚔️ **{team_a}:** {self._roster(game_state, 'A')}",
            f"🆚 **{team_b}:** {self._roster(game_state, 'B')}",
            "",
            (
                f"📊 **ناوگان:** {team_a} {self._remaining(game_state, 'A')}/14"
                f" — {team_b} {self._remaining(game_state, 'B')}/14"
            ),
            "",
        ]

        if is_finished:
            winner = game_state.get("winner_team")
            if game_state.get("is_draw"):
                lines.append("🏁 **بازی تمام شد — مساوی!**")
            else:
                winner_code = "A" if str(winner) in ("1", "A") else "B"
                names = game_state.get("team_names") or {}
                label = names.get(str(winner)) or self._team_label(
                    game_state, winner_code
                )
                lines.append(f"🏁 **بازی تمام شد — برنده: {label}**")
        else:
            current = self._current_player(game_state) or {}
            current_name = current.get("name", "بازیکن")
            code = current.get("team_code", "A")
            lines.append(
                f"🎯 **نوبت: {current_name}** ({self._team_label(game_state, code)})"
            )

        lines.append("")
        log_lines = self.public_log_lines(game_state)
        if log_lines:
            lines.append("📜 **شلیک‌ها:**")
            entries = self._shot_log_entries(game_state)
            hidden = len(entries) - len(log_lines)
            if hidden > 0:
                lines.append(f"… و {hidden} شلیک قبلی")
            lines.extend(log_lines)
        else:
            lines.append("📜 **هنوز شلیکی انجام نشده است.**")

        if not is_finished:
            lines.extend(
                [
                    "",
                    "برای دیدن رادار و شلیک، «🎯 رادار من» را بزن؛ فقط خودت می‌بینی.",
                ]
            )
        return "\n".join(lines)

    def get_public_keyboard(self, match_id: int) -> InlineKeyboardMarkup:
        """Neutral group keyboard: open the private radar / refresh.

        Deliberately contains NO shot coordinates and NO grid: the group
        message can be read by both teams.
        """
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    success_button(
                        text="🎯 رادار من",
                        callback_data=f"bs_radar:{match_id}",
                    )
                ],
                [
                    primary_button(
                        text="🔄 بروزرسانی",
                        callback_data=f"game_refresh:{match_id}",
                    )
                ],
            ]
        )

    # ---- surface 2: the private radar panel ---------------------------

    def render_radar_panel(
        self,
        game_state: Dict[str, Any],
        player_id: int,
        *,
        notice: Optional[str] = None,
    ) -> str:
        """Private panel for ONE player: radar grid + their own status.

        Only ever delivered to that player (ephemeral message or DM) - never
        rendered into the shared group message.
        """
        team_code = self.team_code_of(game_state, player_id) or "A"
        opp_code = "B" if team_code == "A" else "A"
        is_my_turn = (
            not self.is_finished(game_state)
            and game_state.get("current_player_id") == player_id
        )
        current = self._current_player(game_state) or {}
        current_name = current.get("name", "بازیکن")

        lines = [
            self.RADAR_TITLE_YOUR_TURN if is_my_turn else self.RADAR_TITLE_WAITING,
        ]

        if self.is_finished(game_state):
            lines.append("🏁 بازی تمام شد.")
        elif is_my_turn:
            lines.append("🟢 یک خانه روی رادار زیر را هدف بگیر:")
        else:
            lines.append(f"⏳ نوبت {current_name} است؛ رادار فعلاً تماشایی است.")

        lines.extend(
            [
                "",
                "```",
                self._grid_art(self.radar_labels(game_state, team_code)),
                "```",
                "",
                f"🛡️ ناوگان شما: {self._remaining(game_state, team_code)}/14",
                f"🎯 ناوگان دشمن: {self._remaining(game_state, opp_code)}/14",
                "",
            ]
        )

        if notice is None:
            last = self.last_shot_of(game_state, player_id)
            if last and last.get("r") is not None and last.get("c") is not None:
                notice = (
                    f"🎯 شما به {self._coord_label(int(last['r']), int(last['c']))}"
                    f" زدید: {self.shot_result_text(last.get('result', 'miss'), last.get('sunk_size'))}"
                )
        lines.append(notice if notice else "🎯 هنوز شلیکی نکرده‌اید.")
        return "\n".join(lines)

    def get_radar_panel_keyboard(
        self,
        game_state: Dict[str, Any],
        match_id: int,
        player_id: int,
    ) -> InlineKeyboardMarkup:
        """Shooting grid for ONE player's private panel.

        Cells call ``bs_shot:{match}:{r}:{c}`` only when it is this player's
        turn; every other state is inert, so a stale panel can never act.
        """
        player_info = self._player_info(game_state, player_id)
        if not player_info:
            return InlineKeyboardMarkup(inline_keyboard=[])

        team_code = player_info.get("team_code", "A")
        labels = self.radar_labels(game_state, team_code)
        is_my_turn = (
            not self.is_finished(game_state)
            and game_state.get("current_player_id") == player_id
        )

        header_row = [danger_button(text=" ", callback_data="noop")]
        for c in range(self.GRID_SIZE):
            header_row.append(
                success_button(text=chr(ord("A") + c), callback_data="noop")
            )
        buttons = [header_row]

        for r in range(self.GRID_SIZE):
            row = [success_button(text=str(r + 1), callback_data="noop")]
            for c in range(self.GRID_SIZE):
                key = f"{r},{c}"
                if key in labels:
                    row.append(primary_button(text=labels[key], callback_data="noop"))
                else:
                    cb = f"bs_shot:{match_id}:{r}:{c}" if is_my_turn else "noop"
                    row.append(primary_button(text=self.EMPTY, callback_data=cb))
            buttons.append(row)

        buttons.append(
            [
                success_button(
                    text="🔄 بروزرسانی رادار",
                    callback_data=f"bs_radar:{match_id}",
                )
            ]
        )
        buttons.append(
            [
                danger_button(
                    text="✖️ بستن",
                    callback_data=f"bs_close:{match_id}",
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=buttons)
