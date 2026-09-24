"""Persian rendering for Match Found and Acceptance messages."""

from sib.database.models import Match, MatchPlayer


def render_match_found_for_group(match: Match, for_group_id: int) -> str:
    """
    Render 'Match Found' message for a specific Telegram group.
    
    Solo Privacy Rules:
    - Show opponent's Telegram display name only.
    - Do NOT expose numeric Telegram ID.
    - Do NOT expose username unless genuinely required.
    - Do NOT expose opponent's group.
    
    Team Privacy Rules:
    - Show group name.
    - Show team member display names.
    - Show group rating.
    """
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
        # Solo Mode: opponent display name ONLY, no group exposure, no numeric ID
        opp_player = opp_players[0] if opp_players else None
        opp_name = opp_player.user.display_name if opp_player and opp_player.user else "حریف"
        
        my_player = my_players[0] if my_players else None
        my_name = my_player.user.display_name if my_player and my_player.user else "شما"

        text = (
            "⚔️ **حریف پیدا شد!** 🍅\n\n"
            f"🎮 **بازی:** {match.game.name_fa}\n"
            f"⚔️ **حالت:** {mode_title}\n\n"
            f"👤 **شما:** {my_name}\n"
            f"👤 **حریف:** {opp_name}\n\n"
            "⏳ برای شروع مسابقه، روی دکمه «پذیرش مسابقه» بزنید (مهلت: ۱۲۰ ثانیه)."
        )
    else:
        # Team Mode: group name, members display names, group ratings
        my_rating_val = my_group.rating.rating if my_group and my_group.rating else 1500.0
        opp_rating_val = opp_group.rating.rating if opp_group and opp_group.rating else 1500.0
        opp_title = opp_group.title if opp_group else "تیم حریف"

        my_roster = "\n".join([f"  • {p.user.display_name}" for p in my_players if p.user])
        opp_roster = "\n".join([f"  • {p.user.display_name}" for p in opp_players if p.user])

        text = (
            "⚔️ **حریف تیمی پیدا شد!** 🍅\n\n"
            f"🎮 **بازی:** {match.game.name_fa}\n"
            f"⚔️ **حالت:** {mode_title}\n\n"
            f"🛡️ **تیم شما:** {my_group.title} (ریتینگ: `{my_rating_val:g}`)\n"
            f"{my_roster}\n\n"
            f"⚔️ **تیم حریف:** {opp_title} (ریتینگ: `{opp_rating_val:g}`)\n"
            f"{opp_roster}\n\n"
            "⏳ یک تایید از هر تیم برای شروع کافی است (مهلت: ۱۲۰ ثانیه)."
        )

    return text
