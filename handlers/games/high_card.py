"""Telegram gameplay handlers for High Card."""

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sib.handlers.games.common import process_game_action

router = Router(name="game_high_card")


@router.callback_query(F.data.startswith("hc_action:"))
async def handle_high_card_action(callback: CallbackQuery, bot: Bot):
    """Handle High Card actions: draw, keep, swap."""
    parts = callback.data.split(":")
    match_id = int(parts[1])
    action_type = parts[2]

    if action_type == "draw":
        # Initial card draw - just triggers validate_action which draws the card
        await process_game_action(callback, bot, match_id, {"draw": True})
    elif action_type == "keep":
        # Player keeps their current card
        await process_game_action(callback, bot, match_id, {"choice": "keep"})
    elif action_type == "swap":
        # Player swaps for a new card
        await process_game_action(callback, bot, match_id, {"choice": "swap"})
