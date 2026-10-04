"""Inline keyboards + callback-data contract for the marriage flows.

Callback data carries ONE thing and nothing else: a numeric row id. It
never carries a Telegram user id, a display name, a group id or any other
user-controlled text, so a forged, replayed or stale button cannot address
anybody other than the row the domain already owns (authorization stays
entirely in the services, which re-read the row and compare the acting
user against it).

The wire format is fixed::

    marriage_accept:<proposal_id>
    marriage_reject:<proposal_id>
    marriage_divorce_confirm:<marriage_id>
    marriage_divorce_cancel:<marriage_id>

``parse_marriage_proposal_callback`` / ``parse_marriage_divorce_callback``
are deliberately strict - they return ``None`` for anything that is not
exactly their own shape instead of coercing it, so a malformed payload
fails closed.

Note what the divorce contract does NOT carry: the initiator's user id.
"Only the person who opened the confirmation may cancel it" is resolved
server-side from the panel-ownership registry, never from the payload.
"""

import re
from typing import Optional, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.keyboards.common import danger_button, success_button

#: Callback actions of a pending proposal.
MARRIAGE_ACCEPT = "marriage_accept"
MARRIAGE_REJECT = "marriage_reject"

#: Callback actions of a divorce confirmation card.
MARRIAGE_DIVORCE_CONFIRM = "marriage_divorce_confirm"
MARRIAGE_DIVORCE_CANCEL = "marriage_divorce_cancel"

MARRIAGE_ACTIONS: Tuple[str, ...] = (MARRIAGE_ACCEPT, MARRIAGE_REJECT)
MARRIAGE_DIVORCE_ACTIONS: Tuple[str, ...] = (
    MARRIAGE_DIVORCE_CONFIRM,
    MARRIAGE_DIVORCE_CANCEL,
)

# Row ids are positive integers and nothing else: no sign, no whitespace,
# no separators, no non-ASCII digits. Bounded so a hostile payload cannot
# hand an arbitrarily long literal to the database.
_ROW_ID_RE = re.compile(r"[0-9]{1,20}")

# Panel type recorded for the divorce confirmation card. Deliberately NOT
# in ``PERSONAL_PANEL_TYPES``: BOTH spouses must be able to press
# «بله، طلاق», so the panel guard must not lock the card to its opener.
DIVORCE_PANEL_TYPE = "marriage_divorce"


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
    if not _ROW_ID_RE.fullmatch(raw_id):
        return None
    proposal_id = int(raw_id)
    if proposal_id <= 0:
        return None
    return action, proposal_id


def marriage_divorce_callback(action: str, marriage_id: int) -> str:
    """Build the callback data for one divorce-confirmation button.

    ``marriage_id`` is coerced with ``int()`` so an ORM id or a numeric
    string cannot smuggle anything but a bare number into the payload.
    """
    if action not in MARRIAGE_DIVORCE_ACTIONS:
        raise ValueError(f"Unknown marriage divorce action: {action!r}")
    return f"{action}:{int(marriage_id)}"


def parse_marriage_divorce_callback(
    data: Optional[str],
) -> Optional[Tuple[str, int]]:
    """Split ``'marriage_divorce_<action>:<marriage_id>'`` into ``(action, id)``.

    Returns ``None`` for empty, unknown or malformed payloads - callers
    MUST treat ``None`` as "not our button" and stop.
    """
    if not data:
        return None
    action, separator, raw_id = data.partition(":")
    if not separator or action not in MARRIAGE_DIVORCE_ACTIONS:
        return None
    if not _ROW_ID_RE.fullmatch(raw_id):
        return None
    marriage_id = int(raw_id)
    if marriage_id <= 0:
        return None
    return action, marriage_id


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


def get_divorce_confirmation_keyboard(marriage_id: int) -> InlineKeyboardMarkup:
    """The «💔 بله، طلاق» / «❌ انصراف» pair on a divorce confirmation card.

    Either spouse may confirm (the domain decides who may actually end the
    marriage); only the user who opened the card may cancel it, which the
    handler checks against the panel-ownership registry.
    """
    buttons: list[list[InlineKeyboardButton]] = [
        [
            danger_button(
                text="💔 بله، طلاق",
                callback_data=marriage_divorce_callback(
                    MARRIAGE_DIVORCE_CONFIRM, marriage_id
                ),
            ),
            success_button(
                text="❌ انصراف",
                callback_data=marriage_divorce_callback(
                    MARRIAGE_DIVORCE_CANCEL, marriage_id
                ),
            ),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
