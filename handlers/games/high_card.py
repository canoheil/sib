"""Telegram gameplay handlers for High Card."""

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sib.handlers.games.common import process_game_action, answer_invalid_game_callback

router = Router(name="game_high_card")


@router.callback_query(F.data.startswith("hc_action:"))
async def handle_high_card_action(callback: CallbackQuery, bot: Bot):
    """Handle a player drawing a card in High Card."""
    parts = (callback.data or "").split(":")
    try:
        if len(parts) < 2:
            raise ValueError
        match_id = int(parts[1])
    except (IndexError, ValueError):
        await answer_invalid_game_callback(callback)
        return

    await process_game_action(callback, bot, match_id, {"draw": True})
