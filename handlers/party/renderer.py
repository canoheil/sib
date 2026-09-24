"""Rendering helpers for party messages in Persian."""

from sib.database.models import Party


def render_party_card(party: Party) -> str:
    """Render a party state into a characterful Persian message."""
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

    if party.is_full:
        header = "✅ **تیم کامل شد!**" if party.mode != "solo" else "✅ **آماده برای رقابت!**"
        status_line = ""
    else:
        header = "🍅 **پارتی بازی گروه**"
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
