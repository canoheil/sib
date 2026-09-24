"""Telegram gameplay handlers for High Card."""

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sib.handlers.games.common import process_game_action

router = Router(name="game_high_card")


@router.callback_query(F.data.startswith("hc_action:"))
async def handle_high_card_action(callback: CallbackQuery, bot: Bot):
    """Handle a player drawing a card in High Card."""
    parts = callback.data.split(":")
    match_id = int(parts[1])

    await process_game_action(callback, bot, match_id, {"draw": True})
