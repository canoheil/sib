"""Keyboards for Matchmaking and Acceptance flow."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sib.keyboards.common import danger_button, success_button


def get_queue_keyboard(party_id: int) -> InlineKeyboardMarkup:
    """Keyboard shown while in matchmaking queue."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                danger_button(
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
            success_button(
                text="✅ پذیرش مسابقه",
                callback_data=f"accept_match:{match_id}",
            )
        ])
    buttons.append([
        danger_button(
            text="❌ رد مسابقه",
            callback_data=f"reject_match:{match_id}",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)
