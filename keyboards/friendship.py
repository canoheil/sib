"""Keyboards and strict callback parsing for Apple Friends.

Every button carries a glass color (see ``keyboards/common.py``):
- success (green)  -> accepting the request
- danger  (red)    -> rejecting the request, and the «back» row
- primary (blue)   -> list pagination (navigation)
"""
import re
from typing import Optional, Tuple

from aiogram.types import InlineKeyboardMarkup

from sib.keyboards.common import (
    danger_button,
    nav_back_row,
    primary_button,
    success_button,
)

FRIEND_ACCEPT = "friend:accept"
FRIEND_REJECT = "friend:reject"
_ROW_ID = re.compile(r"[0-9]{1,20}")


def get_friend_request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """The «قبول میکنم / رد میکنم» pair attached to a pending friend request.

    Shared group surface: everybody sees the card, but the domain only lets
    the request's recipient resolve it. Green confirms, red refuses - the
    same semantic pair the marriage proposal card uses.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        success_button(text="✅ قبول میکنم", callback_data=f"{FRIEND_ACCEPT}:{int(request_id)}"),
        danger_button(text="❌ رد میکنم", callback_data=f"{FRIEND_REJECT}:{int(request_id)}"),
    ]])


def parse_friend_callback(data: Optional[str]) -> Optional[Tuple[str, int]]:
    if not data:
        return None
    prefix, sep, raw_id = data.rpartition(":")
    if not sep or prefix not in (FRIEND_ACCEPT, FRIEND_REJECT) or not _ROW_ID.fullmatch(raw_id):
        return None
    request_id = int(raw_id)
    return (prefix, request_id) if request_id > 0 else None


def get_friend_list_keyboard(page: int, page_count: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 0:
        nav.append(primary_button(text="⬅️ قبلی", callback_data=f"profile:friends:{page - 1}"))
    if page + 1 < page_count:
        nav.append(primary_button(text="بعدی ➡️", callback_data=f"profile:friends:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend(nav_back_row("↩️ بازگشت به پروفایل", "apple_profile"))
    return InlineKeyboardMarkup(inline_keyboard=rows)
