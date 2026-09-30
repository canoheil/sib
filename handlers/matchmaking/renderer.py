"""Persian rendering for match-found screens."""

from sib.database.models import Match


def render_match_found_for_group(match: Match, for_group_id: int) -> str:
    """Render a match summary while preserving Solo privacy and team identity."""
    mode_titles = {
        "solo": "انفرادی (Solo)",
        "2v2": "تیمی ۲ به ۲ (2v2)",
        "4v4": "تیمی ۴ به ۴ (4v4)",
    }
    mode_title = mode_titles.get(match.mode, match.mode)
    my_team_num = 1 if match.group1_id == for_group_id else 2
    opp_team_num = 2 if my_team_num == 1 else 1
    my_players = [p for p in match.players if p.team == my_team_num]
    opp_players = [p for p in match.players if p.team == opp_team_num]
    opp_group = match.group2 if my_team_num == 1 else match.group1
    my_group = match.group1 if my_team_num == 1 else match.group2

    if match.mode == "solo":
        # Keep real-player group names and numeric identifiers private.
        opp_player = opp_players[0] if opp_players else None
        opp_name = opp_player.user.display_name if opp_player and opp_player.user else "حریف"
        my_player = my_players[0] if my_players else None
        my_name = my_player.user.display_name if my_player and my_player.user else "شما"
        next_step = (
            "🤖 بازی با سیب کوچولو خودکار شروع می‌شه."
            if match.is_bot_match
            else "⏳ برای شروع مسابقه، روی دکمه «پذیرش مسابقه» بزنید (مهلت: ۱۲۰ ثانیه)."
        )
        return (
            "⚔️ **حریف پیدا شد!** 🍏\n\n"
            f"🎮 **بازی:** {match.game.name_fa}\n"
            f"⚔️ **حالت:** {mode_title}\n\n"
            f"👤 **شما:** {my_name}\n"
            f"👤 **حریف:** {opp_name}\n\n"
            f"{next_step}"
        )

    my_title = my_group.title if my_group else "تیم شما"
    opp_title = (
        "🍎 تیم سیب کوچولو"
        if match.is_bot_match and not opp_group
        else opp_group.title if opp_group else "تیم حریف"
    )
    my_wins = my_group.stats.wins if my_group and my_group.stats else 0
    opp_wins = opp_group.stats.wins if opp_group and opp_group.stats else 0
    my_roster = "\n".join(
        f"  • {p.user.display_name}" for p in my_players if p.user
    )
    opp_roster = "\n".join(
        f"  • {p.user.display_name}" for p in opp_players if p.user
    )
    if match.is_bot_match:
        footer = "🤖 بازی شروع شده و نتیجه‌اش روی رتبه‌ی رقابتی اثر نمی‌ذاره."
    else:
        footer = "⏳ یک تأیید از هر تیم برای شروع کافیه (مهلت: ۱۲۰ ثانیه)."
    return (
        "⚔️ **حریف تیمی پیدا شد!** 🍏\n\n"
        f"🎮 **بازی:** {match.game.name_fa}\n"
        f"⚔️ **حالت:** {mode_title}\n\n"
        f"🛡️ **تیم شما:** {my_title} (بردها: `{my_wins}`)\n"
        f"{my_roster}\n\n"
        f"⚔️ **تیم حریف:** {opp_title}"
        + ("" if match.is_bot_match else f" (بردها: `{opp_wins}`)")
        + f"\n{opp_roster}\n\n{footer}"
    )
