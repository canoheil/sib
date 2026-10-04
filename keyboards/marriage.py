"""Inline keyboards + callback-data contract for the marriage proposal flow.

Callback data carries ONE thing and nothing else: the numeric
``MarriageProposal`` id. It never carries a Telegram user id, a display
name, a group id or any other user-controlled text, so a forged, replayed
or stale button cannot address anybody other than the row the domain
already owns (authorization stays entirely in ``MarriageProposalService``,
which re-reads the proposal and compares the pressing user against
``target_id``).

The wire format is fixed::

    marriage_accept:<digits>
    marriage_reject:<digits>

``parse_marriage_proposal_callback`` is deliberately strict - it returns
``None`` for anything that is not exactly that shape instead of coercing
it, so a malformed payload fails closed.
"""

import re
from typing import Optional, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.keyboards.common import danger_button, success_button

#: Callback actions of a pending proposal. Accept/Reject HANDLERS land in a
#: later phase; the data contract is fixed here so the buttons that this
#: phase renders are already the buttons those handlers will receive.
MARRIAGE_ACCEPT = "marriage_accept"
MARRIAGE_REJECT = "marriage_reject"

MARRIAGE_ACTIONS: Tuple[str, ...] = (MARRIAGE_ACCEPT, MARRIAGE_REJECT)

# A proposal id is a positive integer and nothing else: no sign, no
# whitespace, no separators, no non-ASCII digits. Bounded so a hostile
# payload cannot hand an arbitrarily long literal to the database.
_PROPOSAL_ID_RE = re.compile(r"[0-9]{1,20}")


def marriage_proposal_callback(action: str, proposal_id: int) -> str:
    """Build the callback data for one proposal action button.

    ``proposal_id`` is coerced with ``int()`` so an ORM id or a numeric
    string cannot smuggle anything but a bare number into the payload.
    """
    if action not in MARRIAGE_ACTIONS:
        raise ValueError(f"Unknown marriage proposal action: {action!r}")
    return f"{action}:{int(proposal_id)}"


def parse_marriage_proposal_callback(
    data: Optional[str],
) -> Optional[Tuple[str, int]]:
    """Split ``'marriage_<action>:<proposal_id>'`` into ``(action, id)``.

    Returns ``None`` for empty, unknown or malformed payloads - callers
    MUST treat ``None`` as "not our button" and stop.
    """
    if not data:
        return None
    action, separator, raw_id = data.partition(":")
    if not separator or action not in MARRIAGE_ACTIONS:
        return None
    if not _PROPOSAL_ID_RE.fullmatch(raw_id):
        return None
    proposal_id = int(raw_id)
    if proposal_id <= 0:
        return None
    return action, proposal_id


def get_marriage_proposal_keyboard(proposal_id: int) -> InlineKeyboardMarkup:
    """The «قبول / رد» pair attached to a pending marriage proposal.

    Shared group surface: everybody sees the card, but the domain only
    lets the proposal's target resolve it.
    """
    buttons: list[list[InlineKeyboardButton]] = [
        [
            success_button(
                text="✅ قبول",
                callback_data=marriage_proposal_callback(MARRIAGE_ACCEPT, proposal_id),
            ),
            danger_button(
                text="❌ رد",
                callback_data=marriage_proposal_callback(MARRIAGE_REJECT, proposal_id),
            ),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
