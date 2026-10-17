"""Telegram gameplay handlers for Rock Paper Scissors."""

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sib.handlers.games.common import process_game_action

router = Router(name="game_rps")


@router.callback_query(F.data.startswith("rps_action:"))
async def handle_rps_action(callback: CallbackQuery, bot: Bot):
    """Handle a player selecting Rock, Paper, or Scissors."""
    parts = callback.data.split(":")
    match_id = int(parts[1])
    choice = parts[2]

    await process_game_action(
        callback, bot, match_id, {"choice": choice}, blind_commit=True
    )
