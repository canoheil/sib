"""Balance Lab (Phase 16).

Monte-Carlo battle simulation for balancing characters BEFORE release.
Uses the exact same scoring model as AppleBattleService (single source of
truth) and a documented, seeded RNG so results are reproducible.
"""

import random
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import Character
from sib.services.apple_battle import AppleBattleService


class BalanceLabService:
    """Batch battle simulations for the admin balance lab."""

    @staticmethod
    def simulate_matchup(
        *,
        stats_a: Dict[str, int],
        stats_b: Dict[str, int],
        hidden_a: Optional[Dict[str, Any]] = None,
        hidden_b: Optional[Dict[str, Any]] = None,
        rounds: int = 100_000,
        seed: str = "balance-lab",
    ) -> Dict[str, Any]:
        """Simulate `rounds` one-round battles and report win rates.

        Reproducible: the same seed and inputs always give the same
        report. `rounds` can be scaled to whatever budget the lab has.
        """
        if rounds <= 0:
            raise ValueError("rounds must be positive")

        class _CharShim:
            def __init__(self, hidden):
                self.hidden_modifier_params = hidden or {}

        def _score(stats, hidden):
            return AppleBattleService._score_for_stats(
                stats.get("power", 20),
                stats.get("speed", 20),
                stats.get("intelligence", 20),
                _CharShim(hidden),
            )

        score_a = _score(stats_a, hidden_a)
        score_b = _score(stats_b, hidden_b)

        p_a = 1.0 / (1.0 + 10 ** ((score_b - score_a) / 60.0))
        rng = random.Random(seed)
        wins_a = 0
        for _ in range(rounds):
            if rng.random() < p_a:
                wins_a += 1
        wins_b = rounds - wins_a

        return {
            "rounds": rounds,
            "seed": seed,
            "score_a": score_a,
            "score_b": score_b,
            "win_probability_a": round(p_a, 4),
            "wins_a": wins_a,
            "wins_b": wins_b,
            "win_rate_a": round(wins_a / rounds, 4),
            "win_rate_b": round(wins_b / rounds, 4),
        }

    @classmethod
    async def simulate_characters(
        cls,
        session: AsyncSession,
        character_a_id: int,
        character_b_id: int,
        rounds: int = 100_000,
        seed: str = "balance-lab",
    ) -> Dict[str, Any]:
        char_a = await session.get(Character, character_a_id)
        char_b = await session.get(Character, character_b_id)
        if not char_a or not char_b:
            raise ValueError("شخصیت پیدا نشد.")

        report = cls.simulate_matchup(
            stats_a=char_a.stats_dict(),
            stats_b=char_b.stats_dict(),
            hidden_a=char_a.hidden_modifier_params,
            hidden_b=char_b.hidden_modifier_params,
            rounds=rounds,
            seed=seed,
        )
        report["character_a"] = {"code": char_a.code, "name_fa": char_a.name_fa, "balance_version": char_a.balance_version}
        report["character_b"] = {"code": char_b.code, "name_fa": char_b.name_fa, "balance_version": char_b.balance_version}
        return report
