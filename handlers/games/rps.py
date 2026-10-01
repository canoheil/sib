"""Telegram gameplay handlers for Rock Paper Scissors."""

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sib.handlers.games.common import process_game_action, answer_invalid_game_callback

router = Router(name="game_rps")


@router.callback_query(F.data.startswith("rps_action:"))
async def handle_rps_action(callback: CallbackQuery, bot: Bot):
    """Handle a player selecting Rock, Paper, or Scissors."""
    parts = (callback.data or "").split(":")
    try:
        if len(parts) != 3:
            raise ValueError
        match_id = int(parts[1])
        choice = parts[2]
    except (IndexError, ValueError):
        await answer_invalid_game_callback(callback)
        return

    await process_game_action(callback, bot, match_id, {"choice": choice})
