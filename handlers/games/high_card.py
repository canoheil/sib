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
        # The common transaction draws and commits the card, then sends it
        # privately to this player before refreshing the neutral group board.
        await process_game_action(
            callback, bot, match_id, {"draw": True}, ephemeral_card=True
        )
    elif action_type == "keep":
        # Player keeps their current card
        await process_game_action(callback, bot, match_id, {"choice": "keep"})
    elif action_type == "swap":
        # A swap also draws a new card, which must be delivered privately.
        await process_game_action(
            callback, bot, match_id, {"choice": "swap"}, ephemeral_card=True
        )
