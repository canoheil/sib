"""Inline keyboards for the global Halloween event («سیبومورتیس»).

All buttons use semantic Bot API 10.3 styles via ``sib.keyboards.common``
(``success_button``, ``primary_button``, ``danger_button``).
"""

from typing import Sequence

from aiogram.types import InlineKeyboardMarkup

from sib.keyboards.common import danger_button, primary_button, success_button
from sib.services.halloween import MilestoneStatus
from sib.services.sib_claim import fa_digits


def get_halloween_main_keyboard() -> InlineKeyboardMarkup:
    """Main Boss Menu keyboard (HW2: attacking is a TEXT action, not a button).

    The ⚔️ attack button was deliberately removed: players attack by
    replying to the Boss Menu message with the word «حمله» (see
    ``handlers.halloween.handle_halloween_attack_trigger``), which makes the
    raid a live group action instead of a private button tap. The remaining
    two rows are the read-only/utility screens.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [primary_button("🎁 مسیر جوایز", "hw:rewards")],
            [primary_button("🎃 دریافت غنیمت (دونه کدو)", "hw:loot")],
        ]
    )


def get_halloween_reward_track_keyboard(
    statuses: Sequence[MilestoneStatus],
) -> InlineKeyboardMarkup:
    """Milestone Reward Track buttons (locked / unlocked / claimed) + Back."""
    rows = []
    for item in statuses:
        pct_fa = fa_digits(item.milestone_pct)
        cb = f"hw:claim:{item.milestone_pct}"
        if item.state == "unlocked":
            rows.append([success_button(f"🎁 دریافت جایزه {pct_fa}٪", cb)])
        elif item.state == "claimed":
            rows.append([primary_button(f"✅ جایزه {pct_fa}٪ دریافت شد", cb)])
        else:
            rows.append([primary_button(f"🔒 مرحله {pct_fa}٪ (قفل)", cb)])
    rows.append([danger_button("🔙 بازگشت به منوی هالووین", "hw:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_loot_drop_keyboard(
    drop_id: int, claimed_count: int, max_claims: int
) -> InlineKeyboardMarkup:
    """Public group chest button (first ``max_claims`` players win seeds)."""
    if claimed_count >= max_claims:
        return InlineKeyboardMarkup(inline_keyboard=[])
    progress = f"{fa_digits(claimed_count)}/{fa_digits(max_claims)}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                success_button(
                    f"🎃 برداشتن دونه کدو ({progress})",
                    f"hw_drop:{drop_id}",
                )
            ]
        ]
    )
