"""Telegram gameplay handlers for Tic-Tac-Toe."""

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sib.handlers.games.common import process_game_action

router = Router(name="game_tic_tac_toe")


@router.callback_query(F.data.startswith("ttt_action:"))
async def handle_ttt_action(callback: CallbackQuery, bot: Bot):
    """Handle a player selecting a cell in Tic-Tac-Toe."""
    parts = callback.data.split(":")
    match_id = int(parts[1])
    cell = int(parts[2])

    await process_game_action(callback, bot, match_id, {"cell": cell})
