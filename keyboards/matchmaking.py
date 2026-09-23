"""Keyboards for Matchmaking and Acceptance flow."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_queue_keyboard(party_id: int) -> InlineKeyboardMarkup:
    """Keyboard shown while in matchmaking queue."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ انصراف از صف جستجو",
                    callback_data=f"cancel_matchmaking:{party_id}",
                )
            ]
        ]
    )
    return keyboard


def get_acceptance_keyboard(match_id: int, has_accepted: bool = False) -> InlineKeyboardMarkup:
    """Keyboard for accepting or rejecting a found match."""
    buttons = []
    
    if not has_accepted:
        buttons.append([
            InlineKeyboardButton(
                text="✅ پذیرش مسابقه",
                callback_data=f"accept_match:{match_id}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="❌ رد مسابقه",
            callback_data=f"reject_match:{match_id}",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)
