"""Rendering helpers for party messages in Persian."""

from sib.database.models import MatchType, Party, PartyStatus

STATUS_LINES = {
    PartyStatus.IN_QUEUE: "\n🔍 **در جستجوی حریف...**",
    PartyStatus.IN_MATCH: "\n⚔️ **در حال مسابقه...**",
}
MODE_TITLES = {
    "solo": "انفرادی (Solo)",
    "2v2": "تیمی ۲ به ۲ (2v2)",
    "4v4": "تیمی ۴ به ۴ (4v4)",
}
TEAM_CAPACITIES = {"solo": 1, "2v2": 2, "4v4": 4}


def _member_name(member) -> str:
    user = member.user
    name = user.display_name if user else "بازیکن"
    if user and user.is_bot:
        return f"🤖 {name}"
    return f"👤 {name}"


def render_party_card(party: Party) -> str:
    """Render a party state, showing Friendly team occupancy separately."""
    mode_text = MODE_TITLES.get(party.mode, party.mode)
    if party.match_type == MatchType.FRIENDLY:
        team_capacity = TEAM_CAPACITIES.get(party.mode, max(1, party.max_players // 2))
        team_counts = {
            team: sum(1 for member in party.members if member.team == team)
            for team in (1, 2)
        }
        lines = [
            "🤝 **پارتی دوستانه‌ی همین گروه**",
            "",
            f"🎮 **{party.game.name_fa}**",
            f"⚔️ **{mode_text}**",
            f"👥 **بازیکن‌ها:** `{party.current_players_count}/{party.max_players}`",
            "",
        ]
        for team in (1, 2):
            remaining = max(0, team_capacity - team_counts[team])
            label = "تیم ۱" if team == 1 else "تیم ۲"
            lines.append(
                f"**{label}** — `{team_counts[team]}/{team_capacity}` "
                f"(جای خالی: `{remaining}`)"
            )
            roster = [_member_name(member) for member in party.members if member.team == team]
            lines.extend(roster or ["  هنوز کسی توی این تیم نیست."])
            lines.append("")

        if party.status == PartyStatus.IN_MATCH:
            lines.append("⚔️ **بازی دوستانه شروع شده.**")
        elif party.status == PartyStatus.CANCELLED:
            lines.append("❌ **این پارتی لغو شده.**")
        elif party.is_full:
            lines.append("✅ **هر دو تیم آماده‌ان؛ بازی رو شروع کن!**")
        else:
            lines.append(f"⏳ **در مجموع {party.remaining_slots} جای خالی مونده.**")
        lines.append("🌱 این مسابقه روی آمار رقابتی و نهال اثری نداره.")
        return "\n".join(lines)

    member_lines = [_member_name(member) for member in party.members]
    members_text = "\n".join(member_lines) or "هنوز بازیکنی عضو نشده."
    status_line = STATUS_LINES.get(party.status)
    if status_line is not None:
        header = "🍏 **پارتی بازی گروه**"
    elif party.is_full:
        header = "✅ **تیم کامل شد!**" if party.mode != "solo" else "✅ **آماده برای رقابت!**"
        status_line = ""
    else:
        header = "🍏 **پارتی بازی گروه**"
        status_line = f"\n⏳ **{party.remaining_slots} جای خالی**"

    return (
        f"{header}\n\n"
        f"🎮 **{party.game.name_fa}**\n"
        f"⚔️ **{mode_text}**\n\n"
        f"**اعضای تیم:**\n"
        f"{members_text}"
        f"{status_line or ''}"
    )
