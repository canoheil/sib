"""Shared setup helpers for active PvP and bot-fallback matches."""

from typing import Any, Dict


def attach_team_names(state: Dict[str, Any], match) -> Dict[str, Any]:
    """Persist stable team identities in engine state for all later renders.

    Solo screens retain their existing privacy-preserving player-vs-player
    presentation. Team matches use Telegram group identity, or the bot squad
    label for a group-less bot side.
    """
    if match.mode == "solo":
        return state

    group1 = getattr(match, "group1", None)
    group2 = getattr(match, "group2", None)
    state["team_names"] = {
        "1": group1.title if group1 else "تیم شما",
        "2": (
            group2.title
            if group2
            else "🍎 تیم سیب کوچولو"
            if match.is_bot_match
            else "تیم حریف"
        ),
    }
    return state
