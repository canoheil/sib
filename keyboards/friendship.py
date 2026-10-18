"""Keyboards and strict callback parsing for Apple Friends."""
import re
from typing import Optional, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

FRIEND_ACCEPT = "friend:accept"
FRIEND_REJECT = "friend:reject"
_ROW_ID = re.compile(r"[0-9]{1,20}")


def get_friend_request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ قبول میکنم", callback_data=f"{FRIEND_ACCEPT}:{int(request_id)}"),
        InlineKeyboardButton(text="❌ رد میکنم", callback_data=f"{FRIEND_REJECT}:{int(request_id)}"),
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
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"profile:friends:{page - 1}"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"profile:friends:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="↩️ بازگشت به پروفایل", callback_data="apple_profile")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
