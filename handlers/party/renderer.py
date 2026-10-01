"""Rendering helpers for party messages in Persian."""

from sib.database.models import Party, PartyStatus

# Live statuses that are not visible as "open/full party" states but still
# occupy the players; the card must say so instead of looking empty.
STATUS_LINES = {
    PartyStatus.IN_QUEUE: "\n🔍 **در جستجوی حریف...**",
    PartyStatus.IN_MATCH: "\n⚔️ **در حال مسابقه...**",
}


def render_party_card(party: Party) -> str:
    """Render a party state into a characterful Persian message."""
    if party.is_friendly:
        return render_friendly_party_card(party)

    mode_titles = {
        "solo": "انفرادی (Solo)",
        "2v2": "تیمی ۲ به ۲ (2v2)",
        "4v4": "تیمی ۴ به ۴ (4v4)",
    }
    mode_text = mode_titles.get(party.mode, party.mode)

    member_lines = []
    for member in party.members:
        name = member.user.display_name if member.user else "بازیکن"
        member_lines.append(f"👤 {name}")

    members_text = "\n".join(member_lines)

    status_line = STATUS_LINES.get(party.status)
    if status_line is not None:
        header = "🍏 **پارتی بازی گروه**"
    elif party.is_full:
        header = "✅ **تیم کامل شد!**" if party.mode != "solo" else "✅ **آماده برای رقابت!**"
        status_line = ""
    else:
        header = "🍏 **پارتی بازی گروه**"
        remaining = party.remaining_slots
        status_line = f"\n⏳ **{remaining} جای خالی**"

    text = (
        f"{header}\n\n"
        f"🎮 **{party.game.name_fa}**\n"
        f"⚔️ **{mode_text}**\n\n"
        f"**اعضای تیم:**\n"
        f"{members_text}"
        f"{status_line}"
    )
    return text


FRIENDLY_TEAM_TITLES = {
    1: "🍎 تیم سرخ",
    2: "🍏 تیم سبز",
}


def _member_line(member) -> str:
    """One roster line; bots are always clearly marked."""
    user = member.user
    name = user.display_name if user else "بازیکن"
    if user is not None and user.is_bot:
        return name if name.startswith("🤖 ") else f"🤖 {name}"
    return f"👤 {name}"


def render_friendly_party_card(party: Party) -> str:
    """Render a friendly party: both teams of one group-internal match.

    Shows the per-team rosters (bots marked), the free slots and the next
    action, in the established Sib party-card voice.
    """
    mode_titles = {
        "solo": "انفرادی (Solo - ۱ به ۱)",
        "2v2": "تیمی ۲ به ۲ (2v2)",
        "4v4": "تیمی ۴ به ۴ (4v4)",
    }
    mode_text = mode_titles.get(party.mode, party.mode)

    if party.status == PartyStatus.IN_MATCH:
        header = "⚔️ **این بازی رفیقانه شروع شده!**"
    elif party.is_full:
        header = "✅ **تیم رفیقانه کامل شد!**"
    else:
        header = "🎉 **بازی رفیقانه با اعضای گروه**"

    team_blocks = []
    for team in (1, 2):
        members = [m for m in party.members if int(m.team or 1) == team]
        lines = [_member_line(member) for member in members]
        if len(members) < party.team_size:
            lines.append(f"⏳ {party.team_size - len(members)} جای خالی")
        team_blocks.append(
            f"**{FRIENDLY_TEAM_TITLES[team]}** ({len(members)}/{party.team_size})\n"
            + "\n".join(lines)
        )

    if party.status == PartyStatus.IN_MATCH:
        status_line = "\n\n⚔️ مسابقه در جریانه و نتیجه‌اش روی رتبهٔ گروه اثری نداره."
    elif party.is_full:
        status_line = "\n\n🚀 همه آماده‌ن؛ بازی رو شروع کن!"
    else:
        status_line = (
            "\n\n➕ اعضای گروه می‌تونن به تیم‌ها ملحق بشن "
            "یا میزبان جای خالی رو با بات پر کنه.\n"
            "🍏 نتیجهٔ بازی رفیقانه روی رتبهٔ گروه اثری نداره و به‌جای نهال، سیب جایزه می‌ده."
        )

    return (
        f"{header}\n\n"
        f"🎮 **{party.game.name_fa}**\n"
        f"⚔️ **{mode_text}** — رفیقانه\n"
        f"👥 **{party.current_players_count}/{party.max_players}** بازیکن\n\n"
        f"🆚 **تیم‌ها:**\n"
        + "\n\n".join(team_blocks)
        + status_line
    )
